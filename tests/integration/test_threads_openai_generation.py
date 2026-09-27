"""T6.2: OpenAI (GPT-5.6 Luna) による投稿案の自動生成 (偽の通信だけ。本物の API は呼ばない)。

pin する契約:

- Responses API へ、manual と **同じ prompt** を、モデル ``gpt-5.6-luna``・reasoning
  ``medium``・Structured Outputs (strict な JSON schema) で送る。鍵はヘッダーにだけ置く。
- モデルの出力は信用しない入力: いつもの検査 (長さ・切り口・リンク・文体・重複・来歴) と
  自動生成用の事実の検査を通ったものだけが ``awaiting_approval`` になる。
- 承認は人だけ: OpenAI の答え・正しい案・digest の送信は、どれも承認ではない。provider は
  公開も digest の送信もできない。
- 通信の失敗 (timeout / 429 / 5xx) は最大 2 回まで再試行。検査落ちの書き直しは 1 回だけ。
  上限の後は manual の依頼として残る。同じ依頼を 2 度送らない。答え待ちがあれば新しく
  作らない。下限以上なら呼ばない。heartbeat では呼ばない。
- 鍵が無ければ呼ばずに manual の依頼として残す。生成の故障で worker は止まらない。
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.models import (
    TP_APPROVED,
    TP_AWAITING_APPROVAL,
    MobileApprovalSession,
    NotificationDelivery,
    ThreadsPostProposal,
    ThreadsPublication,
)
from app.services.threads_generation_provider import ManualFileProvider, build_provider
from app.services.threads_openai_provider import (
    DEFAULT_MODEL,
    MAX_TRANSPORT_RETRIES,
    OpenAIGenerationProvider,
    OpenAIResponsesClient,
    build_openai_provider,
    sanitize_usage,
)
from tests.integration.test_threads_proposal_stock_service import (
    _NOW,
    _factory,
    _ReadOnlyThreads,
    _service,
    _Settings,
)


@pytest.fixture
def articles(session):
    """在庫の保守のテストと同じ 5 記事 (3 トピック)。"""

    return seed_articles(session)


def seed_articles(session):
    """5 記事 (3 トピック) を入れる (ほかのテストのモジュールからも使う)。"""

    from app.models import Article, Keyword

    topics = {}
    for word in ("生成AI", "Make", "AIエージェント"):
        topics[word] = Keyword(keyword=word)
        session.add(topics[word])
    session.flush()
    rows = []
    for article_id, word in ((21, "生成AI"), (22, "生成AI"), (23, "Make"),
                             (24, "AIエージェント"), (25, "Make")):  # fmt: skip
        row = Article(
            id=article_id, title=f"記事{article_id}", slug=f"article-{article_id}",
            body=f"記事{article_id}の本文。体制を先に決めるほうが早い。", status="published",
            published_url=f"https://bizfluxlab.com/article-{article_id}/",
            published_at=_NOW - timedelta(days=60), article_type="informational",
            keyword_id=topics[word].id,
        )  # fmt: skip
        session.add(row)
        rows.append(row)
    session.commit()
    return rows


API_KEY = "sk-test-NEVER-A-REAL-KEY-0123456789abcdef"
_ID = re.compile(r"記事 \(id=(\d+)\)")
# 記事ごとに中身の違う本文 (T6.3.1 の「最近の話題の重なり」に当たらないように)。
DISTINCT_BODIES = (
    "記事{aid}の話。体制を先に決めるほうが早い。担当が決まると迷いが減る。",
    "記事{aid}の話。範囲を小さく始めると、途中で止まりにくい。",
    "記事{aid}の話。最初の週は記録の置き場所だけをそろえる。",
    "記事{aid}の話。道具より先に、誰が確認するかを決めておく。",
    "記事{aid}の話。うまくいかない日は、手順を一つ減らしてみる。",
)
HOOK_ENDINGS = {
    "none": "",
    "question": "今の現場では、最初にどこから決めている？",
    "choice": "担当を先に決めるか、道具を先に決めるか。どちらから始める？",
    "experience": "導入のとき、どこで止まった？",
    "opinion": "道具が先という見方もあると思う。",
}


class FakeLuna:
    """Responses API の代役 (httpx.MockTransport)。受け取った要求を記録する。"""

    def __init__(self, script=None, *, body_for=None):
        self.requests: list[httpx.Request] = []
        self.script = list(script or [])
        self._default_body = body_for is None
        self.body_for = body_for or (
            lambda aid, angle, n: DISTINCT_BODIES[aid % len(DISTINCT_BODIES)].format(aid=aid)
        )

    @property
    def calls(self) -> int:
        return len(self.requests)

    def payload(self, index=-1) -> dict:
        return json.loads(self.requests[index].content)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, httpx.Response):
            return step
        if step == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if isinstance(step, int):
            return httpx.Response(step, json={"error": {"message": "nope"}})
        payload = json.loads(request.content)
        angle = payload["text"]["format"]["schema"]["properties"]["proposals"]["items"][
            "properties"
        ]["angle"]["enum"][0]
        aid = int(_ID.search(payload["input"][0]["content"]).group(1))
        body = (
            step
            if isinstance(step, str) and step != "ok"
            else self.body_for(aid, angle, self.calls)
        )
        props = payload["text"]["format"]["schema"]["properties"]["proposals"]["items"][
            "properties"
        ]
        item = {"angle": angle, "link_mode": "none", "body": body}
        if "conversation_hook" in props:  # T6.3: 求められたきっかけをそのまま返す
            hook = props["conversation_hook"]["enum"][0]
            if self._default_body and not (isinstance(step, str) and step != "ok"):
                body = body + HOOK_ENDINGS[hook]  # T6.3.1: 求めた形に合う終わり方
            link = props["link_mode"]["enum"][0] if len(props["link_mode"]["enum"]) == 1 else "none"
            if link == "article" and "{link}" not in body:
                body = body + "\n{link}"  # T6.3.1a: 計画の link_mode は拘束
            item = {"angle": angle, "conversation_hook": hook, "link_mode": link, "body": body}
        if isinstance(step, dict):  # T6.3.1a: 項目を上書きする (schema に反する答えの代役)
            item.update(step)
        if step == "malformed":
            text = "not json"
        else:
            text = json.dumps({"proposals": [item]}, ensure_ascii=False)
        return httpx.Response(200, json={
            "id": f"resp_{self.calls}", "model": DEFAULT_MODEL, "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 1200, "output_tokens": 180, "total_tokens": 1380,
                      "output_tokens_details": {"reasoning_tokens": 64}, "secret": API_KEY},
        })  # fmt: skip


def _provider(tmp_path, fake: FakeLuna, sleeps=None) -> OpenAIGenerationProvider:
    client = OpenAIResponsesClient(
        api_key=API_KEY,
        transport=httpx.MockTransport(fake.handler),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )
    return OpenAIGenerationProvider(tmp_path / "gen", client=client)


def _proposals(session):
    return session.scalars(select(ThreadsPostProposal).order_by(ThreadsPostProposal.id)).all()


def _no_human_decisions(session):
    assert session.scalar(select(func.count()).select_from(ThreadsPublication)) == 0
    assert session.scalar(select(func.count()).select_from(MobileApprovalSession)) == 0
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
    assert all(p.status == TP_AWAITING_APPROVAL for p in _proposals(session))
    assert not any(p.status == TP_APPROVED or p.approved_at for p in _proposals(session))


def _all_files(tmp_path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8") for p in (tmp_path / "gen").rglob("*") if p.is_file()
    )


# == happy path / request shape ======================================================
def test_luna_generates_validated_proposals_that_still_need_human_approval(
    session, articles, tmp_path
) -> None:
    fake = FakeLuna()
    provider = _provider(tmp_path, fake)
    outcome = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert len(outcome["created"]) == 3 and fake.calls == 3  # 下限 3・1 回 3 件まで
    payload = fake.payload(0)
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["reasoning"] == {"effort": "medium"}
    fmt = payload["text"]["format"]
    assert (fmt["type"], fmt["strict"], fmt["name"]) == ("json_schema", True, "threads_proposals")
    item = fmt["schema"]["properties"]["proposals"]["items"]
    assert fmt["schema"]["additionalProperties"] is False and item["additionalProperties"] is False
    assert item["required"] == ["angle", "conversation_hook", "link_mode", "body"]
    # T6.3.1a: 計画の link_mode だけを許す (モデルに変えさせない)
    assert len(item["properties"]["link_mode"]["enum"]) == 1
    assert item["properties"]["link_mode"]["enum"][0] in ("none", "article")
    assert payload["store"] is False
    assert fake.requests[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert str(fake.requests[0].url) == "https://api.openai.com/v1/responses"
    # manual と同じ prompt (同じ依頼のファイル) を送っている
    done = sorted((tmp_path / "gen" / "done").glob("*.prompt.txt"))
    assert len(done) == 3
    sent = {p["input"][0]["content"] for p in map(json.loads, (r.content for r in fake.requests))}
    assert sent == {p.read_text(encoding="utf-8") for p in done}
    _no_human_decisions(session)
    record = json.loads(next((tmp_path / "gen" / "done").glob("*.openai.json")).read_text("utf-8"))
    assert record["result"] == "generated" and record["usage"] == {
        "input_tokens": 1200, "output_tokens": 180, "total_tokens": 1380}  # fmt: skip
    assert API_KEY not in _all_files(tmp_path)  # 鍵はどのファイルにも残らない
    generation = outcome["status"]["generation"]
    assert (generation["provider"], generation["mode"], generation["model"]) == (
        "openai", "automatic", "gpt-5.6-luna")  # fmt: skip
    assert generation["last_generation_result"] == "generated"


def test_the_provider_cannot_publish_approve_or_send_digests() -> None:
    for name in ("publish", "approve", "reject", "send_digest", "flush", "notify"):
        assert not hasattr(OpenAIGenerationProvider, name)
    from pathlib import Path

    source = Path("app/services/threads_openai_provider.py").read_text(encoding="utf-8")
    for forbidden in ("ThreadsClient", "publish_threads", "digest_service", "ApprovalService",
                      "threads_auto_publisher", "ThreadsPublication", "session"):  # fmt: skip
        assert forbidden not in source


# == retries / failures ==============================================================
def test_transient_failures_are_retried_at_most_twice_then_fall_back_to_manual(
    session, articles, tmp_path
) -> None:
    sleeps: list[float] = []
    fake = FakeLuna(["timeout", 429, 503] + ["timeout"] * 20)
    provider = _provider(tmp_path, fake, sleeps)
    outcome = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert outcome["created"] == []
    # 依頼ごとに 1 + 最大 2 回。3 件の依頼で 9 回を超えない
    assert fake.calls == 3 * (1 + MAX_TRANSPORT_RETRIES)
    assert sleeps and all(0 < s <= 30 for s in sleeps)
    pending = list((tmp_path / "gen" / "pending").glob("*.request.json"))
    assert len(pending) == 3  # manual の依頼として残る
    records = [
        json.loads(p.read_text("utf-8"))
        for p in (tmp_path / "gen" / "pending").glob("*.openai.json")
    ]
    assert {r["fallback"] for r in records} == {"manual"}
    assert {r["result"] for r in records} <= {"failed:timeout", "failed:rate_limited",
                                               "failed:server_error"}  # fmt: skip
    # 次の保守: 答え待ちの依頼があるので、呼ばない (同じ依頼を 2 度送らない)
    again = _service(session, tmp_path, provider=provider).maintain(
        now=_NOW + timedelta(hours=6), execute=True
    )
    assert fake.calls == 9 and again["requests_created"] == []
    _no_human_decisions(session)


def test_429_then_success_and_non_retryable_errors(session, articles, tmp_path) -> None:
    fake = FakeLuna([httpx.Response(429, headers={"retry-after": "3"}), "ok"])
    sleeps: list[float] = []
    provider = _provider(tmp_path, fake, sleeps)
    request = next(
        iter(_service(session, tmp_path, provider=provider).plan(now=_NOW)["stock"]["requests"])
    )
    assert request  # 計画はある
    out = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert len(out["created"]) == 3 and sleeps[0] == 3.0
    bad = FakeLuna([400] * 5)
    client = OpenAIResponsesClient(api_key=API_KEY, transport=httpx.MockTransport(bad.handler),
                                   sleep=lambda s: None)  # fmt: skip
    with pytest.raises(Exception) as err:
        client.generate("p", angles=("insight",))
    assert bad.calls == 1 and err.value.category == "bad_request"  # 400 は再試行しない
    auth = FakeLuna([401])
    client = OpenAIResponsesClient(api_key=API_KEY, transport=httpx.MockTransport(auth.handler),
                                   sleep=lambda s: None)  # fmt: skip
    with pytest.raises(Exception) as err:
        client.generate("p", angles=("insight",))
    assert auth.calls == 1 and err.value.category == "auth" and API_KEY not in str(err.value)


def test_malformed_output_is_not_retried_and_falls_back(session, articles, tmp_path) -> None:
    fake = FakeLuna(["malformed"] * 10)
    provider = _provider(tmp_path, fake)
    out = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert out["created"] == [] and fake.calls == 3  # 依頼ごとに 1 回だけ
    assert len(list((tmp_path / "gen" / "pending").glob("*.request.json"))) == 3


# == validation / repair =============================================================
def test_a_too_long_post_is_repaired_once(session, articles, tmp_path) -> None:
    long = "長い。" * 200
    fake = FakeLuna([long, "ok", "ok", "ok"])
    provider = _provider(tmp_path, fake)
    out = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert len(out["created"]) == 3 and fake.calls == 4
    repair = fake.payload(1)
    assert [m["role"] for m in repair["input"]] == ["user", "assistant", "user"]
    assert "検査" in repair["input"][2]["content"]
    assert all(len(p.content_text) <= 500 for p in _proposals(session))
    _no_human_decisions(session)


def test_repair_happens_at_most_once_then_manual_fallback(session, articles, tmp_path) -> None:
    fake = FakeLuna(body_for=lambda aid, angle, n: "長い。" * 200)
    provider = _provider(tmp_path, fake)
    out = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert out["created"] == [] and fake.calls == 6  # 3 件 × (最初 + 書き直し 1)
    pending = tmp_path / "gen" / "pending"
    assert len(list(pending.glob("*.request.json"))) == 3  # 人が答えられる形で残る
    assert len(list(pending.glob("*.openai-rejected.json"))) == 3
    assert not list(pending.glob("*.response.json"))  # 検査落ちの答えを取り込み直さない
    later = _service(session, tmp_path, provider=provider).maintain(
        now=_NOW + timedelta(hours=7), execute=True
    )
    assert fake.calls == 6 and later["created"] == []  # 書き直しの繰り返しは無い


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("記事の要点。Proプランは$99で、体制を先に決めるほうが早い。", "number not found"),
        ("実際に使ってみたら体制を先に決めるほうが早かった。", "first-person experience"),
        ("この書き方で収益が伸びた。体制を先に決めるほうが早い。", "revenue"),
    ],
)
def test_unsupported_facts_are_rejected_before_saving(
    session, articles, tmp_path, bad, reason
) -> None:
    fake = FakeLuna(body_for=lambda aid, angle, n: bad)
    provider = _provider(tmp_path, fake)
    out = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert out["created"] == []
    assert any(reason in f["reason"] for f in out["failures"]), out["failures"]


def test_duplicates_and_wrong_angles_are_rejected(session, articles, tmp_path) -> None:
    same = "体制を先に決めるほうが早い。迷ったら範囲を小さくする。"
    fake = FakeLuna(body_for=lambda aid, angle, n: same)
    provider = _provider(tmp_path, fake)
    out = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert len(out["created"]) == 1  # 同じ本文は 2 本目から保存しない

    wrong = FakeLuna()

    def wrong_angle(request):
        response = wrong.handler(request)
        data = response.json()
        text = json.loads(data["output"][0]["content"][0]["text"])
        text["proposals"][0]["angle"] = "not-an-angle"
        data["output"][0]["content"][0]["text"] = json.dumps(text, ensure_ascii=False)
        return httpx.Response(200, json=data)

    client = OpenAIResponsesClient(api_key=API_KEY, transport=httpx.MockTransport(wrong_angle),
                                   sleep=lambda s: None)  # fmt: skip
    other = OpenAIGenerationProvider(tmp_path / "gen2", client=client)
    out = _service(session, tmp_path, provider=other).maintain(
        now=_NOW + timedelta(hours=1), execute=True
    )
    assert out["created"] == [] and wrong.calls <= 2 * 3


# == idempotency / cost ==============================================================
def test_the_same_request_is_never_sent_twice(session, articles, tmp_path) -> None:
    fake = FakeLuna(["timeout"] * 30)
    provider = _provider(tmp_path, fake)
    service = _service(session, tmp_path, provider=provider)
    service.maintain(now=_NOW, execute=True)
    first = fake.calls
    request = provider.pending()[0]
    assert provider.submit(request) is None and provider.generate_pending(request) is None
    assert fake.calls == first  # 記録があれば送らない (再起動をまたいでも)
    restarted = _provider(tmp_path, fake)
    assert restarted.generate_pending(restarted.pending()[0]) is None and fake.calls == first


def test_no_call_when_stock_is_sufficient(session, articles, tmp_path) -> None:
    fake = FakeLuna()
    provider = _provider(tmp_path, fake)
    _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert fake.calls == 3
    out = _service(session, tmp_path, provider=provider).maintain(
        now=_NOW + timedelta(hours=6), execute=True
    )
    assert fake.calls == 3 and out["requests_created"] == []  # 下限 3 を満たしている


def test_two_days_of_worker_cycles_call_the_api_only_for_one_bounded_batch(
    session, articles, tmp_path
) -> None:
    from app.services.threads_worker_service import ThreadsWorkerService

    fake = FakeLuna()
    clock = {"now": _NOW}
    service = ThreadsWorkerService(
        _factory(session), settings=_Settings(), threads_service=_ReadOnlyThreads(),
        stock_provider=_provider(tmp_path, fake), alert_notifiers=[],
        maintain_proposal_stock=True,
    )  # fmt: skip
    worker = service.build_worker(
        now=_NOW,
        clock=lambda: clock["now"],
        sleep=lambda s: clock.__setitem__("now", clock["now"] + timedelta(seconds=s)),
    )
    worker.run(max_cycles=600)  # heartbeat は何百回も回るが、API は在庫が足りないときだけ
    assert fake.calls == 3
    _no_human_decisions(session)


def test_a_generation_failure_never_stops_the_worker(session, articles, tmp_path) -> None:
    from app.services.threads_worker_service import ThreadsWorkerService

    class Exploding(OpenAIResponsesClient):
        def generate(self, *args, **kwargs):
            raise RuntimeError(f"boom {API_KEY}")

    provider = OpenAIGenerationProvider(
        tmp_path / "gen", client=Exploding(api_key=API_KEY, sleep=lambda s: None)
    )
    service = ThreadsWorkerService(
        _factory(session), settings=_Settings(), threads_service=_ReadOnlyThreads(),
        stock_provider=provider, alert_notifiers=[], maintain_proposal_stock=True,
    )  # fmt: skip
    worker = service.build_worker(now=_NOW, clock=lambda: _NOW)
    worker.run_cycle()
    worker.run_cycle()
    assert len(list((tmp_path / "gen" / "pending").glob("*.request.json"))) == 3
    assert API_KEY not in _all_files(tmp_path)
    assert _proposals(session) == []


# == configuration / fallback ========================================================
class _Cfg(_Settings):
    threads_generation_provider = "openai"
    threads_generation_model = "gpt-5.6-luna"
    threads_generation_reasoning_effort = "medium"
    threads_generation_timeout_seconds = 60.0
    openai_api_base_url = "https://api.openai.com/v1"
    openai_api_key = None


def test_a_missing_api_key_keeps_the_worker_on_manual_requests(session, articles, tmp_path) -> None:
    provider = build_openai_provider(_Cfg(), tmp_path / "gen")
    assert provider.mode == "misconfigured"
    assert "OPENAI_API_KEY is not set" in provider.availability().reason
    out = _service(session, tmp_path, provider=provider).maintain(now=_NOW, execute=True)
    assert out["created"] == [] and len(out["requests_created"]) == 3
    assert out["status"]["generation"]["mode"] == "misconfigured"
    # 人が答えれば、manual と同じように取り込まれる (フォールバック)
    for request in provider.pending():
        (tmp_path / "gen" / "pending" / f"{request.request_id}.response.json").write_text(
            json.dumps({"proposals": [{"angle": request.angles[0],
                        "link_mode": request.link_mode,
                        "conversation_hook": request.conversation_hook,
                        "body": f"記事{request.article_id}の答え。体制を先に決める。"
                        + ("\n{link}" if request.link_mode == "article" else "")}]},
                       ensure_ascii=False), encoding="utf-8")  # fmt: skip
    later = _service(session, tmp_path, provider=provider).maintain(
        now=_NOW + timedelta(hours=1), execute=True
    )
    assert len(later["created"]) == 3
    _no_human_decisions(session)


def test_provider_selection_and_rollback_to_manual(tmp_path) -> None:
    from app.social.threads.policy import get_operations_policy

    policy = get_operations_policy()
    assert isinstance(build_provider(policy, _Settings()), ManualFileProvider)  # 既定
    cfg = _Cfg()
    assert isinstance(build_provider(policy, cfg), OpenAIGenerationProvider)
    cfg.threads_generation_provider = "manual"  # 1 行で戻す
    assert isinstance(build_provider(policy, cfg), ManualFileProvider)


def test_explicit_generation_of_an_existing_pending_request(session, articles, tmp_path) -> None:
    manual = ManualFileProvider(tmp_path / "gen")
    _service(session, tmp_path, provider=manual).maintain(now=_NOW, execute=True)
    rid = manual.pending()[0].request_id
    fake = FakeLuna()
    provider = _provider(tmp_path, fake)
    # 保守だけでは、既にある manual の依頼を API に送らない
    _service(session, tmp_path, provider=provider).maintain(
        now=_NOW + timedelta(minutes=5), execute=True
    )
    assert fake.calls == 0
    out = _service(session, tmp_path, provider=provider).generate_pending(rid, now=_NOW)
    assert fake.calls == 1 and len(out["created"]) == 1
    again = _service(session, tmp_path, provider=provider).generate_pending(rid, now=_NOW)
    assert fake.calls == 1 and again["failures"]  # もう待ちに無い / 2 度送らない
    _no_human_decisions(session)


def test_usage_metadata_is_sanitized() -> None:
    usage = {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12, "secret": API_KEY,
             "output_tokens_details": {"reasoning_tokens": 3}}  # fmt: skip
    assert sanitize_usage(usage) == {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}
    assert sanitize_usage(None) == {}
