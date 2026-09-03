"""生成された draft 本文の **editorial validation** (pure)。

parse/transport 成功後にのみ実行する (§13)。結果は Human review 用の
``validation_report`` であり run.status を左右しない。``fail`` が 1 件以上あると
``promotion_eligible=false``。heuristic (キーワード近接) なので warn/fail は
Human へのシグナルであり hard gate ではない。
"""

from __future__ import annotations

import re

from app.article.draft_output_contract import ParsedDraft

_MIN_BODY_CHARS = 800
_META_MIN, _META_MAX = 60, 160
_META_HARD_MIN, _META_HARD_MAX = 20, 220

_H1_LINE_RE = re.compile(r"^#\s", re.MULTILINE)
_SETEXT_H1_RE = re.compile(r"^\S.*\n=+\s*$", re.MULTILINE)
_H2_LINE_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

_PR_MARKERS = ("PR", "広告", "アフィリエイト", "プロモーション", "スポンサー")
_PRICE_TOKENS = ("円", "$", "ドル", "/月", "/年", "／月", "／年", "USD")
_ASOF_MARKERS = ("時点",)
_EXAGGERATION = (
    "No.1", "no.1", "ナンバーワン", "必ず", "絶対に", "絶対おすすめ",
    "最強", "圧倒的", "劇的", "最も優れ", "一番優れ",
)
_HEDGE = ("確認できませんでした", "確認できず", "未確認", "公式情報では", "公式では確認")
_AFFILIATE_REASON_VOCAB = (
    "報酬", "報酬率", "紹介料", "コミッション", "成果報酬", "提携報酬",
    "アフィリエイト条件", "収益条件", "提携条件",
)
_COMMISSION_MAGNITUDES = ("35%", "30%", "25%", "20%")
_SUPERLATIVE = (
    "最も優れ", "一番優れ", "No.1", "no.1", "ナンバーワン", "絶対おすすめ", "圧倒的",
)
_QUALIFIER = ("目的", "用途", "なら", "向け", "場合")
_WINDOW = 90


def _windows_around(text: str, needle: str, radius: int = _WINDOW) -> list[str]:
    out: list[str] = []
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            break
        out.append(text[max(0, i - radius) : i + len(needle) + radius])
        start = i + len(needle)
    return out


def _check(cid: str, level: str, detail: str) -> dict:
    return {"id": cid, "level": level, "detail": detail}


def _structural_checks(parsed: ParsedDraft, package: dict) -> list[dict]:
    body = parsed.body_markdown
    checks: list[dict] = []

    if _H1_LINE_RE.search(body) or _SETEXT_H1_RE.search(body):
        checks.append(_check("body_no_h1", "fail", "body_markdown に H1 がある"))
    else:
        checks.append(_check("body_no_h1", "pass", "H1 なし"))

    if len(body) < _MIN_BODY_CHARS:
        checks.append(
            _check("body_min_length", "fail", f"本文が短い ({len(body)})")
        )
    else:
        checks.append(_check("body_min_length", "pass", f"{len(body)} chars"))

    ml = len(parsed.meta_description)
    if ml < _META_HARD_MIN or ml > _META_HARD_MAX:
        checks.append(_check("meta_length", "fail", f"meta 長さ {ml} が許容外"))
    elif ml < _META_MIN or ml > _META_MAX:
        checks.append(_check("meta_length", "warn", f"meta 長さ {ml} が推奨外"))
    else:
        checks.append(_check("meta_length", "pass", f"{ml} chars"))

    head = body[:700]
    if any(m in head for m in _PR_MARKERS):
        checks.append(_check("pr_disclosure", "pass", "冒頭に PR/広告表記あり"))
    else:
        checks.append(_check("pr_disclosure", "fail", "冒頭に PR/広告表記なし"))

    missing = [
        t["subject_ref"]
        for t in package["comparison_tools"]
        if t["subject_ref"] not in body
    ]
    if missing:
        checks.append(
            _check("all_tools_present", "fail", f"本文に未登場: {missing}")
        )
    else:
        checks.append(_check("all_tools_present", "pass", "全ツール登場"))

    outline_h2 = [
        s["heading"] for s in package["plan"]["outline"] if s.get("level") == "H2"
    ]
    body_h2 = _H2_LINE_RE.findall(body)
    if outline_h2:
        need = max(3, len(outline_h2) - 3)
        if len(body_h2) < need:
            checks.append(
                _check(
                    "outline_h2_presence",
                    "fail",
                    f"## 見出しが少ない ({len(body_h2)} < {need})",
                )
            )
        else:
            checks.append(
                _check(
                    "outline_h2_presence", "pass", f"## 見出し {len(body_h2)} 個"
                )
            )

    if any(tok in body for tok in _PRICE_TOKENS):
        if any(m in body for m in _ASOF_MARKERS):
            checks.append(
                _check("pricing_asof_notice", "pass", "料金に時点注記あり")
            )
        else:
            checks.append(
                _check("pricing_asof_notice", "warn", "料金表記に時点注記なし")
            )

    return checks


