"""Threads 投稿案の自動生成 provider (OpenAI Responses API、T6.2)。

在庫の保守の依頼 (``GenerationRequest``) を、manual と **同じ prompt** のまま OpenAI の
Responses API (``POST /v1/responses``) へ送り、Structured Outputs (strict な JSON schema) で
``{"proposals": [...]}`` を受け取る。

- **モデルの出力は信用しない入力である。** 返った JSON は manual の答えと同じ
  ``pending/<id>.response.json`` に置かれ、在庫の保守がいつもの検査 (T2 / T5 / T5.5) と
  自動生成用の事実の検査 (:mod:`app.social.threads.fact_guard`) を通したものだけを
  ``awaiting_approval`` で保存する。**承認も公開も digest の送信もしない。**
- manual の依頼のファイル (``pending/<id>.request.json`` / ``.prompt.txt``) は同じように
  書く (:class:`ManualFileProvider` をそのまま使う)。自動生成が使えないとき・失敗したとき・
  出力が検査を通らなかったときは、その依頼が **普通の manual の依頼として残る** (人が
  ``response.json`` を置けば、次の保守が取り込む)。
- 依頼ごとの記録 ``pending/<id>.openai.json`` (呼び出しの回数・結果の分類・usage・モデル)。
  API の呼び出しの **前** に書くので、同じ依頼を 2 度送らない (再起動をまたいでも)。
  鍵・認証ヘッダーは記録しない。

費用の上限 (依頼ごと): 最初の呼び出し 1 回 + 通信の失敗 (timeout / 接続 / 429 / 5xx) の
再試行 2 回まで + 検査落ちの書き直し 1 回 (その書き直しも通信の再試行は 2 回まで)。
依頼そのものは在庫が下限より少ないときだけ作られる (1 回の保守で最大 3 件・答え待ちの
依頼があれば出さない)。heartbeat では呼ばない。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.services.threads_generation_provider import (
    GenerationRequest,
    ManualFileProvider,
    ProviderAvailability,
)
from app.social.threads.errors import redact
from app.social.threads.quality import reason_ids

PROVIDER_OPENAI = "openai"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
MAX_TRANSPORT_RETRIES = 2
MAX_REPAIRS = 1
BACKOFF_SECONDS = (2.0, 4.0)
MAX_BACKOFF_SECONDS = 30.0
MAX_OUTPUT_TOKENS = 4000
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
STATUS_CATEGORIES = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    404: "not_found",
    429: "rate_limited",
}
RECORD_SUFFIX = ".openai.json"
REJECTED_SUFFIX = ".openai-rejected.json"
REPAIR_INSTRUCTION = (
    "前の出力は、次の検査を通らなかった。同じ規則 (事実の境界・文体・切り口・リンク) の"
    "まま、問題だけを直して、同じ JSON の形で 1 本書き直す。検査の結果: "
)


class GenerationError(Exception):
    def __init__(self, category: str, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status = status


@dataclass
class GenerationResult:
    text: str
    model: str | None
    response_id: str | None
    usage: dict
    attempts: list[dict] = field(default_factory=list)


def proposal_schema(
    angles: tuple[str, ...] | list[str],
    conversation_hook: str | None = None,
    link_mode: str | None = None,
):
    """Structured Outputs (strict) の JSON schema。manual の答えと同じ形。

    ``conversation_hook`` (T6.3) を求める依頼では、その値だけを許す enum の項目を足す
    (モデルに別の型を選ばせない)。求めない依頼 (T6.3 より前) は前と同じ schema。
    """

    item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["angle", "link_mode", "body"],
        "properties": {
            "angle": {"type": "string", "enum": sorted(set(angles))},
            "link_mode": {"type": "string", "enum": ["none", "article"]},
            "body": {"type": "string"},
        },
    }
    if link_mode in ("none", "article"):
        # T6.3.1a: 計画の link_mode はモデルに変えさせない (その値だけの enum)。
        item["properties"]["link_mode"] = {"type": "string", "enum": [link_mode]}
    if conversation_hook is not None:
        item["required"] = ["angle", "conversation_hook", "link_mode", "body"]
        item["properties"] = {
            "angle": item["properties"]["angle"],
            "conversation_hook": {"type": "string", "enum": [conversation_hook]},
            "link_mode": item["properties"]["link_mode"],
            "body": item["properties"]["body"],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["proposals"],
        "properties": {"proposals": {"type": "array", "items": item}},
    }


def sanitize_usage(usage) -> dict:
    """usage は数だけを残す (ほかの値・識別子は捨てる)。"""

    if not isinstance(usage, dict):
        return {}
    return {
        key: usage[key]
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if isinstance(usage.get(key), int)
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class OpenAIResponsesClient:
    """OpenAI Responses API への最小の呼び出し (httpx。鍵はヘッダーにだけ置く)。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required")
        self._key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._url = base_url.rstrip("/") + "/responses"
        self._timeout = timeout
        self._transport = transport
        self._sleep = sleep
        self._max_output_tokens = max_output_tokens

    def body(
        self,
        prompt: str,
        *,
        angles,
        feedback: tuple[str, str] | None = None,
        conversation_hook: str | None = None,
        link_mode: str | None = None,
    ) -> dict:
        messages = [{"role": "user", "content": prompt}]
        if feedback is not None:
            previous, problems = feedback
            messages += [
                {"role": "assistant", "content": previous},
                {"role": "user", "content": REPAIR_INSTRUCTION + problems},
            ]
        return {
            "model": self.model,
            "input": messages,
            "reasoning": {"effort": self.reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "threads_proposals",
                    "strict": True,
                    "schema": proposal_schema(angles, conversation_hook, link_mode),
                }
            },
            "max_output_tokens": self._max_output_tokens,
            "store": False,
        }

    def generate(
        self, prompt: str, *, angles, feedback=None, conversation_hook=None, link_mode=None
    ) -> GenerationResult:
        body = self.body(
            prompt,
            angles=angles,
            feedback=feedback,
            conversation_hook=conversation_hook,
            link_mode=link_mode,
        )
        attempts: list[dict] = []
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            for attempt in range(1 + MAX_TRANSPORT_RETRIES):
                try:
                    try:
                        response = client.post(self._url, json=body, headers=headers)
                    except httpx.TimeoutException as exc:
                        raise GenerationError("timeout", "timed out", retryable=True) from exc
                    except httpx.TransportError as exc:
                        raise GenerationError(
                            "network", type(exc).__name__, retryable=True
                        ) from exc
                    result = self._parse(response)
                except GenerationError as exc:
                    attempts.append(
                        {"attempt": attempt + 1, "outcome": "error", "category": exc.category,
                         "status": exc.status}
                    )  # fmt: skip
                    if not exc.retryable or attempt == MAX_TRANSPORT_RETRIES:
                        exc.attempts = attempts  # type: ignore[attr-defined]
                        raise
                    self._sleep(self._backoff(attempt, getattr(exc, "retry_after", None)))
                    continue
                attempts.append({"attempt": attempt + 1, "outcome": "ok", "status": 200})
                result.attempts = attempts
                return result
        raise GenerationError("exhausted", "retries exhausted", retryable=False)  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        base = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
        return min(MAX_BACKOFF_SECONDS, max(base, retry_after or 0.0))

    def _parse(self, response: httpx.Response) -> GenerationResult:
        status = response.status_code
        if status != 200:
            fallback = "server_error" if status >= 500 else "http"
            category = STATUS_CATEGORIES.get(status, fallback)
            error = GenerationError(
                category, f"HTTP {status}", retryable=status in RETRYABLE_STATUS, status=status
            )
            retry_after = response.headers.get("retry-after")
            error.retry_after = float(retry_after) if (retry_after or "").isdigit() else None  # type: ignore[attr-defined]
            raise error
        try:
            data = response.json()
        except ValueError as exc:
            raise GenerationError("malformed", "response is not JSON", retryable=False) from exc
        if data.get("status") not in (None, "completed"):
            raise GenerationError("incomplete", f"status {data.get('status')}", retryable=False)
        texts, refusal = [], False
        for item in data.get("output") or []:
            if item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if content.get("type") == "output_text":
                    texts.append(content.get("text") or "")
                elif content.get("type") == "refusal":
                    refusal = True
        if refusal and not texts:
            raise GenerationError("refusal", "the model refused", retryable=False)
        text = "".join(texts)
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise GenerationError("malformed", "output is not JSON", retryable=False) from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("proposals"), list):
            raise GenerationError("malformed", "output has no proposals list", retryable=False)
        return GenerationResult(
            text=text,
            model=data.get("model"),
            response_id=data.get("id"),
            usage=sanitize_usage(data.get("usage")),
        )


