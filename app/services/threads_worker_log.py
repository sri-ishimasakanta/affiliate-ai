"""常駐 Threads worker のログ行 (観測できるようにするための整形、T4.3)。

常駐モードの CLI は、正常に動いているあいだは ``run()`` から戻らない。旧実装は
状態の報告を ``run()`` の後にしか出さなかったので、ログは 0 バイトのままだった
(標準出力のバッファの問題ではない)。ここで、起きたことをその場で 1 行ずつ出す。

出す: 起動 / 仕事の実行と結果 / 警告・失敗 / ロックの結果 / 停止
出さない: 眠るたび・待つたびの行

``health`` と ``queue_observation`` は 5 分おきに動くので、**状態が変わったとき
(と最初の 1 回) だけ** 出す。

どの行も最後に ``sanitize()`` を通す。access token・capability・承認の生 URL・
パスワード・追跡 URL・/go/ の token は出さない (URL は ``[url]`` に置き換える)。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.operations.local_time import to_local
from app.social.threads.errors import redact

#: URL は丸ごと伏せる。承認の URL (capability が fragment にある)、追跡 URL、
#: /go/<token> のどれも、ログに経路や token を残さない。
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
#: /go/<token> が URL の外に単独で現れた場合も伏せる。
_GO_TOKEN = re.compile(r"(?i)/go/[A-Za-z0-9._~-]+")
#: 長い乱数らしい値 (capability・token の断片) を伏せる。数字だけの id は残す。
_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z_-])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}\b")
#: key=value 形式の秘密。
_SECRET_KV = re.compile(r"(?i)\b(password|passwd|secret|token|capability)=\S+")


def sanitize(text: str) -> str:
    out = redact(text or "")
    out = _URL.sub("[url]", out)
    out = _GO_TOKEN.sub("/go/[redacted]", out)
    out = _SECRET_KV.sub(lambda m: f"{m.group(1)}=[redacted]", out)
    out = _OPAQUE.sub("[redacted]", out)
    return out


class WorkerLogFormatter:
    """worker の出来事を 1 行ずつの文字列にする。何も出さないときは ``None``。"""

    #: 状態が変わったときだけ出す仕事。
    CHANGE_ONLY = ("health", "queue_observation")

    def __init__(self, *, timezone: ZoneInfo) -> None:
        self._tz = timezone
        self._last: dict[str, str] = {}

    # -- public ------------------------------------------------------------------
    def startup(self, *, now: datetime, policy_version: str, capabilities: dict) -> str:
        policy_state = "enabled" if capabilities.get("auto_publish_policy") else "disabled"
        caps = ",".join(
            name
            for name in ("collect_insights", "sync_approvals", "send_approval_digests")
            if capabilities.get(name)
        )
        return self._line(
            now,
            "started",
            mode="resident",
            pid=os.getpid(),
            policy=policy_version,
            capabilities=caps or "none",
            auto_publish_flag=capabilities.get("auto_publish_flag"),
            auto_publish_policy=policy_state,
            can_publish=capabilities.get("publish"),
        )

    def format(self, event: dict) -> str | None:
        kind = event.get("event")
        now = event.get("at")
        if kind == "subsystem":
            return self._subsystem(now, event)
        if kind == "subsystem_failed":
            return self._line(
                now,
                "subsystem_failed",
                level="WARN",
                name=event.get("name"),
                error=event.get("error"),
                retry_at=self._local(event.get("next_run_at")),
            )
        if kind == "lock_acquired":
            return self._line(
                now, "lock_acquired", reclaimed_stale=bool(event.get("reclaimed_stale"))
            )
        if kind == "already_running":
            return self._line(
                now,
                "already_running",
                level="WARN",
                detail="another Threads worker holds the lock; exiting without work",
            )
        if kind == "lock_lost":
            return self._line(
                now,
                "lock_lost",
                level="ERROR",
                detail="the worker lock was reclaimed by another worker; stopping",
            )
        if kind == "stopped":
            level = "INFO" if event.get("reason") in ("completed", "interrupted") else "ERROR"
            return self._line(
                now,
                "stopped",
                level=level,
                reason=event.get("reason"),
                exit_code=event.get("exit_code"),
                cycles=event.get("cycles"),
                publications=event.get("publications"),
            )
        return None

    # -- subsystems --------------------------------------------------------------
    def _subsystem(self, now, event: dict) -> str | None:
        name = event.get("name")
        summary = event.get("summary") or {}
        next_run = self._local(event.get("next_run_at"))
        builder = getattr(self, f"_describe_{name}", None)
        fields, level = builder(summary) if builder else ({}, "INFO")
        if name in self.CHANGE_ONLY:
            signature = repr(sorted(fields.items()))
            if self._last.get(name) == signature:
                return None
            self._last[name] = signature
        return self._line(now, name, level=level, **fields, next=next_run)

    @staticmethod
    def _describe_health(summary: dict):
        issues = summary.get("config_issues") or []
        state = summary.get("threads_state")
        level = "WARN" if state == "misconfigured" else "INFO"
        return {"threads_state": state, "config_issues": ";".join(issues) or None}, level

    @staticmethod
    def _describe_approval_sync(summary: dict):
        applied = summary.get("applied") or 0
        failed = summary.get("failed") or 0
        if failed:
            result = "failed"
        elif applied:
            result = f"applied {applied} decision(s)"
        else:
            result = "checked, no decisions"
        fields = {
            "result": result,
            "fetched": summary.get("fetched"),
            "skipped": summary.get("skipped"),
        }
        return fields, ("WARN" if failed else "INFO")

    @staticmethod
    def _describe_queue_observation(summary: dict):
        return {
            "approved": summary.get("approved"),
            "approved_unpublished": summary.get("approved_unpublished"),
            "awaiting_approval": summary.get("awaiting_approval"),
        }, "INFO"

    @staticmethod
    def _describe_insights_refresh(summary: dict):
        refreshed = summary.get("refreshed") or []
        if not refreshed:
            due = summary.get("would_refresh") or []
            if due:
                # 期限は来ているが取得しなかった (PLAN、または取得のフラグが無い)。
                return {"result": "due, not collected", "due": ",".join(map(str, due))}, "INFO"
            return {"result": "not due"}, "INFO"
        parts = []
        level = "INFO"
        for item in refreshed:
            result = item.get("result")
            if result == "failed":
                level = "WARN"
                parts.append(
                    f"#{item.get('publication_id')}:failed[{item.get('category') or 'unknown'}]"
                    f" {item.get('reason') or ''}".rstrip()
                )
            else:
                parts.append(f"#{item.get('publication_id')}:{result}")
        return {
            "result": "; ".join(parts),
            "network_calls": summary.get("network_calls"),
        }, level

    @staticmethod
    def _describe_publication_evaluation(summary: dict):
        # blockers = 実行の **前** の評価。公開の結果の後に、次の評価のブロッカーを分けて出す。
        fields = {
            "next_candidate": summary.get("next_candidate_id"),
            "blockers": ",".join(summary.get("blockers") or []) or "(none)",
        }
        level = "WARN" if summary.get("problems") else "INFO"
        attempt = summary.get("auto_publish")
        if attempt:
            fields["auto_publish"] = attempt.get("outcome")
            if attempt.get("publication_id"):
                fields["publication"] = attempt.get("publication_id")
            fields["threads_writes"] = attempt.get("threads_writes")
            if attempt.get("outcome") in ("uncertain", "failed", "preflight_failed"):
                level = "ERROR"
        if summary.get("next_blockers") is not None:
            fields["next_blockers"] = ",".join(summary["next_blockers"]) or "(none)"
        return fields, level

    @staticmethod
    def _describe_approval_notification_flush(summary: dict):
        digest = summary.get("digest") or {}
        fields = {
            "would_send": summary.get("would_send"),
            "waiting_for": ",".join(summary.get("waiting_for") or []) or None,
            "selected": ",".join(map(str, summary.get("selected") or [])) or None,
            "emails_sent": summary.get("emails_sent"),
        }
        if digest:
            fields["digest"] = digest.get("digest_id")
            fields["digest_reason"] = digest.get("reason")
        return fields, "INFO"

    # -- helpers -----------------------------------------------------------------
    def _local(self, moment) -> str | None:
        if moment is None:
            return None
        return to_local(moment, self._tz).strftime("%Y-%m-%dT%H:%M:%S%z")

    def _line(self, now, event: str, *, level: str = "INFO", **fields) -> str:
        stamp = self._local(now) if now is not None else "-"
        parts = [f"{stamp} threads-worker {level} event={event}"]
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value)
            parts.append(f'{key}="{text}"' if " " in text else f"{key}={text}")
        return sanitize(" ".join(parts))


__all__ = ["WorkerLogFormatter", "sanitize"]
