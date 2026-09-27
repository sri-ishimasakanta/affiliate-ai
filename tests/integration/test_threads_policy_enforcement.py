"""T6.3.1a: 生成の方針を守らせる (偽の Luna だけ。本物の API・本番の依頼は使わない)。

- 呼び出しごとの記録 (1 回目を上書きしない・書き直しの理由の ID・使った token)
- 計画の link_mode の拘束 (none → リンクなし、article → {link} は 1 つまで)
- 本文の長さの方針は 1 つ (280〜360 字。古い「目安 380」を出さない)
- 最近の話題との比較の記録 (窓・近い上位・止めた相手)。閾値は変えない

偽の E2E:

- A: 書き直しの記録 (1 回目と書き直しの出力が両方残る)
- B: link none を求めたのに article → 書き直し → none・{link} なし
- C: link の食い違いが続く → 保存しない (記録は残る・繰り返さない)
- D: 最近の話題と重なる → 止めた記録 (止めた相手の提案つき)
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from app.models import ThreadsPostProposal
from app.services.threads_openai_provider import OpenAIGenerationProvider
from app.social.threads.policy import load_operations_policy
from app.social.threads.quality import RECENT_WINDOW, overlap, reason_ids
from tests.integration.test_threads_openai_generation import API_KEY, FakeLuna, seed_articles
from tests.integration.test_threads_quality_generation import (
    DENSE,
    FACTS,
    NOISE_POINT,
    ONE_POINT_QUESTION,
    PRICE_REWORD,
    _body,
    _existing_price_proposal,
    _generate,
    _no_side_effects,
    _request,
)


@pytest.fixture
def articles(session):
    rows = seed_articles(session)
    for article in rows:
        article.body = FACTS + f" 記事{article.id}。"
    session.commit()
    return rows


def _record(tmp_path, request) -> dict:
    for folder in ("pending", "done", "failed"):
        path = tmp_path / "gen" / folder / f"{request.request_id}.openai.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise AssertionError("no call record")


def _raw_record(tmp_path, request) -> str:
    return json.dumps(_record(tmp_path, request), ensure_ascii=False)


def _count(session) -> int:
    return session.scalar(select(func.count()).select_from(ThreadsPostProposal))


# --- A. 呼び出しごとの記録 ---------------------------------------------------------------


def test_a_one_call_success_records_exactly_one_call(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question")
    out = _generate(session, tmp_path, request, FakeLuna([ONE_POINT_QUESTION]))
    assert len(out["created"]) == 1
    record = _record(tmp_path, request)
    assert record["generation_attempts"] == 1
    assert record["model_calls"] == 1 and record["repair_calls"] == 0
    (call,) = record["history"]
    assert call["ordinal"] == 1 and call["purpose"] == "initial"
    assert call["requested_model"] == call["returned_model"] == record["model"]
    assert call["result"] == "ok" and call["at"]
    assert call["output"]["proposals"][0]["body"] == ONE_POINT_QUESTION
    assert call["validation"] == {
        "ok": True, "reason_ids": [], "reasons": None, "audit": call["validation"]["audit"],
    }  # fmt: skip
    assert "repair_reason_ids" not in call


def test_a_repair_keeps_both_outputs_and_the_exact_reason(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question")
    fake = FakeLuna([DENSE, ONE_POINT_QUESTION])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    record = _record(tmp_path, request)
    assert (record["generation_attempts"], record["model_calls"], record["repair_calls"]) == (
        1, 2, 1,
    )  # fmt: skip
    first, second = record["history"]
    # 1 回目は上書きされない: 元の出力と、落ちた理由がそのまま残る。
    assert first["purpose"] == "initial" and first["output"]["proposals"][0]["body"] == DENSE
    assert first["validation"]["ok"] is False
    assert "excessive_price_density" in first["validation"]["reason_ids"]
    assert "hook_semantic_mismatch" in first["validation"]["reason_ids"]
    # 書き直しの呼び出しは、送った理由 (文と ID) を持つ。1 回目の検査の理由と同じ。
    repair_text = fake.payload(1)["input"][2]["content"]
    assert second["purpose"] == "repair" and second["ordinal"] == 2
    assert second["repair_reason_ids"] == first["validation"]["reason_ids"]
    assert second["repair_reasons"] and second["repair_reasons"] in repair_text
    assert second["output"]["proposals"][0]["body"] == ONE_POINT_QUESTION
    assert second["validation"]["ok"] is True


def test_a_usage_is_recorded_per_call_and_in_aggregate(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question")
    _generate(session, tmp_path, request, FakeLuna([DENSE, ONE_POINT_QUESTION]))
    record = _record(tmp_path, request)
    per_call = [c["usage"] for c in record["history"]]
    assert all(u["input_tokens"] == 1200 and u["output_tokens"] == 180 for u in per_call)
    assert record["usage"]["input_tokens"] == 2400
    assert record["usage"]["output_tokens"] == 360
    assert record["usage"]["total_tokens"] == 2760


def test_a_no_secret_is_persisted_in_the_call_record(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question")
    _generate(session, tmp_path, request, FakeLuna([DENSE, ONE_POINT_QUESTION]))
    raw = _raw_record(tmp_path, request)
    assert API_KEY not in raw and "Authorization" not in raw and "Bearer" not in raw


def test_a_legacy_records_without_history_stay_readable(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question")
    legacy = {"request_id": request.request_id, "model": "gpt-5.6-luna", "calls": 2,
              "repairs": 1, "initial_call_made": True, "result": "repaired",
              "usage": {"input_tokens": 10}}  # fmt: skip
    path = tmp_path / "gen" / "pending" / f"{request.request_id}.openai.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    provider = OpenAIGenerationProvider(tmp_path / "gen", client=None)
    assert provider._read_record(request.request_id)["result"] == "repaired"
    provider.record_validation(request, ok=True)  # 履歴の無い古い記録は書き換えない
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


# --- B. link_mode の拘束 ---------------------------------------------------------------


def test_b_the_schema_pins_the_requested_link_mode(session, articles, tmp_path) -> None:
    for mode in ("none", "article"):
        request = _request(session, tmp_path, hook="question", link_mode=mode,
                           article_id=21 if mode == "none" else 22)  # fmt: skip
        fake = FakeLuna([ONE_POINT_QUESTION if mode == "none" else NOISE_POINT])
        _generate(session, tmp_path, request, fake)
        props = fake.payload(0)["text"]["format"]["schema"]["properties"]["proposals"]["items"][
            "properties"
        ]
        assert props["link_mode"]["enum"] == [mode]
        assert f"link_mode は {mode} に固定する" in request.prompt


def test_b_none_returned_as_article_is_repaired_to_none(session, articles, tmp_path) -> None:
    """偽の E2E B。"""

    request = _request(session, tmp_path, hook="question", link_mode="none")
    wrong = {"link_mode": "article", "body": ONE_POINT_QUESTION + "\n{link}"}
    fake = FakeLuna([wrong, ONE_POINT_QUESTION])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    row = session.get(ThreadsPostProposal, out["created"][0])
    assert row.link_mode == "none" and "{link}" not in row.content_text
    assert row.destination_url is None
    record = _record(tmp_path, request)
    first, second = record["history"]
    assert first["output"]["proposals"][0]["link_mode"] == "article"
    assert first["validation"]["reason_ids"] == ["link_mode_mismatch"]
    assert second["repair_reason_ids"] == ["link_mode_mismatch"]
    assert "keep link_mode=none" in second["repair_reasons"]
    # 書き直しでも、計画の link_mode のまま (schema も同じ)。
    enums = [
        fake.payload(i)["text"]["format"]["schema"]["properties"]["proposals"]["items"][
            "properties"]["link_mode"]["enum"] for i in (0, 1)
    ]  # fmt: skip
    assert enums == [["none"], ["none"]]
    _no_side_effects(session)


def test_b_article_returned_as_none_is_rejected(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question", link_mode="article")
    wrong = {"link_mode": "none", "body": ONE_POINT_QUESTION}
    fake = FakeLuna([wrong, ONE_POINT_QUESTION + "\n{link}"])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    row = session.get(ThreadsPostProposal, out["created"][0])
    assert row.link_mode == "article" and row.destination_url
    assert _record(tmp_path, request)["history"][0]["validation"]["reason_ids"] == [
        "link_mode_mismatch"
    ]


def test_b_none_with_a_link_placeholder_is_rejected(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question", link_mode="none")
    wrong = {"link_mode": "none", "body": ONE_POINT_QUESTION + "\n{link}"}
    fake = FakeLuna([wrong, ONE_POINT_QUESTION])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    first = _record(tmp_path, request)["history"][0]
    assert "must not contain {link}" in first["validation"]["reasons"]
    assert first["validation"]["reason_ids"] == ["link_mode_mismatch"]


def test_b_article_with_two_placeholders_is_rejected(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question", link_mode="article")
    wrong = {"link_mode": "article", "body": "{link}\n" + ONE_POINT_QUESTION + "\n{link}"}
    fake = FakeLuna([wrong, ONE_POINT_QUESTION + "\n{link}"])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    assert _body(session, out["created"][0]).count("https://") == 1  # URL は 1 つだけ
    first = _record(tmp_path, request)["history"][0]
    assert first["validation"]["reason_ids"] == ["link_mode_mismatch"]


def test_c_persistent_link_mismatch_fails_safely(session, articles, tmp_path) -> None:
    """偽の E2E C: 書き直しでも食い違えば保存しない。記録は残り、繰り返さない。"""

    request = _request(session, tmp_path, hook="question", link_mode="none")
    wrong = {"link_mode": "article", "body": ONE_POINT_QUESTION + "\n{link}"}
    fake = FakeLuna([wrong, wrong])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and out["created"] == [] and _count(session) == 0
    assert any("does not match the requested 'none'" in f["reason"] for f in out["failures"])
    record = _record(tmp_path, request)
    assert (record["model_calls"], record["repair_calls"]) == (2, 1)
    assert [c["validation"]["ok"] for c in record["history"]] == [False, False]
    assert record["history"][1]["validation"]["reason_ids"] == ["link_mode_mismatch"]
    assert record["fallback"] == "manual"
    assert (tmp_path / "gen/pending" / f"{request.request_id}.request.json").exists()
    again = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and again["created"] == []  # 送り直さない (繰り返さない)
    _no_side_effects(session)


def test_b_planner_link_share_is_unchanged() -> None:
    policy = load_operations_policy()
    assert float(policy.proposal_stock("article_link_share_max", None)) == 0.34


def test_b_hook_binding_is_unchanged(session, articles, tmp_path) -> None:
    request = _request(session, tmp_path, hook="question")
    wrong = {"conversation_hook": "choice"}
    fake = FakeLuna([{**wrong, "body": ONE_POINT_QUESTION}, ONE_POINT_QUESTION])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    first = _record(tmp_path, request)["history"][0]
    assert "hook_mismatch" in first["validation"]["reason_ids"]


# --- C. 長さの方針は 1 つ ----------------------------------------------------------------


def test_prompt_has_one_prose_target_and_no_stale_380(session, articles, tmp_path) -> None:
    for hook in ("question", "none"):
        request = _request(session, tmp_path, hook=hook, article_id=21 if hook == "none" else 22)
        assert "380" not in request.prompt and "目安" not in request.prompt.split("本文は普通")[0]
        assert request.prompt.count("280〜360") == 1
        assert "420" in request.prompt and "500 文字以内" in request.prompt


# --- D. 最近の話題との比較の記録 ---------------------------------------------------------


def test_d_overlap_block_is_audited_with_the_responsible_item(
    session, articles, tmp_path
) -> None:
    """偽の E2E D: 本物の本番の例ではない (テストの中だけで重ねる)。"""

    existing = _existing_price_proposal(session, tmp_path)
    request = _request(session, tmp_path, article_id=22, hook="question")
    fake = FakeLuna([PRICE_REWORD, NOISE_POINT])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 2 and len(out["created"]) == 1
    first, second = _record(tmp_path, request)["history"]
    audit = first["validation"]["audit"]["overlap"]
    assert audit["recent_window"] == 1
    assert audit["blocked"] is True and audit["reason"] == "recent_topic_overlap"
    assert audit["blocked_by"] == f"proposal #{existing.id}"
    top = audit["top"][0]
    assert top["ref"] == audit["blocked_by"] and top["high"] is True
    assert top["signature"] and {"$10", "$18"} <= set(top["shared_numbers"]) | {
        n.replace("＄", "$") for n in top["shared_numbers"]
    }
    assert audit["max_containment"] == max(c["containment"] for c in audit["top"])
    assert "recent_topic_overlap" in first["validation"]["reason_ids"]
    assert "recent_topic_overlap" in second["repair_reason_ids"]
    # 書き直しの答えは重ならない: 止めなかった記録として残り、提案にも写る。
    assert second["validation"]["audit"]["overlap"]["blocked"] is False
    brief = session.get(ThreadsPostProposal, out["created"][0]).learning_guidance_json[
        "generation_brief"
    ]
    assert brief["overlap"]["blocked"] is False and brief["overlap"]["recent_window"] == 1
    _no_side_effects(session)


def test_d_non_overlapping_output_is_accepted_with_audit(session, articles, tmp_path) -> None:
    _existing_price_proposal(session, tmp_path)
    request = _request(session, tmp_path, article_id=21, hook="question")
    fake = FakeLuna([NOISE_POINT])
    out = _generate(session, tmp_path, request, fake)
    assert fake.calls == 1 and len(out["created"]) == 1
    audit = _record(tmp_path, request)["history"][0]["validation"]["audit"]["overlap"]
    assert audit["blocked"] is False and audit["blocked_by"] is None and audit["reason"] is None
    assert audit["recent_window"] == 1 and len(audit["top"]) == 1


def test_d_overlap_thresholds_are_unchanged() -> None:
    assert RECENT_WINDOW == 12
    same = "Fireflies.aiは月払い$18、年払い$10。KrispのCoreは$8。料金の比較。"
    assert overlap(same, same)["high"] is True  # 包含 1.0 >= 0.6
    assert overlap("会議の雑音を減らす話。", "月額の料金をそろえて比べる話。")["high"] is False
    # 製品 1 つ + 数字 2 つ + 軸が同じ → 高い (T6.3.1 のまま)
    one = overlap("Krispは月$8。年払い$10で料金を比べる。", "Krispの料金は$8と$10。")
    assert one["shared_entities"] == ["krisp"] and one["high"] is True


def test_reason_ids_are_deterministic() -> None:
    assert reason_ids("") == []
    assert reason_ids("something unexpected") == ["other"]
    assert reason_ids(
        "conversation check failed: link_mode 'article' does not match the requested 'none'; "
        "keep link_mode=none"
    ) == ["link_mode_mismatch"]
    assert reason_ids("quality check failed: recent topic overlap with proposal #3 (products "
                      "krisp, facts $8, axes price)") == ["recent_topic_overlap"]  # fmt: skip
    assert reason_ids("quality check failed: prose is 450 chars; keep it within 420") == [
        "prose_over_quality_limit"
    ]
