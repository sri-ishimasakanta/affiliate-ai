"""システムが書いた記録を読む (読むだけ): worker の起動のログ。

常駐 worker は起動時に 1 行を書く (``app/services/threads_worker_log.py``)::

    ... threads-worker INFO event=started mode=resident pid=16200 policy=t6.0
        capabilities=collect_insights,... auto_publish_flag=True auto_publish_policy=enabled
        can_publish=True

ロックを持つ pid の最後の ``event=started`` を、その worker が実際に持っている能力 (公開
できるか) の記録として使う。ログの場所は ``app/operations/threads_worker_task.py`` の既定
(``D:\\Logs\\affiliate-ai\\threads-worker.log``)。ファイルの末尾だけを読む。
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_WORKER_LOG = Path(r"D:\Logs\affiliate-ai\threads-worker.log")
TAIL_BYTES = 2_000_000
_STARTED = re.compile(r"^(?P<at>\S+) threads-worker INFO event=started (?P<fields>.*)$")
_FIELD = re.compile(r"(\w+)=(\S+)")


def parse_started(line: str) -> dict | None:
    match = _STARTED.match(line.strip())
    if not match:
        return None
    fields = dict(_FIELD.findall(match.group("fields")))
    as_bool = {"True": True, "False": False}
    return {
        "at": match.group("at"),
        "mode": fields.get("mode"),
        "pid": int(fields["pid"]) if fields.get("pid", "").isdigit() else None,
        "policy": fields.get("policy"),
        "capabilities": [c for c in fields.get("capabilities", "").split(",") if c and c != "none"],
        "auto_publish_flag": as_bool.get(fields.get("auto_publish_flag")),
        "auto_publish_policy": fields.get("auto_publish_policy"),
        "can_publish": as_bool.get(fields.get("can_publish")),
    }


def last_started(text: str, *, pid: int | None = None) -> dict | None:
    """最後の起動の記録 (``pid`` を指定すればその pid の最後)。"""

    found = None
    for line in text.splitlines():
        event = parse_started(line)
        if event and (pid is None or event["pid"] == pid):
            found = event
    return found


def read_tail(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - TAIL_BYTES))
        return handle.read().decode("utf-8", errors="replace")


def lock_pid(owner_label: str | None) -> int | None:
    match = re.search(r"\bpid=(\d+)", owner_label or "")
    return int(match.group(1)) if match else None
