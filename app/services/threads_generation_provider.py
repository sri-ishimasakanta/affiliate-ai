"""Threads 投稿案の本文を作る provider の境界 (T6)。

このリポジトリの生成は **人が外部で動かす** のが既定である
(:mod:`app.services.draft_generation_adapters` の ManualAdapter と同じ方針。
追加の実費 0 円、secret を増やさない)。T6 はその前提のまま、在庫の保守を
provider の境界の外側で自動化する。

provider がすること: 生成の依頼 (prompt と来歴) を受け取り、既存の検査が読む JSON
(``{"proposals": [...]}``) を返す。**承認も公開もしない。** 返った JSON は必ず
``ThreadsProposalService`` の検査を通り、``awaiting_approval`` として保存される。

- :class:`ManualFileProvider` (既定): 依頼をファイルに書き、人がその prompt を外部で
  実行して ``<id>.response.json`` を置く。次の保守のサイクルがそれを取り込む。
- :class:`~app.services.threads_openai_provider.OpenAIGenerationProvider` (T6.2): OpenAI の
  Responses API で自動生成する。manual と同じ依頼・prompt・答えのファイルを使い、失敗は
  manual の依頼として残す。環境変数 ``THREADS_GENERATION_PROVIDER=openai`` と
  ``OPENAI_API_KEY`` で選ぶ (人が API の費用と鍵を用意する)。
- :class:`DisabledAutomatedProvider`: 何も呼ばない provider (``disabled`` / 不明な名前)。

ファイルの置き場 (既定 ``data/threads-generation``、git 管理外)::

    pending/<id>.request.json   依頼の来歴 (記事・切り口・as_of・参考の指紋・prompt hash)
    pending/<id>.prompt.txt     外部で実行する prompt
    pending/<id>.response.json  人が置く生成結果 (この名前で置く)
    done/<id>.*                 取り込み済み (outcome.json に作った提案の ID)
    failed/<id>.*               取り込めなかった (outcome.json に理由)。自動では再試行しない
    status.json                 最後の保守の要約 (T7 が読む)

PLAN (読むだけ) では、ディレクトリを作らない・何も書かない。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

PROVIDER_MANUAL = "manual"
PROVIDER_DISABLED = "disabled"

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class GenerationRequest:
    request_id: str
    article_id: int
    article_title: str
    angles: tuple[str, ...]
    link_mode: str
    learning_as_of: str
    guidance_fingerprint: str
    guidance_mode: str
    prompt_hash: str
    created_at: str
    reasons: tuple[str, ...] = ()
    prompt: str = field(default="", repr=False)
    #: T6.3: 求める会話のきっかけ。T6.3 より前の依頼は ``None`` (きっかけ無しの形のまま)。
    conversation_hook: str | None = None
    #: T6.5: prompt を作った時点の成績の参考 (``performance_analysis.freeze_for_request``)。
    #: 使っていなければ ``{"used_in_generation": false}``。この項目より前の依頼は ``None``。
    performance_feedback: dict | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict:
        extra = {"conversation_hook": self.conversation_hook} if self.conversation_hook else {}
        if self.performance_feedback is not None:
            extra["performance_feedback"] = self.performance_feedback
        return {
            "request_id": self.request_id,
            "article_id": self.article_id,
            "article_title": self.article_title,
            "angles": list(self.angles),
            "link_mode": self.link_mode,
            "learning_as_of": self.learning_as_of,
            "guidance_fingerprint": self.guidance_fingerprint,
            "guidance_mode": self.guidance_mode,
            "prompt_hash": self.prompt_hash,
            "created_at": self.created_at,
            "reasons": list(self.reasons),
            **extra,
        }

    @classmethod
    def from_dict(cls, data: dict, prompt: str = "") -> GenerationRequest:
        return cls(
            request_id=str(data["request_id"]),
            article_id=int(data["article_id"]),
            article_title=str(data.get("article_title", "")),
            angles=tuple(data.get("angles", ())),
            link_mode=str(data.get("link_mode", "none")),
            learning_as_of=str(data["learning_as_of"]),
            guidance_fingerprint=str(data["guidance_fingerprint"]),
            guidance_mode=str(data.get("guidance_mode", "")),
            prompt_hash=str(data.get("prompt_hash", "")),
            created_at=str(data["created_at"]),
            reasons=tuple(data.get("reasons", ())),
            prompt=prompt,
            conversation_hook=data.get("conversation_hook"),
            performance_feedback=(data.get("performance_feedback")
                                  if isinstance(data.get("performance_feedback"), dict)
                                  else None),  # fmt: skip
        )


@dataclass(frozen=True)
class ProviderAvailability:
    name: str
    available: bool
    synchronous: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "synchronous": self.synchronous,
            "reason": self.reason,
        }


class ThreadsProposalGenerationProvider(Protocol):
    """生成の依頼 → 検査前の JSON。承認・公開・DB には触れない。"""

    name: str

    def availability(self) -> ProviderAvailability: ...

    def submit(self, request: GenerationRequest) -> str | None:
        """依頼を出す。同期の provider は出力を返し、非同期は ``None`` (後で collect)。"""
        ...

    def pending(self) -> list[GenerationRequest]:
        """答えを待っている依頼 (古い順)。"""
        ...

    def collect(self, request: GenerationRequest) -> str | None:
        """その依頼の出力。まだ無ければ ``None``。"""
        ...

    def complete(self, request: GenerationRequest, *, ok: bool, outcome: dict) -> None:
        """取り込んだ (ok) か、取り込めなかったかを記録して、待ちから外す。"""
        ...

    def write_status(self, status: dict) -> None: ...

    def read_status(self) -> dict | None: ...


class ManualFileProvider:
    """人が外部で prompt を実行する provider (既定)。ファイルだけでやり取りする。"""

    name = PROVIDER_MANUAL

    def __init__(self, directory: Path | str) -> None:
        self._root = Path(directory)

    @property
    def directory(self) -> Path:
        return self._root

    def availability(self) -> ProviderAvailability:
        if self._root.exists() and not os.access(self._root, os.W_OK):
            return ProviderAvailability(
                self.name, False, False, f"the request directory is not writable: {self._root}"
            )
        return ProviderAvailability(
            self.name,
            True,
            False,
            "asynchronous: a person runs pending/<id>.prompt.txt and saves "
            "pending/<id>.response.json; the next maintenance cycle ingests it",
        )

    def submit(self, request: GenerationRequest) -> str | None:
        pending = self._dir("pending")
        base = pending / request.request_id
        request_path = base.with_suffix(".request.json")
        if request_path.exists():
            return None  # 同じ依頼を二重に出さない (決定的な ID)
        (pending / f"{request.request_id}.prompt.txt").write_text(request.prompt, encoding="utf-8")
        request_path.write_text(_dump(request.as_dict()), encoding="utf-8")
        return None

    def pending(self) -> list[GenerationRequest]:
        folder = self._root / "pending"
        if not folder.is_dir():
            return []
        requests = []
        for path in sorted(folder.glob("*.request.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            prompt_path = folder / f"{data['request_id']}.prompt.txt"
            prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
            requests.append(GenerationRequest.from_dict(data, prompt))
        return sorted(requests, key=lambda r: (r.created_at, r.request_id))

    def collect(self, request: GenerationRequest) -> str | None:
        path = self._root / "pending" / f"{request.request_id}.response.json"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def complete(self, request: GenerationRequest, *, ok: bool, outcome: dict) -> None:
        target = self._dir("done" if ok else "failed")
        for suffix in (".request.json", ".prompt.txt", ".response.json"):
            source = self._root / "pending" / f"{request.request_id}{suffix}"
            if source.exists():
                os.replace(source, target / source.name)
        (target / f"{request.request_id}.outcome.json").write_text(_dump(outcome), encoding="utf-8")

    def write_status(self, status: dict) -> None:
        self._dir("").joinpath("status.json").write_text(_dump(status), encoding="utf-8")

    def read_status(self) -> dict | None:
        path = self._root / "status.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _dir(self, name: str) -> Path:
        folder = self._root / name if name else self._root
        folder.mkdir(parents=True, exist_ok=True)
        return folder


class DisabledAutomatedProvider:
    """自動生成の provider の場所。**何も呼ばない。** 依頼も作らない。"""

    name = PROVIDER_DISABLED

    def __init__(self, reason: str | None = None) -> None:
        self._reason = reason or (
            "no approved automated LLM provider is configured (repository policy: generation "
            "runs outside the system at zero added cost); use provider 'manual'"
        )

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(self.name, False, True, self._reason)

    def submit(self, request: GenerationRequest) -> str | None:
        raise RuntimeError(self._reason)

    def pending(self) -> list[GenerationRequest]:
        return []

    def collect(self, request: GenerationRequest) -> str | None:
        return None

    def complete(self, request: GenerationRequest, *, ok: bool, outcome: dict) -> None:
        return None

    def write_status(self, status: dict) -> None:
        return None

    def read_status(self) -> dict | None:
        return None


def provider_name(policy, settings=None) -> str:
    """使う provider の名前。環境変数 ``THREADS_GENERATION_PROVIDER`` (設定) が優先、
    無ければポリシーの ``proposal_stock.provider`` (コミット済みの既定は ``manual``)。"""

    override = getattr(settings, "threads_generation_provider", None) if settings else None
    return str(override or policy.proposal_stock("provider", PROVIDER_MANUAL)).strip().lower()


def build_provider(policy, settings=None) -> ThreadsProposalGenerationProvider:
    """provider を作る。鍵は設定 (環境変数) からだけ読み、記録に残さない。"""

    name = provider_name(policy, settings)
    directory = Path(str(policy.proposal_stock("request_directory", "data/threads-generation")))
    if not directory.is_absolute():
        directory = _REPO_ROOT / directory
    if name == PROVIDER_MANUAL:
        return ManualFileProvider(directory)
    if name == "openai":
        from app.services.threads_openai_provider import build_openai_provider

        return build_openai_provider(settings, directory)
    return DisabledAutomatedProvider(
        None if name == PROVIDER_DISABLED else f"unknown provider {name!r}; nothing is generated"
    )


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


__all__ = [
    "DisabledAutomatedProvider",
    "GenerationRequest",
    "ManualFileProvider",
    "ProviderAvailability",
    "ThreadsProposalGenerationProvider",
    "build_provider",
]
