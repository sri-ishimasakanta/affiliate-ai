"""知識の商品の候補 (N4。決定的・読むだけ)。

開発の記録をそのまま商品にしない。各候補は、このプロジェクトで **確かめた** 決定・完了した
フェーズ・運用のドキュメントの言い回しを根拠にした一般化で、誰の・どんな問題を・どうできる
ようにするかを持つ。根拠が今もあるかを発見のときに確かめる。サイト固有のもの (このサイトの
名前・アカウント・数字) は商品に入れない。

候補は優先度のためだけに並べる (点数で出す / 出さないを決めない)。商品にするのは人が決める。
"""

from __future__ import annotations

from pathlib import Path

from app.products.spec import list_products, resolve_source
from app.social.note.sources import SourceBundle

CANDIDATES = (
    {
        "id": "approval-gated-automation-kit",
        "title": "AI自動化に「人の承認の門」を組み込むためのチェックリストと設定例",
        "target_user": "AIで記事・投稿・運用を自動化し始めた、個人や小さなチームの運営者",
        "problem": "AIに任せる範囲が広がるほど、誤った書き込みや公開を止める場所が分からなくなる",
        "intended_outcome": "自動でよい作業・人が承認する作業・自動化しない作業を分け、"
                            "承認を内容の hash に結びつけて運用できる",
        "reusable": True,
        "assets": ("checklist", "config_example", "guide"),
        "evidence": (
            {"ref": "decision:threads-human-approval-required"},
            {"ref": "decision:c10-growth-approval-never-applies"},
            {"ref": "phase:C10"},
            {"ref": "doc:docs/operations/site-growth-operations.md",
             "phrase": "Autonomy classification (A–E)"},
        ),
    },
    {
        "id": "actionable-alerting-playbook",
        "title": "小さな運用の通知設計：行動が要るときだけ知らせる",
        "target_user": "サイトや自動化を 1 人で運用し、通知が多すぎて見なくなった人",
        "problem": "正常の通知や同じ障害の繰り返しで、本当に必要な通知が埋もれる",
        "intended_outcome": "警告にする条件・まとめ方・知らせ直しの間隔・自動の解決を決められる",
        "reusable": True,
        "assets": ("guide", "checklist"),
        "evidence": (
            {"ref": "decision:c10-health-actionable-only"},
            {"ref": "phase:C10"},
            {"ref": "doc:docs/operations/site-growth-operations.md", "phrase": "auto-resolved"},
        ),
    },
    {
        "id": "project-state-handoff-template",
        "title": "AIとの開発を引き継ぐための「今の状態」レポートの作り方",
        "target_user": "AIのコーディング支援と長く開発を続ける個人開発者",
        "problem": "セッションや担当が変わるたびに、どこまで終わって何が本番かが分からなくなる",
        "intended_outcome": "観測した事実を古い文章より優先する、引き継ぎのレポートの型を使える",
        "reusable": True,
        "assets": ("template", "guide"),
        "evidence": (
            {"ref": "phase:T7"},
            {"ref": "decision:project-state-live-outranks-stale-prose"},
            {"ref": "decision:project-state-docs-never-mutate-production"},
        ),
    },
    {
        "id": "small-sample-measurement-guide",
        "title": "少ないデータで結論を出さない：小さなメディアの計測の手引き",
        "target_user": "投稿や記事の数がまだ少なく、数字の読み方に迷っている運営者",
        "problem": "数本の結果から「効いた」「効かない」と決めつけ、判断を誤る",
        "intended_outcome": "標本の大きさを書き、比べられる条件がそろうまで仮説のまま扱える",
        "reusable": True,
        "assets": ("guide", "checklist"),
        "evidence": (
            {"ref": "phase:T5"},
            {"ref": "doc:docs/operations/threads-performance-analysis.md",
             "phrase": "本数が少ない間は仮説"},
        ),
    },
    {
        "id": "this-site-operations-runbook",
        "title": "このサイトの運用手順書",
        "target_user": "このサイトの運営者",
        "problem": "—",
        "intended_outcome": "—",
        "reusable": False,
        "assets": ("guide",),
        "evidence": ({"ref": "phase:C10"},),
    },
)


def discover(root: Path, sources: SourceBundle) -> list[dict]:
    existing = set(list_products(root))
    out = []
    for c in CANDIDATES:
        found, problems = 0, []
        for ref in c["evidence"]:
            digest, problem = resolve_source(ref, root, sources)
            if digest:
                found += 1
            else:
                problems.append(problem)
        eligible = c["reusable"] and found * 2 >= len(c["evidence"])
        out.append({**{k: c[k] for k in ("id", "title", "target_user", "problem",
                                         "intended_outcome", "reusable", "assets")},
                    "evidence_found": found, "evidence_required": len(c["evidence"]),
                    "problems": problems, "eligible": eligible,
                    "product_exists": c["id"] in existing,
                    "reason": ("site-specific: not a product (kept as an internal runbook)"
                               if not c["reusable"] else
                               "evidence present" if eligible else "evidence is too thin")})
    order = {True: 0, False: 1}
    out.sort(key=lambda c: (order[c["eligible"]], -c["evidence_found"], c["id"]))
    return out


__all__ = ["CANDIDATES", "discover"]
