"""ChangeRequest V2 の適用の経路 (C10-3 / C10-E)。偽の WordPress だけ。

pin する契約:

- 本文の書き換え: 人が書いた本文だけ。リンクを消す・変える・足す提案、PR 表記を消す提案は作らない。
  適用は、その依頼の承認 + 方針のスイッチ (既定 false) のあとだけ。無効の間は WordPress に触れない。
  今の本文が提案のときと違えば止める (drift)。
- メタディスクリプション: 抜粋だけの書き込み (``{"excerpt": ...}``)。承認・方針・手元の drift・
  WordPress 側の drift を確かめる。read-back が一致したときだけ手元の値を揃え applied。
  一致しない・応答が分からない → outcome_unknown (手元は変えない)。2 回目は書かない (冪等)。
- Growth Action の承認では何も書かない (変更の依頼の承認が要る)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.exceptions import WordPressAmbiguousOutcomeError
from app.models import Article, ChangeApplication, ChangeRequest
from app.services.change_application_service import ChangeApplicationService
from app.services.change_request_service import ChangeRequestError, ChangeRequestService
from app.services.meta_description_apply_service import MetaDescriptionApplyService
from tests.integration.test_change_request_service import _SOURCE_BODY, _article

_NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
_META = ("RPA の導入の手順を、準備から運用まで順に整理します。"
         "社内で進めるときの注意点もまとめました。")
_NEW_META = ("RPA 導入の手順を 5 つの段階で整理。"
             "準備・ツール選び・試行・展開・運用の要点と注意点。")


class _Settings:
    wordpress_base_url = "https://example.com"


def _factory(session):
    class _Scoped:
        def __call__(self):
            return self

        def __enter__(self):
            return session

        def __exit__(self, *_exc):
            return False

    return _Scoped()


class _FakeWP:
    def __init__(self, excerpt: str, *, after: str | None = None, error=None):
        self.excerpt, self.after, self.error = excerpt, after, error
        self.writes: list[tuple[int, dict]] = []

    def get_post(self, post_id: int) -> dict:
        return {"id": post_id, "excerpt": {"raw": self.excerpt}}

    def update_post_excerpt_exact(self, post_id: int, payload_json: str) -> dict:
        payload = json.loads(payload_json)
        assert set(payload) == {"excerpt"}  # 抜粋だけ
        self.writes.append((post_id, payload))
        if self.error:
            raise self.error
        self.excerpt = self.after if self.after is not None else payload["excerpt"]
        return {"id": post_id}


@pytest.fixture
def article(session):
    a = _article(session, 18, "rpa-implementation",
                 body="PR: 本記事は広告を含みます。\n" + _SOURCE_BODY)
    a.meta_description = _META
    session.commit()
    return a


def _approved(session, request):
    ChangeRequestService(session).approve(request.id, proposal_hash=request.proposal_hash,
                                          now=_NOW)
    session.refresh(request)
    return request


# == 本文の書き換え ================================================================================
def test_text_edit_proposals_keep_links_and_disclosure(session, article) -> None:
    service = ChangeRequestService(session)
    body = article.body
    with pytest.raises(ChangeRequestError, match="existing links must stay"):
        service.propose_text_edit(article_id=18, proposed_body=body.replace(
            "[公式](https://official.example.jp/docs)", "公式"), rationale="r", now=_NOW)
    with pytest.raises(ChangeRequestError, match="must not add links"):
        service.propose_text_edit(article_id=18, proposed_body=body + "\n[x](https://x.test/)",
                                  rationale="r", now=_NOW)
    with pytest.raises(ChangeRequestError, match="PR disclosure"):
        service.propose_text_edit(article_id=18, proposed_body=body.replace(
            "PR: 本記事は広告を含みます。\n", ""), rationale="r", now=_NOW)
    with pytest.raises(ChangeRequestError, match="identical"):
        service.propose_text_edit(article_id=18, proposed_body=body, rationale="r", now=_NOW)
    edited = body.replace("導入の段落です。", "導入の段落を書き直しました。")
    request = service.propose_text_edit(article_id=18, proposed_body=edited,
                                        rationale="clarify the intro", idempotency_key="te-1",
                                        now=_NOW)
    assert (request.change_type, request.status, request.source_engine) == (
        "text_edit", "awaiting_approval", "manual")
    assert request.proposal_json["removed_lines"] == 1 and "-導入の段落です。" in (
        request.proposal_json["diff"])
    again = service.propose_text_edit(article_id=18, proposed_body=edited,
                                      rationale="clarify the intro", now=_NOW)
    assert again.id == request.id  # 同じ内容は作り直さない


def test_text_edit_apply_is_gated_and_writes_nothing_while_disabled(session, article,
                                                                    monkeypatch) -> None:
    edited = article.body.replace("導入の段落です。", "導入を整理しました。")
    request = ChangeRequestService(session).propose_text_edit(
        article_id=18, proposed_body=edited, rationale="r", now=_NOW)
    service = ChangeApplicationService(_factory(session), settings=_Settings(),
                                       wordpress_client=object())
    unapproved = service.plan(request.id)
    assert any("only 'approved'" in r for r in unapproved.blocked_reasons)
    _approved(session, request)
    disabled = service.apply(request.id, execute=True)
    assert disabled.outcome == "blocked" and any("text_edit apply is not enabled" in r
                                                 for r in disabled.blocked_reasons)
    assert session.scalar(select(func.count()).select_from(ChangeApplication)) == 0
    from app.services import change_apply_policy

    monkeypatch.setattr(change_apply_policy, "text_edit_apply_enabled", lambda policy=None: True)
    ready = service.plan(request.id)
    assert ready.blocked_reasons == []  # 承認・hash・規則がそろえば適用できる (ここでは書かない)
    article.body = article.body + "\n追記"  # 提案の後に本文が変わった
    session.commit()
    drift = service.plan(request.id)
    assert any("body changed after the proposal" in r for r in drift.blocked_reasons)


# == メタディスクリプション ========================================================================
def _meta_request(session):
    request = ChangeRequestService(session).propose_meta_description(
        article_id=18, proposed_meta=_NEW_META, rationale="snippet CTR", now=_NOW)
    return _approved(session, request)


def _enable(monkeypatch):
    from app.services import change_apply_policy

    monkeypatch.setattr(change_apply_policy, "meta_description_apply_enabled",
                        lambda policy=None: True)


def test_meta_apply_writes_only_the_excerpt_after_approval(session, article, monkeypatch) -> None:
    request = _meta_request(session)
    assert request.change_type == "meta_description"
    assert request.proposed_body == article.body  # 本文は変えない
    wp = _FakeWP(_META)
    service = MetaDescriptionApplyService(_factory(session), settings=_Settings(),
                                          wordpress_client=wp)
    disabled = service.apply(request.id, execute=True)
    assert disabled.outcome == "blocked" and wp.writes == []  # 既定は無効
    _enable(monkeypatch)
    assert service.apply(request.id).outcome == "planned" and wp.writes == []  # PLAN は書かない
    done = service.apply(request.id, execute=True, now=_NOW)
    assert done.outcome == "succeeded" and done.wordpress_writes == 1
    assert wp.writes == [(1018, {"excerpt": _NEW_META})]
    session.refresh(article)
    assert article.meta_description == _NEW_META
    assert session.get(ChangeRequest, request.id).status == "applied"
    [app_row] = session.scalars(select(ChangeApplication)).all()
    assert app_row.result_json["pre_excerpt"] == _META and app_row.outcome == "succeeded"
    again = service.apply(request.id, execute=True)
    assert again.outcome == "blocked" and len(wp.writes) == 1  # 2 回目は書かない


def test_meta_apply_refuses_drift_on_either_side(session, article, monkeypatch) -> None:
    _enable(monkeypatch)
    request = _meta_request(session)
    live_changed = _FakeWP("WordPress で誰かが書き換えた抜粋です。二十文字以上あります。")
    out = MetaDescriptionApplyService(_factory(session), settings=_Settings(),
                                      wordpress_client=live_changed).apply(request.id,
                                                                           execute=True)
    assert out.outcome == "blocked" and live_changed.writes == []
    assert any("live WordPress excerpt differs" in r for r in out.blocked_reasons)
    article.meta_description = "手元で書き換えたメタディスクリプションです。二十文字以上。"
    session.commit()
    local = MetaDescriptionApplyService(_factory(session), settings=_Settings(),
                                        wordpress_client=_FakeWP(_META)).plan(request.id)
    assert any("local meta description changed" in r for r in local.blocked_reasons)


def test_meta_apply_mismatch_or_ambiguity_is_outcome_unknown(session, article,
                                                             monkeypatch) -> None:
    _enable(monkeypatch)
    request = _meta_request(session)
    mismatch = _FakeWP(_META, after="違う抜粋")
    out = MetaDescriptionApplyService(_factory(session), settings=_Settings(),
                                      wordpress_client=mismatch).apply(request.id, execute=True)
    assert out.outcome == "outcome_unknown"
    session.refresh(article)
    assert article.meta_description == _META  # 手元は変えない
    assert session.get(ChangeRequest, request.id).status == "apply_failed"
    other = ChangeRequestService(session).propose_meta_description(
        article_id=18, proposed_meta=_NEW_META + "。", rationale="r", now=_NOW)
    _approved(session, other)
    ambiguous = _FakeWP(_META, error=WordPressAmbiguousOutcomeError("timeout"))
    out2 = MetaDescriptionApplyService(_factory(session), settings=_Settings(),
                                       wordpress_client=ambiguous).apply(other.id, execute=True)
    assert out2.outcome == "outcome_unknown" and out2.wordpress_writes == 1


def test_meta_proposals_follow_the_length_rules(session, article) -> None:
    service = ChangeRequestService(session)
    with pytest.raises(ChangeRequestError, match="outside 20-220"):
        service.propose_meta_description(article_id=18, proposed_meta="短い", rationale="r")
    with pytest.raises(ChangeRequestError, match="HTML"):
        service.propose_meta_description(article_id=18, proposed_meta="<b>" + _NEW_META + "</b>",
                                         rationale="r")
    with pytest.raises(ChangeRequestError, match="identical"):
        service.propose_meta_description(article_id=18, proposed_meta=_META, rationale="r")


def test_the_body_path_refuses_meta_requests(session, article) -> None:
    request = _meta_request(session)
    plan = ChangeApplicationService(_factory(session), settings=_Settings(),
                                    wordpress_client=object()).plan(request.id)
    assert any("MetaDescriptionApplyService" in r for r in plan.blocked_reasons)


def test_the_excerpt_adapter_accepts_only_the_exact_payload() -> None:
    from app.wordpress.client import _assert_exact_excerpt_payload

    _assert_exact_excerpt_payload(json.dumps({"excerpt": "ok"}))
    for bad in ({"excerpt": "ok", "content": "x"}, {"excerpt": ""}, {"title": "x"}, ["x"]):
        with pytest.raises(ValueError):
            _assert_exact_excerpt_payload(json.dumps(bad))


def test_the_committed_policy_keeps_both_apply_paths_disabled() -> None:
    from app.services.change_apply_policy import (
        meta_description_apply_enabled,
        text_edit_apply_enabled,
    )

    assert text_edit_apply_enabled() is False and meta_description_apply_enabled() is False
    assert Article is not None
