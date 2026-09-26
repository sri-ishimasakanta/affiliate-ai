"""候補から、ローカルの下書き (``NoteDraft``) を決定的に組み立てる (公開しない)。

変換の型: 出来事 → 前提 (何のための話か) → 決めたこと / 試したこと → 観測した結果 → 学び →
まだ分からないこと。各文は ``Claim`` で、種類と出どころを持つ。本文にはリポジトリの内部名
(ファイルのパス・ID・commit) を出さず、根拠は下書きの ``claims`` と Markdown の「根拠」の
節 (公開の前に消す) にだけ置く。

この下書きは **編集前の素材** である。文章を整えるのは N1 の人の作業 (人が最終の題名と本文を
承認する)。
"""

from __future__ import annotations

from datetime import datetime

from app.social.note import safety
from app.social.note.catalog import CONTENT_TYPE_RULES
from app.social.note.models import Claim, EvidenceRef, NoteCandidate, NoteDraft
from app.social.note.sources import SourceBundle

POSITIONING = "AIで収益メディアをどこまで自動化できるか。実際に作りながら記録する。"
AUDIENCE = "AIで仕事や副業の仕組みを作ってみたい初心者 (リポジトリを見たことがない読者)"


def _fact_sentence(name: str, value, sources: SourceBundle) -> str | None:
    if name == "wordpress_featured_images":
        return (
            f"公開済みの記事のアイキャッチは {value} 本に付いている (WordPress を読み取って確認)。"
        )
    if name == "wordpress_taxonomy_matches_plan":
        return (
            "カテゴリは計画どおりの状態になっている (WordPress を読み取って確認)。"
            if value
            else None
        )
    if name == "threads_automatic_publication":
        return (
            "Threads は、人が承認した投稿案だけを自動で公開する設定で動いている。"
            if value
            else None
        )
    if name == "stock_maintenance_enabled":
        return (
            "投稿案の在庫の自動補充はまだ止めてあり、足りなくなったら手で補充している。"
            if value is False
            else None
        )
    if name == "make_tracked_articles":
        return f"計測用のリンクが入っている記事は {len(value)} 本。" if value else None
    if name == "last_completed_phase":
        return "作業の段階 (フェーズ) は、完了したものと次のものが機械で読める形で管理されている。"
    if name == "db_revision":
        head = sources.fact("db_at_code_head")
        if head and head.get("value") is True:
            return "データベースの構造は、コードの最新の定義と一致していることを毎回確かめている。"
    return None


def _clean(text: str, warnings: list[str]) -> str:
    out, found = safety.sanitize(text)
    warnings.extend(f"redacted {kind}" for kind in found)
    return out


def _reader_claims(claims: list[Claim], warnings: list[str]) -> list[Claim]:
    """本文に出す主張だけ (内部の言葉が残るものは本文から外し、根拠の一覧には残す)。"""

    kept = []
    for claim in claims:
        text, internal = safety.reader_facing(claim.text)
        if internal:
            warnings.append(
                f"kept out of the body (internal wording {internal}): {claim.text[:24]}"
            )
            continue
        kept.append(Claim(text, claim.kind, claim.evidence))
    return kept


