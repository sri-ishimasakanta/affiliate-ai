"""長く効く決定 (リポジトリの記録で裏づけられるものだけ)。

各項目は根拠のファイルと、そのファイルに実際にある短い言い回し (``phrase``) を持つ。報告を
作るときに、根拠のファイルにその言い回しが **今もある** ことを確かめ、無ければ
``supported: False`` にして警告にする (記録が変わったのに決定だけが残る、を防ぐ)。
"""

from __future__ import annotations

from pathlib import Path

DECISIONS = (
    {
        "id": "w1-featured-images-complete",
        "area": "wordpress/featured-images",
        "decision": (
            "W1: 公開済みの 25 記事すべてに featured image を付けた (W1.4 の 4 + W1.5 の 21)"
        ),
        "rationale": "一覧・OGP・記事で画像の無い状態をなくす。人が 5 バッチを目で見て承認した",
        "evidence": ["docs/operations/featured-image-pipeline.md", "c48dbd6"],
        "phrase": "公開済みの 25 記事すべてに featured image が付いた",
        "resulting_state": "25/25 に featured image",
        "follow_up": "任意: media 99 (W1.4 の重複) の片付けは人が判断",
    },
    {
        "id": "w2-parent-plus-child",
        "area": "wordpress/taxonomy",
        "decision": "W2: カテゴリは「業務効率化 + 子 1 つ」、article 1 は業務効率化だけ",
        "rationale": "全記事が親に直接入る形を保ちつつ、話題ごとにたどれるようにする",
        "evidence": ["docs/operations/taxonomy-w2.md", "cdb4c6e"],
        "phrase": "方式は **親 + 子**",
        "resulting_state": "5 つの子 (7/7/5/3/2)、24 記事が親 + 子、article 1 は親だけ",
        "follow_up": None,
    },
    {
        "id": "w2-article-25-ai",
        "area": "wordpress/taxonomy",
        "decision": "article 25 (AI業務効率化) は AI・生成AI",
        "rationale": (
            "WordPress のカテゴリは話題で分ける。content_clusters.json の cluster B "
            "に合わせる必要はない"
        ),
        "evidence": ["docs/operations/taxonomy-w2.md", "app/wordpress/taxonomy_plan.py"],
        "phrase": "article 25 は **AI・生成AI**",
        "resulting_state": "article 25 は業務効率化 + AI・生成AI",
        "follow_up": None,
    },
    {
        "id": "w2-article-1-parent-only",
        "area": "wordpress/taxonomy",
        "decision": "article 1 (業務効率化ツールの総論) は親の業務効率化だけに置く",
        "rationale": "複数の話題にまたがる総論なので、子 1 つに入れると範囲を狭めてしまう",
        "evidence": ["docs/operations/taxonomy-w2.md"],
        "phrase": "article 1 は「業務効率化」だけ",
        "resulting_state": "article 1 のカテゴリは [業務効率化]",
        "follow_up": None,
    },
    {
        "id": "wordpress-modified-gmt-refresh-accepted",
        "area": "wordpress",
        "decision": (
            "W1 / W2 の書き込みで post の modified_gmt が新しくなる (Cocoon "
            "の更新日・サイトマップの lastmod も) ことを許容する"
        ),
        "rationale": "WordPress の仕組みで避けられない。日付を戻す・隠すことはしない",
        "evidence": [
            "docs/operations/featured-image-pipeline.md",
            "docs/operations/taxonomy-w2.md",
        ],
        "phrase": "人が許容済み",
        "resulting_state": "25 記事の更新日は W1.5 / W2 の適用日",
        "follow_up": None,
    },
    {
        "id": "wordpress-api-user-least-privilege",
        "area": "wordpress/security",
        "decision": (
            "WordPress の API の利用者は最小権限の author のままにする。"
            "カテゴリの作成のために権限を恒久的に広げない"
        ),
        "rationale": (
            "自動化の資格情報が漏れたときの影響を小さく保つ。カテゴリの作成はまれなので人が "
            "wp-admin で行う"
        ),
        "evidence": ["docs/operations/taxonomy-w2.md", "dea7f08"],
        "phrase": "利用者の権限は変えていない",
        "resulting_state": "API の利用者は author。W2 の子カテゴリは人が wp-admin で作った",
        "follow_up": (
            "新しいカテゴリが要るときも、人が wp-admin で作り、道具は一致を確かめて採用する"
        ),
    },
    {
        "id": "threads-stock-maintenance-off",
        "area": "threads",
        "decision": "常駐 worker の提案の在庫の保守 (--maintain-proposal-stock) は無効のまま",
        "rationale": "在庫づくりを自動で回す前に、人が運用の手順と携帯での承認を確かめる",
        "evidence": ["docs/operations/threads-proposal-stock.md"],
        "phrase": "**まだ有効にしていない**",
        "resulting_state": "提案づくりは手動 (collect-only / 明示の実行) のまま",
        "follow_up": "携帯での承認の表示を実際に確かめてから、在庫の運用の手順を人が決める",
    },
    {
        "id": "threads-no-fixed-times",
        "area": "threads",
        "decision": "Threads の公開・承認の通知に固定の時刻表を持たない",
        "rationale": "公開窓・通知窓と実際の公開からの間隔だけで決め、機械的な投稿時刻を避ける",
        "evidence": [
            "docs/operations/threads-worker.md",
            "docs/operations/threads-approval-digest.md",
        ],
        "phrase": "固定の時刻表",
        "resulting_state": "公開窓 07:00–23:00 JST・通知窓 08:00–21:00 JST・間隔の目安 120 分",
        "follow_up": None,
    },
    {
        "id": "threads-no-catch-up-burst",
        "area": "threads",
        "decision": "Threads は 1 サイクルで最大 1 本だけ公開し、止まっていた分をまとめて出さない",
        "rationale": "連続投稿で届きにくくなるのと、誤りの影響が広がるのを避ける",
        "evidence": ["docs/operations/threads-worker.md", "docs/operations/threads-autopublish.md"],
        "phrase": "1 回の評価サイクルで公開してよいのは最大 1 本",
        "resulting_state": "worker は 1 サイクルで 2 件目の公開が報告されたら止まる",
        "follow_up": None,
    },
    {
        "id": "threads-human-approval-required",
        "area": "threads/approvals",
        "decision": "Threads の提案は人が承認したものだけを公開する",
        "rationale": (
            "自動で作った文章をそのまま公開しない。承認は digest のメールと携帯の承認で行う"
        ),
        "evidence": ["docs/operations/threads-autopublish.md"],
        "phrase": "承認済み queue から自動で公開する",
        "resulting_state": "自動公開の対象は承認済みの提案だけ",
        "follow_up": None,
    },
    {
        "id": "project-state-live-outranks-stale-prose",
        "area": "project-state",
        "decision": ("T7B: 観測した本番の状態と実行の設定は、古い文章より強い (source precedence)"),
        "rationale": (
            "自動公開・中継の配備・DB の revision で、ドキュメントの見出しが本番と食い違っていた。"
            "どれを信じるかを決まった順で決める"
        ),
        "evidence": ["docs/operations/project-state.md"],
        "phrase": "観測した本番の状態と実行の設定は、古い文章より強い",
        "resulting_state": "古いドキュメントは stale_doc として出し、ドキュメントだけを直す",
        "follow_up": "policy の automatic_publication.note の古い説明は人が直す",
    },
    {
        "id": "project-state-docs-never-mutate-production",
        "area": "project-state",
        "decision": "T7B: ドキュメントの食い違いを理由に本番を変えない",
        "rationale": (
            "報告を緑にするために本番を変えると、正しい本番の状態を古い文章に合わせてしまう"
        ),
        "evidence": ["docs/operations/project-state.md"],
        "phrase": "ドキュメントの食い違いを理由に本番を変えない",
        "resulting_state": (
            "policy・worker・スケジューラ・DB・WordPress は変えず、ドキュメントを直した"
        ),
        "follow_up": None,
    },
    {
        "id": "make-tracking-articles-1-10-11",
        "area": "monetization",
        "decision": "Make の tracking は article 1・10・11 (article 1 は Make が主のプログラム)",
        "rationale": "DB の有効な target と mapping が根拠。指示の想定 (10・11 だけ) より強い",
        "evidence": ["docs/operations/project-state.md"],
        "phrase": "Make の tracking は article 1・10・11",
        "resulting_state": "3 記事が Make の tracking を持つ",
        "follow_up": None,
    },
    {
        "id": "threads-worker-lock-label-mode-plan",
        "area": "threads/worker",
        "decision": (
            "worker のロックの表示 mode=plan は正しい (核の唯一の mode。公開は能力で決まる)"
        ),
        "rationale": (
            "表示は人が読むためだけ。所有は owner_token、古さは heartbeat で判定する。"
            "表示のために worker を再起動しない"
        ),
        "evidence": ["docs/operations/threads-worker.md"],
        "phrase": "`mode=plan` と出る。これは正しい",
        "resulting_state": "報告は mode=plan を expected_difference として出す",
        "follow_up": None,
    },
)


def check_support(root: Path, decisions=DECISIONS, *, commit_exists=None) -> list[dict]:
    """各決定の根拠が今もリポジトリにあるか (ファイルの言い回し・commit)。"""

    rows = []
    for decision in decisions:
        problems = []
        files = [e for e in decision["evidence"] if "/" in e or e.endswith(".md")]
        commits = [e for e in decision["evidence"] if e not in files]
        texts = []
        for rel in files:
            path = root / rel
            if not path.exists():
                problems.append(f"{rel} is missing")
            else:
                texts.append(path.read_text(encoding="utf-8"))
        if files and not any(decision["phrase"] in text for text in texts):
            problems.append(f"phrase not found in {files}")
        if commit_exists is not None:
            problems += [f"commit {c} not found" for c in commits if not commit_exists(c)]
        rows.append(
            {
                **{k: v for k, v in decision.items() if k != "phrase"},
                "supported": not problems,
                "problems": problems,
            }
        )
    return rows
