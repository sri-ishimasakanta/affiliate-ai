"""Threads の文体ポリシー (T2)。

閾値と禁止語を **コードに埋めない**。``app/config/threads_style_policy.json`` に置き、
``policy_version`` で「どの基準で作った提案か」を後から再現できるようにする
(C6/C7 の policy と同じ方針)。

ここにあるのは運用上の文体の約束であって、良し悪しの点数ではない。不透明な合成
スコアは作らず、名前の付いた検査を個別に通す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any

_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "threads_style_policy.json"


@dataclass(frozen=True)
class ThreadsStylePolicy:
    policy_version: str
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    def gate(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    def listing(self, name: str) -> tuple[str, ...]:
        value = self.raw.get(name)
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    @property
    def max_characters(self) -> int:
        return int(self.gate("limits", "max_characters", 500))

    @property
    def preferred_max_characters(self) -> int:
        return int(self.gate("limits", "preferred_max_characters", 380))

    @property
    def max_sentences(self) -> int:
        return int(self.gate("limits", "max_sentences", 8))

    @property
    def preferred_max_sentence_characters(self) -> int:
        return int(self.gate("limits", "preferred_max_sentence_characters", 60))

    @property
    def angles(self) -> tuple[str, ...]:
        return self.listing("angles")

    @property
    def banned_phrases(self) -> tuple[str, ...]:
        return self.listing("banned_phrases")

    @property
    def discouraged_phrases(self) -> tuple[str, ...]:
        return self.listing("discouraged_phrases")

    @property
    def prohibitions(self) -> tuple[str, ...]:
        return self.listing("prohibitions")

    @property
    def allowed_link_modes(self) -> tuple[str, ...]:
        value = self.gate("link", "allowed_modes", [])
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    @property
    def allowed_link_host(self) -> str:
        return str(self.gate("link", "allowed_host", "bizfluxlab.com"))

    @property
    def forbidden_path_prefixes(self) -> tuple[str, ...]:
        value = self.gate("link", "forbidden_path_prefixes", [])
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    @property
    def max_emoji(self) -> int:
        return int(self.gate("emoji", "max_count", 2))

    @property
    def max_questions(self) -> int:
        return int(self.gate("tone", "max_questions", 1))

    @property
    def polite_markers(self) -> tuple[str, ...]:
        """文末が機械的に見える語尾。「〜です」と「〜ます」の両方を見る。"""

        value = self.gate("polite_form", "markers", ["です", "ます"])
        return tuple(str(v) for v in value) if isinstance(value, list) else ("です", "ます")

    @property
    def max_consecutive_polite_endings(self) -> int:
        return int(self.gate("polite_form", "max_consecutive_sentence_endings", 3))

    @property
    def factual_rules(self) -> tuple[str, ...]:
        value = self.gate("factual_grounding", "rules", [])
        return tuple(str(v) for v in value) if isinstance(value, list) else ()


def load_policy(path: Path | str | None = None) -> ThreadsStylePolicy:
    target = Path(path) if path is not None else _POLICY_PATH
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("threads style policy must be a JSON object")
    version = document.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("threads style policy must declare a policy_version")
    return ThreadsStylePolicy(policy_version=version, raw=document)


@lru_cache
def get_policy() -> ThreadsStylePolicy:
    return load_policy()


@dataclass(frozen=True)
class ThreadsMeasurementPolicy:
    """計測の閾値 (T4)。境界はコードではなく JSON に置く。"""

    policy_version: str
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    @property
    def maturity_hours(self) -> dict[str, float]:
        raw = self.section("maturity_hours")
        return {
            "just_published": float(raw.get("just_published", 1)),
            "early_observation": float(raw.get("early_observation", 24)),
            "initial_sample": float(raw.get("initial_sample", 72)),
        }

    def maturity_note(self, stage: str) -> str:
        return str(self.section("maturity_notes").get(stage, ""))

    @property
    def minimum_mature_posts(self) -> int:
        return int(self.section("comparison").get("minimum_mature_posts_per_dimension", 3))

    @property
    def minimum_views_for_ratio(self) -> int:
        return int(self.section("comparison").get("minimum_views_for_ratio", 30))

    @property
    def length_buckets(self) -> list[dict[str, Any]]:
        value = self.raw.get("length_buckets")
        return list(value) if isinstance(value, list) else []

    @property
    def metric_caveats(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self.section("metric_caveats").items()}

    @property
    def unsupported_metrics(self) -> tuple[str, ...]:
        value = self.raw.get("unsupported_metrics")
        return tuple(str(v) for v in value) if isinstance(value, list) else ()

    @property
    def min_observation_spacing_minutes(self) -> int:
        """C8 と常駐 worker が同じ投稿を続けて取りに行かないための最小間隔 (T4.2)。"""

        return int(self.section("collection").get("min_observation_spacing_minutes", 10))

    # -- T5: learning (成熟の境界と本数・views の下限は上の値をそのまま使う) --------
    @property
    def mature_after_hours(self) -> float:
        """比較してよい成熟の境界。T4 の ``initial_sample`` と同じ値。

        別の成熟の仕組みを作らないため、ここで新しい値を持たない。
        """

        return self.maturity_hours["initial_sample"]

    @property
    def comparable_window_hours(self) -> float:
        section = self.section("learning").get("canonical_snapshot")
        section = section if isinstance(section, dict) else {}
        return float(section.get("comparable_window_hours", 24))

    @property
    def canonical_snapshot_rule(self) -> str:
        section = self.section("learning").get("canonical_snapshot")
        return str(section.get("rule", "")) if isinstance(section, dict) else ""

    @property
    def minimum_relative_difference(self) -> float:
        return float(self.section("learning").get("minimum_relative_difference", 0.2))

    @property
    def retain_relative_difference(self) -> float:
        return float(self.section("learning").get("retain_relative_difference", 0.1))

    @property
    def outlier_ratio_to_median(self) -> float:
        return float(self.section("learning").get("outlier_ratio_to_median", 5))

    @property
    def change_baseline_hours(self) -> float:
        return float(self.section("learning").get("change_baseline_hours", 24))

    @property
    def diagnostic_dimensions(self) -> tuple[str, ...]:
        value = self.section("learning").get("diagnostic_dimensions", ["trigger"])
        return tuple(str(v) for v in value) if isinstance(value, list) else ("trigger",)

    @property
    def dayparts(self) -> list[dict[str, Any]]:
        value = self.section("learning").get("dayparts")
        return list(value) if isinstance(value, list) else list(_DEFAULT_DAYPARTS)


_DEFAULT_DAYPARTS = (
    {"name": "morning", "start_hour": 5, "end_hour": 10},
    {"name": "midday", "start_hour": 10, "end_hour": 14},
    {"name": "afternoon", "start_hour": 14, "end_hour": 18},
    {"name": "evening", "start_hour": 18, "end_hour": 22},
    {"name": "night", "start_hour": 22, "end_hour": 5},
)

_MEASUREMENT_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "threads_measurement_policy.json"
)


def load_measurement_policy(path: Path | str | None = None) -> ThreadsMeasurementPolicy:
    target = Path(path) if path is not None else _MEASUREMENT_PATH
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("threads measurement policy must be a JSON object")
    version = document.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("threads measurement policy must declare a policy_version")
    return ThreadsMeasurementPolicy(policy_version=version, raw=document)


@lru_cache
def get_measurement_policy() -> ThreadsMeasurementPolicy:
    return load_measurement_policy()


# == T4.1: Threads operations policy =========================================
@dataclass(frozen=True)
class DailyWindowSpec:
    """``[start, end)`` の壁時計の範囲。日付をまたぐ窓は V1 では扱わない。"""

    start: time
    end: time


@dataclass(frozen=True)
class ThreadsOperationsPolicy:
    """常駐 worker の運用上の選択 (T4.1)。**Meta の制限ではない。**

    タイムゾーンはここに持たない。``operations_policy.json`` の ``timezone`` を
    使う (C8 / C9.5 と同じ設定を 1 つだけ持つ)。
    """

    policy_version: str
    raw: dict[str, Any]
    publication_window: DailyWindowSpec
    approval_notification_window: DailyWindowSpec
    soft_min_gap_minutes: int
    daily_target_low: int
    daily_target_high: int

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        return value if isinstance(value, dict) else {}

    @property
    def worker(self) -> dict[str, Any]:
        return self.section("worker")

    @property
    def heartbeat_max_seconds(self) -> int:
        return int(self.worker.get("heartbeat_max_seconds", 300))

    @property
    def stale_lock_after_minutes(self) -> int:
        return int(self.worker.get("stale_lock_after_minutes", 15))

    @property
    def idle_publication_reevaluation_minutes(self) -> int:
        return int(self.worker.get("idle_publication_reevaluation_minutes", 30))

    def subsystem(self, name: str) -> dict[str, Any]:
        subsystems = self.worker.get("subsystems")
        value = subsystems.get(name) if isinstance(subsystems, dict) else None
        return value if isinstance(value, dict) else {}

    @property
    def starvation_guard_hours(self) -> float:
        return float(self.section("queue").get("starvation_guard_hours", 48))

    # -- T4.2: approval digest -------------------------------------------------
    @property
    def digest(self) -> dict[str, Any]:
        return self.section("approval_digest")

    @property
    def digest_max_items(self) -> int:
        return int(self.digest.get("max_items", 5))

    @property
    def digest_min_items(self) -> int:
        return int(self.digest.get("min_items", 1))

    @property
    def digest_cooldown_minutes(self) -> int:
        return int(self.digest.get("cooldown_minutes", 60))

    @property
    def digest_gather_minutes(self) -> int:
        return int(self.digest.get("gather_minutes", 60))

    @property
    def approval_ttl_hours(self) -> int:
        return int(self.digest.get("ttl_hours", 24))

    @property
    def digest_preview_characters(self) -> int:
        return int(self.digest.get("preview_characters", 80))

    # -- T4.3: automatic publication --------------------------------------------
    @property
    def automatic_publication_enabled(self) -> bool:
        """ポリシー上、自動公開を許すか。**既定 (コミット済みの値) は False。**

        これだけでは公開しない。worker の ``--auto-publish``・ロック・設定の健全性・
        読み取りの事前確認・queue の評価がすべてそろったときだけ公開する。
        """

        return self.section("automatic_publication").get("enabled") is True

    @property
    def automatic_publication_preflight(self) -> bool:
        return self.section("automatic_publication").get("preflight_read", True) is not False

    @property
    def stock_days_low(self) -> float:
        return float(self.section("stock").get("approved_days_low", 1))

    @property
    def stock_days_high(self) -> float:
        return float(self.section("stock").get("approved_days_high", 3))


def _parse_window(document: dict, key: str) -> DailyWindowSpec:
    raw = document.get(key)
    if not isinstance(raw, dict):
        raise ValueError(f"threads operations policy must declare {key}")
    try:
        start = time.fromisoformat(str(raw["start"]))
        end = time.fromisoformat(str(raw["end"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{key} must have HH:MM start and end") from exc
    if not start < end:
        # 日付をまたぐ窓を黙って受け付けると、境界の意味が曖昧になる。
        raise ValueError(f"{key} must start before it ends (overnight windows are not supported)")
    return DailyWindowSpec(start=start, end=end)


_OPERATIONS_PATH = Path(__file__).resolve().parents[2] / "config" / "threads_operations_policy.json"


def load_operations_policy(path: Path | str | None = None) -> ThreadsOperationsPolicy:
    target = Path(path) if path is not None else _OPERATIONS_PATH
    document = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("threads operations policy must be a JSON object")
    version = document.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("threads operations policy must declare a policy_version")
    if "timezone" in document:
        raise ValueError(
            "threads operations policy must not declare its own timezone; "
            "the operations timezone lives in operations_policy.json"
        )
    target_range = document.get("daily_activity_target") or {}
    low = int(target_range.get("low", 3))
    high = int(target_range.get("high", 5))
    if not 0 <= low <= high:
        raise ValueError("daily_activity_target must satisfy 0 <= low <= high")
    if target_range.get("advisory") is not True:
        # 目安を「上限」や「ノルマ」として読ませない。
        raise ValueError("daily_activity_target must be advisory")
    gap = int(document.get("soft_min_gap_minutes", 120))
    if gap <= 0:
        raise ValueError("soft_min_gap_minutes must be positive")
    digest = document.get("approval_digest") or {}
    max_items = int(digest.get("max_items", 5))
    min_items = int(digest.get("min_items", 1))
    if not 1 <= min_items <= max_items:
        raise ValueError("approval_digest must satisfy 1 <= min_items <= max_items")
    if max_items > 10:
        # 1 通に詰め込みすぎると、個別に読んで判断するという前提が崩れる。
        raise ValueError("approval_digest.max_items must stay small (<= 10)")
    stock = document.get("stock") or {}
    if stock and stock.get("advisory") is not True:
        raise ValueError("stock coverage must be advisory")
    return ThreadsOperationsPolicy(
        policy_version=version,
        raw=document,
        publication_window=_parse_window(document, "publication_window"),
        approval_notification_window=_parse_window(document, "approval_notification_window"),
        soft_min_gap_minutes=gap,
        daily_target_low=low,
        daily_target_high=high,
    )


@lru_cache
def get_operations_policy() -> ThreadsOperationsPolicy:
    return load_operations_policy()
