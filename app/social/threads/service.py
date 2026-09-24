"""ThreadsService -- アプリケーション側の意味論 (T1)。

client は「どう話すか」を持ち、この service は「何をしてよいか」を持つ。

守る境界:

- **既定は PLAN。** 外部へ書き込むのは、呼び出し側が ``execute=True`` を明示した
  ときだけである (このリポジトリの他の経路と同じ約束)。
- **T2 の提案エンジンはこの service しか呼ばない。** HTTP を直接触らせない。
- **承認は公開ではない。** 公開が許されるのは、人が承認した不変の提案を持って
  きたときだけ。この service は自分で投稿文を作らないし、承認も行わない。
- 設定が無ければ、外部に触れる前に fail closed。

T1 では本番公開を行わない。``publish_text`` は実装済みだが、``execute=True`` は
T3 の公開経路 (人の承認を経たもの) からしか渡されない。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.social.threads.client import ThreadsClient
from app.social.threads.errors import ThreadsError, ThreadsNotConfiguredError
from app.social.threads.models import (
    DEFAULT_API_VERSION,
    MEDIA_METRICS,
    METRICS_WITHOUT_TIME_RANGE,
    RECOMMENDED_PUBLISH_DELAY_SECONDS,
    TEXT_MAX_LENGTH,
    USER_METRICS,
    ThreadsInsights,
    ThreadsProfile,
    ThreadsPublication,
    text_within_limit,
    validate_metrics,
)

#: 公開が満たすべき前提 (T3 がここへ承認済み提案を渡す)。
PUBLISH_REQUIRES = (
    "the text must come from a human-approved immutable proposal",
    "approval is recorded before publishing, and is not itself publishing",
    "an explicit execute intent must be supplied",
)


#: 設定の状態 (T4.1)。**「使っていない」と「壊れている」を同じ語で呼ばない。**
#:
#: - ``disabled``: THREADS_ENABLED=false。意図した停止であり、健全な no-op。
#: - ``misconfigured``: THREADS_ENABLED=true なのに必要な設定が欠けている/不正。
#:   **運用上の障害** であり、すぐ人に知らせる。
#: - ``ready``: 有効で、必要な設定がそろっている。
THREADS_STATE_DISABLED = "disabled"
THREADS_STATE_MISCONFIGURED = "misconfigured"
THREADS_STATE_READY = "ready"
THREADS_STATES = (THREADS_STATE_DISABLED, THREADS_STATE_MISCONFIGURED, THREADS_STATE_READY)


@dataclass
class ThreadsConnectionStatus:
    """接続確認の結果。**token は含めない。**"""

    enabled: bool
    configured: bool
    api_version: str
    user_id_configured: bool
    access_token_configured: bool
    reachable: bool | None = None
    profile: dict | None = None
    error: dict | None = None
    notes: list[str] = field(default_factory=list)
    #: 欠けている/不正な設定の **名前だけ**。値は決して入れない。
    config_issues: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if not self.enabled:
            return THREADS_STATE_DISABLED
        return THREADS_STATE_READY if self.configured else THREADS_STATE_MISCONFIGURED

    def as_dict(self) -> dict:
        return {
            "threads_state": self.state,
            "threads_config_issues": list(self.config_issues),
            "threads_enabled": self.enabled,
            "threads_configured": self.configured,
            "threads_api_version": self.api_version,
            "threads_user_id_configured": self.user_id_configured,
            "threads_access_token_configured": self.access_token_configured,
            "reachable": self.reachable,
            "profile": self.profile,
            "error": self.error,
            "notes": list(self.notes),
        }


@dataclass
class PublishOutcome:
    """公開 (または PLAN) の結果。"""

    executed: bool
    outcome: str
    text_length: int
    creation_id: str | None = None
    publication: dict | None = None
    blocked_reasons: list[str] = field(default_factory=list)
    error: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.blocked_reasons

    def as_dict(self) -> dict:
        return {
            "executed": self.executed,
            "outcome": self.outcome,
            "text_length": self.text_length,
            "creation_id": self.creation_id,
            "publication": self.publication,
            "blocked_reasons": list(self.blocked_reasons),
            "error": self.error,
        }


class ThreadsService:
    def __init__(self, settings, *, client: ThreadsClient | None = None, sleep=time.sleep) -> None:
        self._settings = settings
        self._client = client or ThreadsClient(settings)
        self._sleep = sleep

    @property
    def client(self) -> ThreadsClient:
        """公開経路 (T3) が使う HTTP 層。判断はここには無い。"""

        return self._client

    # -- configuration --------------------------------------------------------
    def describe(self) -> ThreadsConnectionStatus:
        """設定状況だけを返す (外部に触れない)。**token は出さない。**"""

        settings = self._settings
        raw_user_id = str(getattr(settings, "threads_user_id", "") or "").strip()
        raw_token = (getattr(settings, "threads_access_token", "") or "").strip()
        user_id = bool(raw_user_id)
        token = bool(raw_token)
        enabled = bool(getattr(settings, "threads_enabled", False))

        # 値そのものは見せない。問題のある設定の **名前** だけを集める。
        issues: list[str] = []
        if not user_id:
            issues.append("THREADS_USER_ID is missing")
        elif not raw_user_id.isdigit():
            issues.append("THREADS_USER_ID must be the numeric Threads user id")
        if not token:
            issues.append("THREADS_ACCESS_TOKEN is missing")
        elif any(ch.isspace() for ch in raw_token):
            issues.append("THREADS_ACCESS_TOKEN contains whitespace")

        status = ThreadsConnectionStatus(
            enabled=enabled,
            configured=bool(enabled and not issues),
            api_version=(getattr(settings, "threads_api_version", "") or DEFAULT_API_VERSION),
            user_id_configured=user_id,
            access_token_configured=token,
            # 無効なときに欠けている設定は「問題」ではない (使っていないだけ)。
            config_issues=issues if enabled else [],
        )
        if not enabled:
            status.notes.append("THREADS_ENABLED is false; no Threads call will be made")
            # 無効なうちは問題ではないが、有効にするとき何が要るかは見せておく。
            status.notes.extend(
                f"{issue} (needed only when THREADS_ENABLED=true)" for issue in issues
            )
        else:
            status.notes.extend(issues)
        return status

    def check_connection(self) -> ThreadsConnectionStatus:
        """設定が揃っていればプロフィールだけを読む (副作用なし)。"""

        status = self.describe()
        if not status.configured:
            status.reachable = None
            return status
        try:
            profile: ThreadsProfile = self._client.fetch_profile()
        except ThreadsError as exc:
            status.reachable = False
            status.error = exc.as_dict()
            return status
        status.reachable = True
        status.profile = profile.as_dict()
        return status

    # -- publishing -----------------------------------------------------------
    def publish_text(
        self,
        text: str,
        *,
        execute: bool = False,
        approved_proposal_hash: str | None = None,
    ) -> PublishOutcome:
        """テキスト投稿を公開する。**既定は PLAN で、外部に何も書かない。**

        ``execute=True`` は「人が承認した不変の提案を適用する」経路からしか
        渡らない。承認そのものはここでは行わないし、ここで文章を作ることもない。
        """

        outcome = PublishOutcome(executed=False, outcome="planned", text_length=len(text or ""))
        status = self.describe()
        if not status.enabled:
            outcome.blocked_reasons.append("THREADS_ENABLED is false")
        if not status.user_id_configured:
            outcome.blocked_reasons.append("THREADS_USER_ID is not configured")
        if not status.access_token_configured:
            outcome.blocked_reasons.append("THREADS_ACCESS_TOKEN is not configured")
        if not text_within_limit(text):
            outcome.blocked_reasons.append(
                f"the text must be between 1 and {TEXT_MAX_LENGTH} characters "
                f"(got {len(text or '')})"
            )
        if execute and not approved_proposal_hash:
            # 承認の痕跡なしに外部へ公開しない。
            outcome.blocked_reasons.append(
                "publishing requires the hash of the human-approved proposal"
            )

        if outcome.blocked_reasons:
            outcome.outcome = "blocked"
            return outcome
        if not execute:
            return outcome

        try:
            container = self._client.create_text_container(text)
            outcome.creation_id = container.creation_id
            # 公式が推奨する待ち時間を守る (急いで publish しない)。
            self._sleep(RECOMMENDED_PUBLISH_DELAY_SECONDS)
            publication: ThreadsPublication = self._client.publish_container(container.creation_id)
        except ThreadsError as exc:
            outcome.executed = True
            outcome.outcome = "failed"
            outcome.error = exc.as_dict()
            return outcome

        outcome.executed = True
        outcome.outcome = "published"
        outcome.publication = publication.as_dict()
        return outcome

    # -- measurement (T3 が使う) ----------------------------------------------
    def media_insights(self, media_id: str, metrics=MEDIA_METRICS) -> ThreadsInsights:
        """投稿単位の指標。**公式に存在する名前だけ**を問い合わせる。"""

        wanted = validate_metrics(metrics, MEDIA_METRICS)
        if not wanted:
            raise ThreadsNotConfiguredError("no supported media metric was requested")
        payload = self._client.fetch_media_insights(media_id, wanted)
        return _insights(f"media:{media_id}", payload, wanted)

    def user_insights(self, metrics=USER_METRICS, *, since=None, until=None) -> ThreadsInsights:
        """アカウント単位の指標。時間範囲と併用できない指標は自動で外さない。"""

        wanted = validate_metrics(metrics, USER_METRICS)
        if not wanted:
            raise ThreadsNotConfiguredError("no supported user metric was requested")
        if (since is not None or until is not None) and any(
            m in METRICS_WITHOUT_TIME_RANGE for m in wanted
        ):
            # 黙って落とさない -- 呼び出し側に選ばせる。
            raise ThreadsNotConfiguredError(
                "follower_demographics cannot be combined with since/until; "
                "request it in a separate call"
            )
        payload = self._client.fetch_user_insights(wanted, since=since, until=until)
        return _insights("user", payload, wanted)

    def fetch_publication(self, media_id: str) -> dict:
        """公開済み投稿の事実 (permalink / timestamp) を読む。"""

        return self._client.fetch_media(media_id)


def _insights(subject: str, payload: dict, wanted) -> ThreadsInsights:
    """Meta の ``{"data":[{"name":...,"values":[{"value":n}]}]}`` を平らにする。

    欠けている指標は 0 で埋めず、``missing`` として残す。
    """

    values: dict[str, object] = {}
    data = payload.get("data")
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not name:
                continue
            raw = entry.get("values")
            if isinstance(raw, list) and raw and isinstance(raw[0], dict):
                values[str(name)] = raw[0].get("value")
            elif "total_value" in entry and isinstance(entry["total_value"], dict):
                values[str(name)] = entry["total_value"].get("value")
    missing = tuple(m for m in wanted if m not in values)
    return ThreadsInsights(subject=subject, values=values, missing=missing)
