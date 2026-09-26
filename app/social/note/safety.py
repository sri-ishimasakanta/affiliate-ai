"""note の下書きの安全の検査 (pure)。

- 秘密・内部の詳細を伏せる: T7 の ``redaction.redact_text`` (token・Bearer / Basic・URL の
  認証情報・``/go/``) に加えて、ローカルの絶対パス・メールアドレス・端末名・長い digest・
  承認や capability の URL。
- 主張の検査: 観測した事実と決定には出どころが要る。収益の主張は、手数料 (commission) の
  記録があるときだけ。因果の言い切りは警告。煽りの言葉は警告。
- 重複の検査: WordPress の記事・Threads の投稿・内部のドキュメントと、文字の 3-gram で
  重なりを比べる (下書きがそれらの写しになっていないか)。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

from app.project_state.redaction import redact_text
from app.social.note.models import SOURCED_KINDS, Claim

_PATTERNS = (
    (re.compile(r"\b[A-Za-z]:\\[^\s`'\"<>|]+"), "[local path]"),
    (re.compile(r"(?<![\w.])/(?:Users|home)/[^\s/]+[^\s`'\"<>]*"), "[local path]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\bDESKTOP-[A-Z0-9]+\b"), "[host]"),
    (re.compile(r"https?://\S*(?:session|capability|review|token|sig)\S*", re.I), "[url]"),
    (re.compile(r"\b[0-9a-f]{32,}\b"), "[digest]"),
    (re.compile(r"\b(?:owner_token|capability_digest|capability_binding)\S*"), "[internal]"),
)
REVENUE_PATTERNS = re.compile(
    r"収益が(?:伸び|増え|出)|売上が|稼げ|儲か|報酬が(?:発生|入)|成約|収益化に成功|月\s*\d+\s*万"
)
CAUSAL_PATTERNS = re.compile(
    r"おかげで|によって(?:伸び|増え|改善)|効果があった|効果が出た|で伸びた"
)
HYPE_WORDS = ("神ツール", "絶対に", "革命的", "圧倒的に", "誰でも簡単に", "必ず儲かる", "爆伸び")


# 読み手に意味の無い内部の言葉 (フェーズの記号・記事の内部 ID・CLI の flag・内部の項目名)。
INTERNAL_WORDING = re.compile(
    r"(?<![A-Za-z0-9])[TWCN]\d+(?:\.\d+)?[A-C]?(?![A-Za-z0-9])|--[a-z][a-z-]+"
    r"|\barticle \d+\b|\bmedia \d+\b"
    r"|roadmap|next_phase|last_completed_phase|project_state|decision-log"
)
_INTERNAL_PARENS = re.compile(
    r"\s*[(（][^()（）]*(?:[TWCN]\d+(?:\.\d+)?|article \d+)[^()（）]*[)）]"
)


def reader_facing(text: str) -> tuple[str, list[str]]:
    """内部の言葉を含む括弧書きを落とし、残った内部の言葉の一覧を返す (空なら本文に使える)。"""

    cleaned = _INTERNAL_PARENS.sub("", text)
    return cleaned, sorted(set(INTERNAL_WORDING.findall(cleaned)))


def sanitize(text: str) -> tuple[str, list[str]]:
    """伏せた文章と、伏せた種類の一覧。"""

    found = []
    out = redact_text(text)
    if out != text:
        found.append("secret-like value")
    for pattern, label in _PATTERNS:
        out, count = pattern.subn(label, out)
        if count:
            found.append(label.strip("[]"))
    return out, found


def check_claims(claims: Iterable[Claim], *, commissions_known: bool) -> tuple[list, list]:
    """(errors, warnings)。errors があれば review_ready にできない。"""

    errors, warnings = [], []
    for claim in claims:
        if claim.kind in SOURCED_KINDS and not claim.evidence:
            errors.append(f"unsourced {claim.kind}: {claim.text[:40]}")
        if REVENUE_PATTERNS.search(claim.text) and not commissions_known:
            errors.append(f"unsupported revenue claim: {claim.text[:40]}")
        if CAUSAL_PATTERNS.search(claim.text) and claim.kind == "observed_fact":
            warnings.append(f"causal wording needs evidence: {claim.text[:40]}")
    return errors, warnings


def check_body(body: str, *, commissions_known: bool) -> tuple[list, list]:
    errors, warnings = [], []
    if REVENUE_PATTERNS.search(body) and not commissions_known:
        errors.append("unsupported revenue claim in body")
    for word in HYPE_WORDS:
        if word in body:
            warnings.append(f"hype word: {word}")
    return errors, warnings


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s\W_]+", "", text)


def shingles(text: str, size: int = 3) -> set[str]:
    norm = _normalize(text)
    return {norm[i : i + size] for i in range(max(0, len(norm) - size + 1))}


def containment(text: str, other: str) -> float:
    """``text`` の 3-gram のうち ``other`` にもある割合 (0〜1)。"""

    mine = shingles(text)
    return round(len(mine & shingles(other)) / len(mine), 3) if mine else 0.0


DUPLICATE = 0.5
SIMILAR = 0.3


def duplication(body: str, corpus: Mapping[str, str]) -> dict:
    """一番重なる出どころと、その割合。``duplicate`` / ``similar`` / ``distinct``。"""

    best_source, best = None, 0.0
    for source, text in corpus.items():
        score = containment(body, text or "")
        if score > best:
            best_source, best = source, score
    verdict = "duplicate" if best >= DUPLICATE else "similar" if best >= SIMILAR else "distinct"
    return {"verdict": verdict, "max_containment": best, "closest_source": best_source}
