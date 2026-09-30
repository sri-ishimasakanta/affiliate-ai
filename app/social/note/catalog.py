"""note の話題の型 (v1)。どの出来事が 1 本の記事になりうるかを、根拠の条件つきで並べる。

コミットごとに記事を作らない。まとまった出来事 (完了したフェーズ・長く効く決定・運用の
学び) だけを話題にする。各話題は、必要な根拠 (完了したフェーズ・決定の記録・報告の事実・
ドキュメントの言い回し) を持ち、発見のときに **今もあるか** を確かめる。

``lessons`` は解釈 (``interpretation``) で、必ず根拠の決定 / ドキュメントに結びつける。
``cannot_claim`` は、その話題で書いてはいけないこと。
"""

from __future__ import annotations

CONTENT_TYPE_RULES = {
    "build_log": {
        "purpose": "何を作ったか・なぜ作ったか",
        "minimum_evidence": "完了したフェーズ 1 つ以上 + そのフェーズの決定の記録 1 つ以上",
        "structure": ["前提", "作ったもの", "決めたこと", "結果", "学び", "まだ分からないこと"],
        "must_not_claim": "作った仕組みが収益を増やした、など観測していない効果",
    },
    "decision_note": {
        "purpose": "意味のある設計 / 運用の決定と、その理由",
        "minimum_evidence": "決定の記録 1 つ以上 (理由と結果の状態つき)",
        "structure": ["前提", "迷ったこと", "決めたこと", "理由", "結果", "学び"],
        "must_not_claim": "ほかの選択肢が悪い、という比較していない断定",
    },
    "experiment_result": {
        "purpose": "試したこと・起きたこと・変えたこと",
        "minimum_evidence": "試した記録と、その結果の記録 (報告・運用の記録)",
        "structure": [
            "前提",
            "試したこと",
            "起きたこと",
            "変えたこと",
            "学び",
            "まだ分からないこと",
        ],
        "must_not_claim": "少ないサンプルからの因果・一般化",
    },
    "milestone_recap": {
        "purpose": "完了したフェーズ / 節目の読みやすいまとめ",
        "minimum_evidence": "完了したフェーズ 2 つ以上 + 報告の事実",
        "structure": ["前提", "ここまでにやったこと", "今の状態", "まだ分からないこと", "次"],
        "must_not_claim": "収益の成果 (手数料の記録が 0 件のあいだ)",
    },
}

