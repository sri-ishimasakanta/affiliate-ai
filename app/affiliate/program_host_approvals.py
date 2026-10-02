"""program 単位の tracking URL host の許可 (C11, 2026-10-02)。

provider 単位の許可 (:data:`~app.affiliate.destination_policy.DEFAULT_DESTINATION_HOST_POLICY`)
だけでは、複数のベンダーをまとめた ``direct`` を安全に扱えない。ここでは **program ごと** に、
Human が ASP の画面で確かめた host だけを許す。

- 記録は version 管理の ``app/config/affiliate_program_host_approvals.json``。追記だけ
  (approve / revoke の出来事)。書くのは ``scripts/affiliate_tracking_intake.py`` の
  ``approve-host`` / ``revoke-host`` を ``--execute`` で実行したときだけ。変更は git の差分で
  人が確かめて commit する。
- 許可は ``(program_id, program_name, provider, host)`` に結びつく。名前や provider が変われば
  効かない。host は正規化済みの完全一致だけ (wildcard / suffix / 部分一致なし)。
- 自サイト・合成 probe の host・IP アドレス・一段だけの名前は許可できない。
- URL 全体・query・token・秘密はこのファイルに入れない (host と識別子だけ)。
- catalog の landing page の host は許可にならない。
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from app.affiliate.destination_safety import SELF_HOSTS, AffiliateDestinationError, normalize_host

SCHEMA = "affiliate-program-host-approvals/1"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "affiliate_program_host_approvals.json"  # noqa: E501
ACTIONS = ("approve", "revoke")
_RECORD_KEYS = {"id", "action", "program_id", "program_name", "provider", "host", "decided_by",
                "observed_at", "entered_at", "source"}  # fmt: skip


class HostApprovalError(ValueError):
    """許可の記録が不適 (メッセージに URL を含めない)。"""


def approvable_host(raw: str) -> str:
    """許可してよい形の host に正規化する。だめなら raise。"""

    try:
        host = normalize_host(raw)
    except AffiliateDestinationError as exc:
        raise HostApprovalError(str(exc)) from None
    from app.affiliate.synthetic_probe import PROBE_DESTINATION_HOST

    if "*" in host or "\\" in host or len(host) > 253:
        raise HostApprovalError("host must be one exact host name (no wildcard)")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise HostApprovalError("an IP address cannot be approved")
    if "." not in host:
        raise HostApprovalError("a single-label host cannot be approved")
    if host in SELF_HOSTS:
        raise HostApprovalError("this site cannot be an affiliate destination")
    if host == PROBE_DESTINATION_HOST:
        raise HostApprovalError("the synthetic probe host cannot be approved")
    return host


def load_records(path: Path | None = None) -> list[dict]:
    path = path or DEFAULT_PATH
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise HostApprovalError(f"{path.name} is not a {SCHEMA} file")
    records = data.get("records") or []
    for number, record in enumerate(records, 1):
        if set(record) != _RECORD_KEYS or record["id"] != number or record["action"] not in ACTIONS:
            raise HostApprovalError(f"{path.name} record {number} is malformed (do not edit "
                                    "the file by hand)")  # fmt: skip
        approvable_host(record["host"])
    return records


def policy_from_records(records: list[dict]) -> dict[int, frozenset[tuple[str, str, str]]]:
    """今有効な許可: program_id -> {(program_name, provider, host)}。revoke は同じ program × host
    の許可を外す。"""

    active: dict[int, set[tuple[str, str, str]]] = {}
    for record in records:
        pid = int(record["program_id"])
        entry = (record["program_name"], record["provider"], record["host"])
        if record["action"] == "approve":
            active.setdefault(pid, set()).add(entry)
        else:
            active[pid] = {e for e in active.get(pid, set()) if e[2] != record["host"]}
    return {pid: frozenset(entries) for pid, entries in active.items() if entries}


def load_program_host_policy(path: Path | None = None) -> Mapping[int, frozenset]:
    return policy_from_records(load_records(path))


def append_record(record: dict, path: Path | None = None) -> dict:
    """1 件足す (前の記録はそのまま書き戻す)。書き込みは一時ファイル経由で置き換える。"""

    path = path or DEFAULT_PATH
    records = load_records(path)
    full = {"id": len(records) + 1, **record}
    if set(full) != _RECORD_KEYS or full["action"] not in ACTIONS:
        raise HostApprovalError("record is malformed")
    full["host"] = approvable_host(full["host"])
    body = {"schema": SCHEMA, "note": NOTE, "records": [*records, full]}
    lines = [json.dumps({k: v for k, v in body.items() if k != "records"}, ensure_ascii=False)[:-1]
             + ', "records": [']  # fmt: skip
    last = len(body["records"]) - 1
    lines += ["  " + json.dumps(r, ensure_ascii=False, sort_keys=True) + ("," if i < last else "")
              for i, r in enumerate(body["records"])]
    lines.append("]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".host-approvals-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return full


NOTE = ("C11 program-level tracking URL host approvals. Append-only; written only by "
        "scripts/affiliate_tracking_intake.py approve-host / revoke-host --execute after a human "
        "checked the host at the provider. Hosts and identifiers only (no URL, token or secret). "
        "A catalog landing page host is not an approval.")  # fmt: skip

__all__ = ["ACTIONS", "DEFAULT_PATH", "HostApprovalError", "SCHEMA", "append_record",
           "approvable_host", "load_program_host_policy", "load_records", "policy_from_records"]
