"""Cross-runtime golden fixtures を Python から生成する (deterministic, 非 secret)。

WordPress MU-plugin (PHP) 側の canonical JSON / projection_entry_hash /
projection_snapshot_hash / HMAC 検証が Python と byte 一致することを、
実 PHP 実行 (wordpress/mu-plugins/bizfluxlab-affiliate-runtime/tests/run.php) で
証明するための固定 JSON。

    uv run python scripts/gen_affiliate_projection_fixtures.py

出力先: wordpress/mu-plugins/bizfluxlab-affiliate-runtime.fixtures/
合成ドメイン (*.example.test) と偽 token / 偽 hash / 偽 secret のみ。
実 ASP URL / 本番 token / 本番 secret は含めない。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.affiliate.projection import (  # noqa: E402
    build_projection_batch_request,
    build_projection_entry,
    build_projection_snapshot,
)
from app.affiliate.projection_signing import (  # noqa: E402
    body_sha256,
    compute_signature,
)
from app.article.draft_input_canonical import canonical_json  # noqa: E402

_OUT = (
    Path(__file__).resolve().parent.parent
    / "wordpress"
    / "mu-plugins"
    / "bizfluxlab-affiliate-runtime.fixtures"
)

_LH1 = "1" * 64
_LH2 = "2" * 64
_A = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_D = datetime(2026, 9, 3, 9, 30, 0, tzinfo=UTC)

_LS = " "  # LINE SEPARATOR
_PS = " "  # PARAGRAPH SEPARATOR


def _entry(**kw):
    base = dict(
        token="AAAA0000tokenone00000",
        destination_url="https://aff.example.test/track/x?a8mat=SYNTH1&utm_source=n#f",
        destination_host="aff.example.test",
        link_identity_hash=_LH1,
        alt_status="active",
        activated_at=_A,
        disabled_at=None,
    )
    base.update(kw)
    return build_projection_entry(**base)


def _projection_fixture(description: str, entries: list) -> dict:
    snapshot = build_projection_snapshot(entries)
    return {
        "description": description,
        "note": "synthetic fixture; no real ASP URL / no production token",
        "request_body": build_projection_batch_request(snapshot),
        "expected": {
            "projection_snapshot_hash": snapshot.projection_snapshot_hash,
            "entries": [
                {
                    "token": t.token,
                    "status": t.status,
                    "projection_version": t.projection_version,
                    "projection_entry_hash": t.projection_entry_hash,
                }
                for t in snapshot.targets
            ],
        },
    }


def _canonical_primitive(name: str, value: object) -> dict:
    cj = canonical_json(value)
    return {
        "name": name,
        "value": value,
        "canonical_json": cj,
        "canonical_json_sha256": hashlib.sha256(cj.encode("utf-8")).hexdigest(),
    }


def _canonical_primitives_fixture() -> dict:
    cases = [
        _canonical_primitive("ascii_string", "plain ascii value"),
        _canonical_primitive("slash_not_escaped", "https://a.example/x/y?q=1#f"),
        _canonical_primitive("quote_and_backslash", 'a "quote" and a \\ backslash'),
        _canonical_primitive("cjk_raw_utf8", "日本語 検索 テスト"),
        # D-C0.1 correction: with ensure_ascii=False Python emits U+2028/U+2029
        # as raw UTF-8 bytes (NOT   /  ). PHP must match
        # (JSON_UNESCAPED_LINE_TERMINATORS).
        _canonical_primitive("u2028_line_separator", f"before{_LS}after"),
        _canonical_primitive("u2029_paragraph_separator", f"x{_PS}y"),
        _canonical_primitive("control_char_escaped", "tab\tnewline\nend"),
        _canonical_primitive("key_sort", {"b": 2, "a": 1, "_z": 3, "A": 4}),
        _canonical_primitive("list_order_preserved", ["z", "a", "m", "a"]),
        _canonical_primitive("null_and_int", {"n": None, "i": 42, "zero": 0}),
        _canonical_primitive(
            "nested",
            {"nested": {"y": 1, "x": 2}, "arr": [{"k": "v"}, {"a": None}]},
        ),
        _canonical_primitive("empty_object", {}),
        _canonical_primitive("empty_list", []),
    ]
    return {
        "description": (
            "direct BFL_Canonical::encode parity vectors; PHP recomputes "
            "sha256(encode(value)) and must equal canonical_json_sha256"
        ),
        "note": "synthetic; exercises U+2028/U+2029 raw-UTF-8 emission",
        "cases": cases,
    }


def _hmac_vectors_fixture() -> dict:
    secret = "synthetic-shared-secret-for-fixtures-only"
    vectors = []
    _proj = "/wp-json/affiliate-ai/v1/target-projections"
    _clk = "/wp-json/affiliate-ai/v1/outbound-clicks"
    combos = [
        ("POST", _proj, b'{"schema_version":1,"targets":[]}', 1_760_000_000),
        ("GET", f"{_clk}?limit=1000&since_id=0", b"", 1_760_000_123),
        ("GET", f"{_clk}?limit=50&since_id=987", b"", 1_760_000_999),
    ]
    for method, path, body, ts in combos:
        bh = body_sha256(body)
        vectors.append(
            {
                "method": method,
                "signed_path": path,
                "timestamp": ts,
                "body_utf8": body.decode("utf-8"),
                "body_sha256": bh,
                "signature": compute_signature(
                    shared_secret=secret,
                    method=method,
                    path=path,
                    timestamp=ts,
                    body_sha256_hex=bh,
                ),
            }
        )
    return {
        "description": "HMAC v1 vectors; PHP BFL_Hmac::verify must accept each",
        "note": "synthetic secret; never a production value",
        "shared_secret": secret,
        "max_skew_seconds": 300,
        "vectors": vectors,
    }


def build_all() -> dict[str, dict]:
    active_v1 = _entry(token="AAAA0000tokenone00000")
    disabled_v2 = _entry(
        token="BBBB0000tokentwo00000",
        link_identity_hash=_LH2,
        alt_status="disabled",
        disabled_at=_D,
    )
    unicode_path = _entry(
        token="CCCC0000tokenthree000",
        destination_url="https://aff.example.test/検索/日本語?q=あ&x=1#フラグ",
        destination_host="aff.example.test",
    )
    return {
        "empty_snapshot.json": _projection_fixture("empty snapshot", []),
        "active_v1_snapshot.json": _projection_fixture(
            "one active ASCII-host target (version 1)", [active_v1]
        ),
        "disabled_v2_snapshot.json": _projection_fixture(
            "one disabled target (version 2)", [disabled_v2]
        ),
        "two_target_snapshot.json": _projection_fixture(
            "two targets, token-sorted", [disabled_v2, active_v1]
        ),
        "unicode_path_snapshot.json": _projection_fixture(
            "active target whose URL path/query contains non-ASCII (host stays ASCII)",
            [unicode_path],
        ),
        "canonical_primitives.json": _canonical_primitives_fixture(),
        "hmac_vectors.json": _hmac_vectors_fixture(),
    }


def main() -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in build_all().items():
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (_OUT / name).write_text(text, encoding="utf-8")
        print(f"wrote {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