TOPICS = (
    {
        "key": "overview-automation-so-far",
        "type": "milestone_recap",
        "title": "AIで収益メディアをどこまで自動化できるか：ここまでの全体像",
        "premise": (
            "記事・画像・カテゴリ・Threads・運用の記録を、"
            "どこまで仕組みにできたかを実際の状態から振り返る"
        ),
        "phases": ("W1", "W2", "T4.3", "T6.1", "T7"),
        "decisions": (
            "w1-featured-images-complete",
            "w2-parent-plus-child",
            "threads-human-approval-required",
            "t7-project-state-complete",
            "project-state-docs-never-mutate-production",
        ),
        "facts": (
            "wordpress_featured_images",
            "wordpress_taxonomy_matches_plan",
            "threads_automatic_publication",
            "make_tracked_articles",
            "last_completed_phase",
        ),
        "docs": (),
        "why": "連載の入口。初めての読者が全体像をつかめる。成果は手数料 0 件を含めて正直に書ける",
        "duplication_risk": "low",
        "readability": 3,
        "lessons": (
            (
                "自動化は「全部を任せる」ことではなく、人が決める場所を先に決めることだった",
                "threads-human-approval-required",
            ),
            (
                "記録が本番と食い違ったときは、記録を直して本番は触らない",
                "project-state-docs-never-mutate-production",
            ),
        ),
    },
    {
        "key": "threads-approval-and-autopublish",
        "type": "build_log",
        "title": "Threadsの投稿を「人の承認」と「自動公開」に分けた仕組み",
        "premise": "AIが作った投稿案を、人が携帯で承認し、公開の時刻と間隔は仕組みが守る形にした",
        "phases": ("T3", "T4.1", "T4.2", "T4.3", "T6", "T6.1"),
        "decisions": (
            "threads-human-approval-required",
            "threads-no-fixed-times",
            "threads-no-catch-up-burst",
            "threads-stock-maintenance-off",
        ),
        "facts": ("threads_automatic_publication", "stock_maintenance_enabled"),
        "docs": (),
        "why": (
            "「AIに投稿させる」ときの安全の作り方は初心者にも役立つ。"
            "Threads の投稿そのものとは中身が違う"
        ),
        "duplication_risk": "low",
        "readability": 2,
        "lessons": (
            (
                "承認は「この内容なら出してよい」であって「今すぐ出す」ではない、と分けた",
                "threads-human-approval-required",
            ),
            ("止まっていた分をまとめて取り戻す連続投稿はしない", "threads-no-catch-up-burst"),
        ),
    },
    {
        "key": "project-state-system",
        "type": "build_log",
        "title": "プロジェクトの「今の状態」を自動でまとめる仕組みを作った",
        "premise": (
            "フェーズ・品質・本番の状態を 1 つの報告にまとめ、"
            "古いドキュメントとの食い違いも見つける"
        ),
        "phases": ("T7A", "T7B", "T7"),
        "decisions": (
            "project-state-live-outranks-stale-prose",
            "project-state-docs-never-mutate-production",
            "t7-project-state-complete",
            "project-state-on-demand",
        ),
        "facts": ("last_completed_phase", "db_revision"),
        "docs": (),
        "why": "AIと作業を引き継ぐときの「今どうなっているか」の作り方。似た記事が少ない",
        "duplication_risk": "low",
        "readability": 2,
        "lessons": (
            ("観測した本番の状態は、古い文章より強い", "project-state-live-outranks-stale-prose"),
            (
                "報告は必要なときに作れば足りる。スケジュールに載せるのは必要が出てから",
                "project-state-on-demand",
            ),
        ),
    },
    {
        "key": "taxonomy-parent-child",
        "type": "decision_note",
        "title": "WordPressのカテゴリを「親 + 子」にした理由",
        "premise": "25記事のカテゴリを作り直すときに、何を基準に分けたか",
        "phases": ("W2",),
        "decisions": (
            "w2-parent-plus-child",
            "w2-article-1-parent-only",
            "w2-article-25-ai",
            "wordpress-api-user-least-privilege",
        ),
        "facts": ("wordpress_taxonomy_matches_plan",),
        "docs": (),
        "why": "小さなサイトのカテゴリ設計と、権限を広げずに作業する判断は再利用しやすい",
        "duplication_risk": "low",
        "readability": 3,
        "lessons": (
            (
                "API の利用者の権限は広げず、カテゴリの作成だけは人が管理画面で行った",
                "wordpress-api-user-least-privilege",
            ),
        ),
    },
    {
        "key": "featured-images-25",
        "type": "milestone_recap",
        "title": "公開済みの25記事すべてにアイキャッチを付けるまで",
        "premise": "画像の無い記事をなくすために、生成・文字入れ・人の確認をどう組んだか",
        "phases": ("W1",),
        "decisions": ("w1-featured-images-complete",),
        "facts": ("wordpress_featured_images",),
        "docs": (),
        "why": "目に見える成果で、手順も具体的。ただし「画像でアクセスが増えた」とは書けない",
        "duplication_risk": "low",
        "readability": 3,
        "lessons": (
            (
                "画像は 5 バッチに分け、人が目で見て承認してから本番に入れた",
                "w1-featured-images-complete",
            ),
        ),
    },
    {
        "key": "worker-restart-lesson",
        "type": "experiment_result",
        "title": "常駐プログラムを止めたつもりが止まっていなかった話",
        "premise": "タスクを止めても子のプロセスが残り、ロックが古くなるのを待って再開した",
        "phases": ("T6.1",),
        "decisions": (),
        "facts": (),
        "docs": (
            (
                ("docs/operations/threads-proposal-stock.md"),
                "`Stop-ScheduledTask` は起動した `cmd.exe` しか",
            ),
            ("docs/operations/threads-proposal-stock.md", "最後の heartbeat から\n15 分経ってから"),
        ),
        "why": "運用の小さな失敗と手順の直し方。具体的で再現しやすい",
        "duplication_risk": "low",
        "readability": 2,
        "lessons": (),
    },
    {
        "key": "threads-small-sample",
        "type": "experiment_result",
        "title": "Threadsの成績は、まだ結論を出さない：少ない投稿数の扱い方",
        "premise": "数本の投稿の数字から傾向を決めつけず、比べられる条件がそろうまで待つ",
        "phases": ("T5", "T5.1"),
        "decisions": (),
        "facts": (),
        "docs": (),
        "diagnostic": True,
        "why": "数字を急いで解釈しない、という判断そのものに価値がある。結果は仮説として書く",
        "duplication_risk": "medium",
        "readability": 2,
        "lessons": (),
    },
    {
        "key": "growth-proposals-need-their-own-approval",
        "type": "build_log",
        "title": "AIが次の打ち手を提案しても、承認だけでは記事を書き換えない仕組み",
        "premise": (
            "分析が「この記事を直す」「この語で書く」と提案しても、"
            "書き込みは別の承認と手順を通るようにした"
        ),
        "phases": ("C10",),
        "decisions": ("c10-growth-approval-never-applies", "c10-discovery-candidate-not-keyword"),
        "facts": (),
        "docs": (
            ("docs/operations/site-growth-operations.md", "never applies anything"),
            ("docs/operations/site-growth-operations.md", "There is no approve-all entry point."),
        ),
        "why": "AIの提案をそのまま実行しない設計は、自動化を始める初心者にそのまま役立つ",
        "duplication_risk": "low",
        "readability": 2,
        "lessons": (
            (
                "「提案を認める」ことと「書き込みを認める」ことは別の判断として分けた",
                "c10-growth-approval-never-applies",
            ),
            (
                "見つけた語はすぐ使わず、人が 1 つずつ選ぶ",
                "c10-discovery-candidate-not-keyword",
            ),
        ),
    },
    {
        "key": "alerts-only-when-action-needed",
        "type": "decision_note",
        "title": "監視の通知は「行動が要るときだけ」にした理由",
        "premise": "毎日の点検の結果を全部知らせるのをやめ、直す必要があるときだけ知らせる形にした",
        "phases": ("C10",),
        "decisions": ("c10-health-actionable-only",),
        "facts": (),
        "docs": (("docs/operations/site-growth-operations.md", "auto-resolved"),),
        "why": "通知が多すぎて読まれなくなる問題は、小さな運用でもよく起きる",
        "duplication_risk": "low",
        "readability": 3,
        "lessons": (
            (
                "同じ問題は 1 つの通知にまとめ、直ったら自動で閉じる",
                "c10-health-actionable-only",
            ),
        ),
    },
)