class OpenAIGenerationProvider:
    """自動生成の provider。manual のファイルの形をそのまま使い、失敗は manual へ戻す。"""

    name = PROVIDER_OPENAI
    automatic = True

    def __init__(
        self,
        directory: Path | str,
        *,
        client: OpenAIResponsesClient | None,
        model: str = DEFAULT_MODEL,
        misconfigured_reason: str | None = None,
    ) -> None:
        self._manual = ManualFileProvider(directory)
        self._client = client
        self.model = client.model if client else model
        self._misconfigured = None if client else (misconfigured_reason or "no API client")

    @property
    def directory(self) -> Path:
        return self._manual.directory

    @property
    def mode(self) -> str:
        return "misconfigured" if self._misconfigured else "automatic"

    def availability(self) -> ProviderAvailability:
        if self._misconfigured:
            return ProviderAvailability(
                self.name,
                True,
                False,
                f"misconfigured ({self._misconfigured}); requests fall back to manual answers "
                "(pending/<id>.response.json)",
            )
        return ProviderAvailability(
            self.name,
            True,
            True,
            f"automatic: {self.model} via the OpenAI Responses API (Structured Outputs); the "
            "output is validated before saving and still needs human approval",
        )

    # -- the provider boundary ---------------------------------------------------------
    def submit(self, request: GenerationRequest) -> str | None:
        self._manual.submit(request)  # 同じ依頼と prompt を manual の形で残す (代わりの経路)
        return self._generate(request)

    def repair(self, request: GenerationRequest, previous: str, problems: str) -> str | None:
        """検査落ちの出力を 1 回だけ書き直させる。上限を超えたら ``None``。"""

        return self._generate(request, feedback=(previous, problems))

    def generate_pending(self, request: GenerationRequest) -> str | None:
        """既にある manual の依頼を明示で 1 回送る (人の指示のときだけ。冪等)。"""

        return self._generate(request)

    def pending(self) -> list[GenerationRequest]:
        return self._manual.pending()

    def collect(self, request: GenerationRequest) -> str | None:
        return self._manual.collect(request)

    def complete(self, request: GenerationRequest, *, ok: bool, outcome: dict) -> None:
        rid = request.request_id
        pending = self.directory / "pending"
        record = self._read_record(rid)
        response = pending / f"{rid}.response.json"
        if not ok and record.get("response_source") == PROVIDER_OPENAI and response.exists():
            # 自動の答えが検査を通らなかった: 答えだけを外し、依頼は manual として残す。
            os.replace(response, pending / f"{rid}{REJECTED_SUFFIX}")
            record.update(
                result="rejected_by_validation",
                fallback="manual",
                response_source=None,
                validation=redact(str(outcome.get("reason", "")))[:500],
                updated_at=_now(),
            )
            self._write_record(rid, record)
            return
        self._manual.complete(request, ok=ok, outcome=outcome)
        target = self.directory / ("done" if ok else "failed")
        for suffix in (RECORD_SUFFIX, REJECTED_SUFFIX):
            source = pending / f"{rid}{suffix}"
            if source.exists():
                os.replace(source, target / source.name)

    def write_status(self, status: dict) -> None:
        self._manual.write_status(status)

    def read_status(self) -> dict | None:
        return self._manual.read_status()

    def generation_summary(self) -> dict:
        """最後の自動生成 (記録の中で一番新しいもの)。鍵・本文は含めない。"""

        latest = None
        for folder in ("pending", "done", "failed"):
            for path in (self.directory / folder).glob(f"*{RECORD_SUFFIX}"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                if latest is None or data.get("updated_at", "") > latest.get("updated_at", ""):
                    latest = data
        return {
            "provider": self.name,
            "mode": self.mode,
            "model": self.model,
            "last_generation_at": (latest or {}).get("updated_at"),
            "last_generation_result": (latest or {}).get("result"),
            "last_usage": (latest or {}).get("usage"),
        }

    # -- internals ---------------------------------------------------------------------
    def _generate(self, request: GenerationRequest, *, feedback=None) -> str | None:
        rid = request.request_id
        record = self._read_record(rid)
        if self._misconfigured:
            if not record:
                self._write_record(rid, {"request_id": rid, "model": self.model,
                                         "result": "misconfigured", "fallback": "manual",
                                         "reason": self._misconfigured, "calls": 0,
                                         "updated_at": _now()})  # fmt: skip
            return None
        if feedback is None and record.get("initial_call_made"):
            return None  # 同じ依頼を 2 度送らない
        if feedback is not None and record.get("repairs", 0) >= MAX_REPAIRS:
            return None
        purpose = "repair" if feedback is not None else "initial"
        history = list(record.get("history") or [])
        record = {
            "request_id": rid,
            "model": self.model,
            "calls": record.get("calls", 0),
            "repairs": record.get("repairs", 0) + (1 if feedback is not None else 0),
            **{k: v for k, v in record.items() if k not in ("calls", "repairs")},
            "initial_call_made": True,
            "result": "in_progress",
            "updated_at": _now(),
        }
        self._write_record(rid, record)  # 呼ぶ前に残す (途中で落ちても 2 度送らない)
        call = {
            "ordinal": len(history) + 1,
            "purpose": purpose,
            "at": _now(),
            "requested_model": self.model,
        }
        if feedback is not None:
            call["repair_reason_ids"] = reason_ids(feedback[1])
            call["repair_reasons"] = redact(feedback[1])[:1500]
        link_mode = request.link_mode if getattr(request, "conversation_hook", None) else None
        try:
            result = self._client.generate(
                request.prompt,
                angles=request.angles,
                feedback=feedback,
                conversation_hook=getattr(request, "conversation_hook", None),
                link_mode=link_mode,
            )
        except GenerationError as exc:
            attempts = getattr(exc, "attempts", [])
            call.update(result=f"failed:{exc.category}", http_attempts=attempts, usage={})
            history.append(call)
            record.update(
                calls=record["calls"] + max(1, len(attempts)),
                result=f"failed:{exc.category}",
                fallback="manual",
                attempts=attempts,
                updated_at=_now(),
                **_call_totals(history),
            )
            self._write_record(rid, record)
            return None
        except Exception as exc:  # 生成の故障で worker を止めない
            call.update(result=f"failed:unexpected:{type(exc).__name__}", usage={})
            history.append(call)
            record.update(result=f"failed:unexpected:{type(exc).__name__}", fallback="manual",
                          updated_at=_now(), **_call_totals(history))  # fmt: skip
            self._write_record(rid, record)
            return None
        pending = self.directory / "pending"
        (pending / f"{rid}.response.json").write_text(result.text, encoding="utf-8")
        call.update(
            result="ok",
            returned_model=result.model,
            response_id=result.response_id,
            http_attempts=result.attempts,
            usage=result.usage,
            output=_sanitized_output(result.text),
        )
        history.append(call)
        record.update(
            calls=record["calls"] + len(result.attempts),
            result="generated" if feedback is None else "repaired",
            response_source=PROVIDER_OPENAI,
            response_model=result.model,
            response_id=result.response_id,
            attempts=result.attempts,
            fallback=None,
            updated_at=_now(),
            **_call_totals(history),
        )
        self._write_record(rid, record)
        return result.text

    def record_validation(self, request: GenerationRequest, *, ok: bool, reasons=None,
                          audit=None) -> None:  # fmt: skip
        """最後の呼び出しの検査の結果を、呼び出しの履歴に残す (理由の ID つき。秘密は残さない)。"""

        rid = request.request_id
        record = self._read_record(rid)
        history = list(record.get("history") or [])
        if not history:
            return
        text = redact(str(reasons or ""))[:1500]
        history[-1]["validation"] = {
            "ok": bool(ok),
            "reason_ids": [] if ok else reason_ids(text),
            "reasons": None if ok else text,
            **({"audit": audit} if audit else {}),
        }
        record["history"] = history
        record["updated_at"] = _now()
        self._write_record(rid, record)

    def _record_path(self, rid: str) -> Path:
        pending = self.directory / "pending" / f"{rid}{RECORD_SUFFIX}"
        if pending.exists():
            return pending
        for folder in ("done", "failed"):
            path = self.directory / folder / f"{rid}{RECORD_SUFFIX}"
            if path.exists():
                return path
        return pending

    def _read_record(self, rid: str) -> dict:
        path = self._record_path(rid)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def _write_record(self, rid: str, record: dict) -> None:
        path = self._record_path(rid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sanitized_output(text: str) -> dict | str:
    """モデルの構造化出力を、秘密らしい値を落として残す (本文は監査のために残す)。"""

    try:
        data = json.loads(text)
    except ValueError:
        return redact(text)[:4000]
    items = []
    for item in (data.get("proposals") or []) if isinstance(data, dict) else []:
        if isinstance(item, dict):
            items.append(
                {
                    key: (redact(item[key]) if isinstance(item.get(key), str) else item.get(key))
                    for key in ("angle", "conversation_hook", "link_mode", "body")
                    if key in item
                }
            )
    return {"proposals": items}


def _call_totals(history: list[dict]) -> dict:
    """呼び出しの数え方をはっきり分ける (1 件の依頼 = 生成の試み 1 回)。"""

    usage: dict[str, int] = {}
    for call in history:
        for key, value in (call.get("usage") or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
    return {
        "generation_attempts": 1,
        "model_calls": len(history),
        "repair_calls": sum(1 for c in history if c.get("purpose") == "repair"),
        "http_requests": sum(len(c.get("http_attempts") or []) or 1 for c in history),
        "usage": usage,
        "history": history,
    }


def build_responses_client(settings) -> OpenAIResponsesClient | None:
    """設定から OpenAI の client を作る。鍵が無ければ ``None`` (呼ばない)。"""

    key = getattr(settings, "openai_api_key", None)
    if not key:
        return None
    return OpenAIResponsesClient(
        api_key=key,
        model=getattr(settings, "threads_generation_model", None) or DEFAULT_MODEL,
        reasoning_effort=getattr(settings, "threads_generation_reasoning_effort", None)
        or DEFAULT_REASONING_EFFORT,
        base_url=getattr(settings, "openai_api_base_url", None) or DEFAULT_BASE_URL,
        timeout=float(getattr(settings, "threads_generation_timeout_seconds", 60) or 60),
    )


def build_openai_provider(settings, directory: Path) -> OpenAIGenerationProvider:
    """設定から作る。鍵が無ければ misconfigured (manual の依頼として残す。呼ばない)。"""

    model = getattr(settings, "threads_generation_model", None) or DEFAULT_MODEL
    client = build_responses_client(settings)
    if client is None:
        return OpenAIGenerationProvider(
            directory, client=None, model=model, misconfigured_reason="OPENAI_API_KEY is not set"
        )
    return OpenAIGenerationProvider(directory, client=client, model=model)


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_REASONING_EFFORT",
    "GenerationError",
    "MAX_REPAIRS",
    "MAX_TRANSPORT_RETRIES",
    "OpenAIGenerationProvider",
    "OpenAIResponsesClient",
    "PROVIDER_OPENAI",
    "build_openai_provider",
    "build_responses_client",
    "proposal_schema",
    "sanitize_usage",
]