def _exaggeration_check(body: str) -> list[dict]:
    hits = sorted({w for w in _EXAGGERATION if w in body})
    if hits:
        return [
            _check("prohibited_exaggeration", "warn", f"誇大・断定の可能性: {hits}")
        ]
    return [_check("prohibited_exaggeration", "pass", "誇大表現なし")]


_JP_SUPPORT_RE = re.compile(
    r"日本語(?:に)?(?:対応していない|非対応|未対応|対応)"
)


def _claim_safety_checks(body: str, package: dict) -> list[dict]:
    checks: list[dict] = []

    # 「日本語(非)対応」の言明ごとに、直前 40 字以内の "Make" から言明+25 字までの
    # 区間に hedge が無ければ fail (無関係な hedge の混入で見逃さない)。
    make_bad = False
    for m in _JP_SUPPORT_RE.finditer(body):
        pre = body[max(0, m.start() - 40) : m.start()]
        if "Make" not in pre:
            continue
        seg = pre[pre.rfind("Make") :] + body[m.start() : m.end() + 25]
        if not any(h in seg for h in _HEDGE):
            make_bad = True
            break
    checks.append(
        _check(
            "claim_make_japanese",
            "fail" if make_bad else "pass",
            "Make の日本語対応/非対応を断定" if make_bad else "Make 日本語 断定なし",
        )
    )

    hs_bad = any(
        ("AI" in w)
        and any(v in w for v in ("搭載", "提供しています", "できます", "機能があります"))
        and not any(h in w for h in _HEDGE)
        for w in _windows_around(body, "HubSpot")
    )
    checks.append(
        _check(
            "claim_hubspot_ai",
            "warn" if hs_bad else "pass",
            "HubSpot の具体 AI 機能を断定の可能性" if hs_bad else "HubSpot AI 断定なし",
        )
    )

    td_bad = any(
        any(v in w for v in ("ワークフロー自動化", "自動化ワークフロー", "自動化エンジン"))
        and not any(h in w for h in _HEDGE)
        for w in _windows_around(body, "Todoist")
    )
    checks.append(
        _check(
            "claim_todoist_automation",
            "warn" if td_bad else "pass",
            "Todoist の自動化を断定の可能性" if td_bad else "Todoist 自動化 断定なし",
        )
    )
    return checks


def _commission_leakage_check(text: str) -> list[dict]:
    for mag in _COMMISSION_MAGNITUDES:
        for w in _windows_around(text, mag, radius=60):
            if any(v in w for v in _AFFILIATE_REASON_VOCAB):
                return [
                    _check(
                        "commission_leakage",
                        "fail",
                        f"affiliate 文脈で割合 {mag} が出現",
                    )
                ]
    for w in _windows_around(text, "報酬", radius=40):
        if re.search(r"\d{1,2}%", w):
            return [
                _check("commission_leakage", "warn", "『報酬』の近くに割合表現")
            ]
    return [_check("commission_leakage", "pass", "commission leakage なし")]


def _fairness_check(body: str, package: dict) -> list[dict]:
    checks: list[dict] = []
    primary = package["primary"]["subject_ref"]

    superlative_bad = any(
        any(s in w for s in _SUPERLATIVE)
        and not any(q in w for q in _QUALIFIER)
        for w in _windows_around(body, primary, radius=80)
    )
    checks.append(
        _check(
            "fairness_primary_superlative",
            "warn" if superlative_bad else "pass",
            f"{primary} に無根拠な最上級の可能性"
            if superlative_bad
            else "primary への無根拠な最上級なし",
        )
    )

    primary_tool = next(
        (t for t in package["comparison_tools"] if t["subject_ref"] == primary), None
    )
    has_limitations = primary_tool is not None and any(
        f["fact_key"] == "limitations" for f in primary_tool["usable_facts"]
    )
    if has_limitations:
        mentions = any(
            any(c in w for c in ("注意", "デメリット", "弱点", "制限", "留意"))
            for w in _windows_around(body, primary, radius=200)
        )
        checks.append(
            _check(
                "fairness_primary_caveat",
                "pass" if mentions else "warn",
                f"{primary} の注意点に言及"
                if mentions
                else f"{primary} の limitations があるが注意点の言及なし",
            )
        )
    return checks


def validate_draft_output(*, parsed: ParsedDraft, package: dict) -> dict:
    """editorial validation_report を返す。"""

    body = parsed.body_markdown
    combined = f"{parsed.meta_description}\n{body}"
    checks: list[dict] = []
    checks += _structural_checks(parsed, package)
    checks += _exaggeration_check(body)
    checks += _claim_safety_checks(body, package)
    checks += _commission_leakage_check(combined)
    checks += _fairness_check(body, package)

    has_fail = any(c["level"] == "fail" for c in checks)
    has_warn = any(c["level"] == "warn" for c in checks)
    overall = "fail" if has_fail else ("warn" if has_warn else "pass")
    return {
        "overall": overall,
        "promotion_eligible": not has_fail,
        "checks": checks,
    }