def build_draft(candidate: NoteCandidate, topic: dict, sources: SourceBundle, *, now: datetime,
                corpus: dict[str, str] | None = None) -> NoteDraft:  # fmt: skip
    warnings: list[str] = []
    ev = {e.source: e for e in candidate.evidence}
    report_at = (sources.project_state or {}).get("generated_at")
    premise = [Claim(POSITIONING, "interpretation"), Claim(candidate.premise, "interpretation")]
    decided, results, lessons, unknown = [], [], [], []
    for did in topic["decisions"]:
        ref = ev.get(f"decision:{did}")
        entry = sources.decisions.get(did)
        if not ref or not entry:
            continue
        title = entry["title"].split(": ", 1)[-1]
        decided.append(Claim(f"{title}。", "decision", (ref,)))
        # 決めたことを本文に出せないなら、その理由だけを残さない (文脈の無い「理由」を作らない)
        if entry.get("rationale") and not safety.reader_facing(title)[1]:
            decided.append(Claim(f"理由: {entry['rationale']}。", "decision", (ref,)))
        if entry.get("resulting_state"):
            results.append(Claim(f"{entry['resulting_state']}。", "observed_fact", (ref,)))
        if entry.get("follow_up") and entry["follow_up"] not in ("—", "-"):
            unknown.append(Claim(f"次に残っていること: {entry['follow_up']}。", "planned", (ref,)))
    for name in topic["facts"]:
        ref, fact = ev.get(f"fact:{name}"), sources.fact(name)
        sentence = _fact_sentence(name, (fact or {}).get("value"), sources) if ref else None
        if sentence:
            results.append(Claim(sentence, "observed_fact", (ref,)))
    money = (sources.project_state or {}).get("monetization") or {}
    commissions = (money.get("commission_facts") or {}).get("count")
    if candidate.content_type == "milestone_recap" and commissions is not None:
        ref = EvidenceRef("fact:commission_facts", "live_observed",
                          "reports/project_state_latest.json", report_at)  # fmt: skip
        if commissions == 0:
            results.append(Claim("成果報酬 (手数料) の記録はまだ 0 件。"
                                 "収益の成果はまだ出ていない。",
                                 "observed_fact", (ref,)))  # fmt: skip
        else:
            results.append(Claim(f"成果報酬 (手数料) の記録は {commissions} 件。",
                                 "observed_fact", (ref,)))  # fmt: skip
    if topic.get("diagnostic") and sources.diagnostic:
        ref = ev.get("report:threads_performance_diagnostic")
        diag = sources.diagnostic
        count = diag.get("publication_count")
        results.append(Claim(f"診断に使えた投稿は {count} 本で、比べられる数がまだ少ない。",
                             "observed_fact", (ref,)))  # fmt: skip
        unknown.append(Claim("投稿の数が増えるまで、どの書き方が効くかは仮説のままにしておく。",
                             "hypothesis"))  # fmt: skip
    for path, _phrase in topic["docs"]:
        ref = ev.get(f"doc:{path}")
        if ref:
            results.append(Claim(
                "タスクを止めても子のプロセスが残ることがあり、残ったプロセスを確かめてから止め直した。"
                "ロックが古くなる 15 分を待ってから 1 回だけ再開した。",
                "observed_fact", (ref,)))  # fmt: skip
            break
    for text, did in topic["lessons"]:
        ref = ev.get(f"decision:{did}")
        if ref:
            lessons.append(Claim(f"{text}。", "interpretation", (ref,)))
    unknown.append(Claim("この記録は途中経過で、うまくいったかどうかの結論はまだ出していない。",
                         "interpretation"))  # fmt: skip

    headings = CONTENT_TYPE_RULES[candidate.content_type]["structure"]
    blocks = {"premise": premise, "decided": decided, "results": results, "lessons": lessons,
              "unknown": unknown}  # fmt: skip
    order = [("premise", headings[0]), ("decided", "決めたこと"), ("results", "結果"),
             ("lessons", "学び"), ("unknown", "まだ分からないこと")]  # fmt: skip
    evidence_claims = [c for role, _ in order for c in blocks[role]]
    blocks = {role: _reader_claims(items, warnings) for role, items in blocks.items()}
    sections = [
        {"heading": heading, "role": role,
         "paragraphs": [_clean(c.text, warnings) for c in blocks[role]]}
        for role, heading in order
        if blocks[role]
    ]  # fmt: skip
    claims = evidence_claims
    draft = NoteDraft(
        id=f"draft-{candidate.id.removeprefix('note-')}",
        candidate_id=candidate.id,
        content_type=candidate.content_type,
        working_title=_clean(candidate.working_title, warnings),
        premise=candidate.premise,
        audience=AUDIENCE,
        summary=_clean(candidate.premise, warnings),
        sections=sections,
        claims=claims,
        phases=candidate.phases,
        source_event_ids=candidate.source_event_ids,
        wordpress_refs=[],
        threads_refs=[],
        created_at=now.isoformat(timespec="seconds"),
    )
    commissions_known = bool(commissions)
    errors, claim_warnings = safety.check_claims(claims, commissions_known=commissions_known)
    body_errors, body_warnings = safety.check_body(draft.body, commissions_known=commissions_known)
    draft.errors = errors + body_errors
    draft.warnings = sorted(set(warnings + claim_warnings + body_warnings))
    if corpus:
        dup = safety.duplication(draft.body, corpus)
        draft.warnings.append(
            f"duplication {dup['verdict']} (max containment {dup['max_containment']} vs "
            f"{dup['closest_source']})"
        )
        if dup["verdict"] == "duplicate":
            draft.errors.append(f"duplicates {dup['closest_source']}")
    return draft


def render_markdown(draft: NoteDraft) -> str:
    lines = [
        "<!-- LOCAL DRAFT — NOT PUBLISHED. Human review required before any note publication. -->",
        "",
        f"# {draft.working_title}",
        "",
        f"_status: {draft.status} · type: {draft.content_type} · {draft.generator_version} · "
        f"content_hash {draft.content_hash[:12]}_",
        "",
        draft.body,
        "",
        "---",
        "",
        "## 根拠 (レビュー用。公開の前に消す)",
        "",
    ]
    for claim in draft.claims:
        refs = ", ".join(f"{e.source} ({e.authority}: {e.locator})" for e in claim.evidence) or "—"
        lines.append(f"- [{claim.kind}] {claim.text} — {refs}")
    lines += ["", "## 検査", ""]
    lines += [f"- error: {e}" for e in draft.errors] or ["- errors: none"]
    lines += [f"- warning: {w}" for w in draft.warnings]
    return "\n".join(lines) + "\n"
