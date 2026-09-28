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
        # 以前の決定 threads-stock-maintenance-off (無効のまま) は、決定の記録に歴史として残る。
        "id": "threads-stock-maintenance-enabled",
        "area": "threads",
        "decision": (
            "常駐 worker の提案の在庫の保守 (--maintain-proposal-stock) を 2026-09-26 に有効にした"
        ),
        "rationale": (
            "有効にする前の条件 (携帯の承認ページの本文表示を本物の承認依頼で確かめる) を満たした。"
            "manual provider なので、依頼は自動で出るが、答え (response.json) は人が作る"
        ),
        "evidence": ["docs/operations/threads-proposal-stock.md"],
        "phrase": "**有効 (2026-09-26 20:25 JST から)**",
        "resulting_state": (
            "worker pid 4840 が maintain_proposal_stock つきで起動。最初の保守で依頼 1 件"
        ),
        "follow_up": "答え待ちの依頼に答える運用 (72 時間で stale)",
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
    {
        "id": "t7-project-state-complete",
        "area": "project-state",
        "decision": "T7 (Autonomous Project State) は完了。次のフェーズは N0",
        "rationale": (
            "完了の条件 (live / offline・JSON と Markdown・provenance・drift・strict・伏せ字・"
            "比較・決定の記録・roadmap・次の行動の順) がそろった。運用の警告は完了を止めない"
        ),
        "evidence": ["docs/operations/project-state.md", "7edc394", "85c40ce"],
        "phrase": "T7 (Autonomous Project State) は完了",
        "resulting_state": "roadmap: last_completed_phase T7、next_phase N0 (まだ始めていない)",
        "follow_up": "N0 を始めるときに、報告を --strict で作り、前の報告と --compare で比べる",
    },
    {
        "id": "project-state-on-demand",
        "area": "project-state",
        "decision": (
            "プロジェクトの状態の報告は必要なときに作る (on-demand)。"
            "自動のスケジュールにはまだ載せない"
        ),
        "rationale": (
            "派生の報告を新しくするためだけに C8 / スケジューラの構成を変えない。"
            "具体的な運用の必要が出たときに考え直す"
        ),
        "evidence": ["docs/operations/project-state.md"],
        "phrase": "プロジェクトの状態の報告は必要なときに作る (on-demand)",
        "resulting_state": (
            "スケジュールされたタスクは作らない。報告は generation_policy を出すだけ"
        ),
        "follow_up": None,
    },
    {
        "id": "runtime-policy-notes-match-values",
        "area": "threads/policy",
        "decision": "実行の設定の中の説明の文は、動作を決める値と食い違ってはいけない",
        "rationale": (
            "automatic_publication.note が「Committed disabled」のまま enabled=true と食い違って"
            "いた。note は動作を決めないことを確かめてから、文だけを直した"
        ),
        "evidence": ["docs/operations/project-state.md"],
        "phrase": "実行の設定の中の説明の文は、動作を決める値と食い違ってはいけない",
        "resulting_state": "note は今の状態 (有効) を述べる。値は変えていない",
        "follow_up": None,
    },
    {
        "id": "t6-1-mobile-render-observed",
        "area": "threads/approvals",
        "decision": (
            "T6.1: 携帯の承認ページの本文表示を、本物の承認依頼で実機で確かめた (提案 #10)"
        ),
        "rationale": (
            "デプロイの時点ではテストでだけ確かめていた。在庫の保守を有効にする前の条件だった"
        ),
        "evidence": ["docs/operations/threads-proposal-stock.md"],
        "phrase": "確認済み (2026-09-26、人が実機で確認)",
        "resulting_state": "「投稿される本文」が表示され、承認・却下のボタンも通常どおり",
        "follow_up": "在庫の保守の有効化 (ランチャーのフラグ + worker の再起動) は人が行う",
    },
    {
        "id": "threads-auto-generation-luna",
        "area": "threads/generation",
        "decision": (
            "Threads の投稿案の主な生成器は OpenAI の GPT-5.6 Luna (Responses API・Structured "
            "Outputs)。出力は信用せず、決定的な検査と人の承認を必須のままにする"
        ),
        "rationale": (
            "日々の人の作業を承認と却下だけにする。失敗・鍵なし・上限の後は manual の依頼に戻す"
        ),
        "evidence": ["docs/operations/threads-proposal-stock.md"],
        "phrase": "**GPT-5.6 Luna を投稿案の主な生成器にする**",
        "resulting_state": (
            "実装済み。本番の有効化 (鍵・費用・設定・worker の再起動) は人の確認点"
        ),
        "follow_up": "本番の確認点: 鍵と月の上限を用意し、最初の実際の生成を人が確かめる",
    },
    {
        "id": "threads-conversation-hooks",
        "area": "threads/generation",
        "decision": (
            "Threads の投稿案は、それだけで役に立つまま、約 5 本に 4 本で"
            "自然な会話のきっかけを持つ "
            "(none・question・choice・experience・opinion を決定的に割り当てる)"
        ),
        "rationale": (
            "Threads は会話の場。反応を釣る言い回しにせず、話題から出るきっかけだけにする。"
            "表示や到達が増えるとは主張せず、観測で測る。少ない数から割合を自動で変えない"
        ),
        "evidence": ["docs/operations/threads-proposal-stock.md"],
        "phrase": "**目標: 約 5 本に 4 本がきっかけ付き",
        "resulting_state": (
            "実装済み (migration なし)。T6.3 より前の提案は legacy。"
            "本番の新しい prompt の最初の生成は"
            "人の確認点"
        ),
        "follow_up": "本番の確認点: 次の新しい依頼で、きっかけつきの実際の生成を人が確かめる",
    },
    {
        "id": "threads-content-quality-t631",
        "area": "threads/generation",
        "decision": (
            "T6.3.1: Threads の投稿は 1 投稿 1 要点。本文は目安 280〜360 字・420 字を超えたら"
            "書き直し。金額は 2 つまで。最近の話題 (製品・数値・軸) を繰り返さない"
        ),
        "rationale": (
            "#12 は本文 322 字に金額 4 つと軸 4 つを詰め、最近の案と同じ料金を繰り返した。"
            "完全一致の検査では見つからない。事実の検査と人の承認は変えない"
        ),
        "evidence": ["docs/operations/threads-proposal-stock.md"],
        "phrase": "## 投稿の質 (T6.3.1)",
        "resulting_state": "実装済み (migration なし)。本番の最初の質の確認は人の確認点",
        "follow_up": "本番の質の確認: 次の新しい依頼の実際の出力を人が確かめる",
    },
    {
        "id": "threads-generation-policy-t631a",
        "area": "threads/generation",
        "decision": (
            "T6.3.1a: 自動生成の呼び出しを 1 回ずつ記録する (1 回目を上書きしない・書き直しの理由の"
            " ID)。計画の link_mode を schema と検査で拘束する。"
            "本文の長さの方針は 280〜360 字だけ。重なりの判断を記録する"
        ),
        "rationale": (
            "T6.3.1 の確認で、#14・#15 の書き直しの元の出力と理由が残らず、link none の依頼 9 件中"
            " 7 件が article で返り、prompt に古い目安 380 が残っていた。本番で重なりを止めた例は"
            "まだ無い (作らない)"
        ),
        "evidence": ["docs/operations/threads-proposal-stock.md"],
        "phrase": "## 方針を守らせる (T6.3.1a)",
        "resulting_state": (
            "実装済み (migration なし)。閾値・計画のリンクの割合は同じ。本番の worker は"
            " 79e831c のまま (切り替えは人の確認点)"
        ),
        "follow_up": (
            "本番の切り替え: worker を新しい commit で再起動し、次の自然な依頼の記録を確かめる"
        ),
    },
    {
        "id": "threads-quality-t631-complete",
        "area": "threads/generation",
        "decision": (
            "T6.3.1 は完了。T6.3.1a の方針は本番で動いている。本番で重なりを止めた例が無いことは"
            "止めない観測項目として残す"
        ),
        "rationale": (
            "2026-09-28 の最初の自然な依頼 (提案 #22) で、link_mode の拘束・呼び出しごとの記録・"
            "自然に起きた書き直しの記録・重なりの記録を本番の記録で確かめた"
        ),
        "evidence": ["docs/operations/threads-proposal-stock.md"],
        "phrase": "### 本番での確認 (T6.3.1 完了、2026-09-28)",
        "resulting_state": "T6.3.1 complete。重なりを本番で止めた例はまだ無い (作らない)",
        "follow_up": "本番で自然に重なりを止めたら記録する",
    },
    {
        "id": "threads-topic-tag-t632",
        "area": "threads/publication",
        "decision": (
            "T6.3.2: 記事から作る通常の投稿は、公式の topic_tag でいつも \"AI Threads\" を付けて"
            "公開する。アカウントを育てる投稿には付けない。トピックが断られたら出し直さない"
        ),
        "rationale": (
            "人の決定。トピックは本文ではなくメタデータなので、本文・hash・500 字は変わらない。"
            "トピックなしの通常の投稿を出さないために、断られたら閉じる"
        ),
        "evidence": ["docs/operations/threads-autopublish.md"],
        "phrase": "## 固定トピック (T6.3.2)",
        "resulting_state": (
            "実装済み (migration なし)。\"AI Threads\" が本番で受け入れられるかは本番の確認点"
        ),
        "follow_up": "本番の確認: worker を新しい commit で再起動し、次の通常の公開を確かめる",
    },
    {
        "id": "threads-localization-t64-complete",
        "area": "threads/presentation",
        "decision": (
            "T6.4 は完了。日本語の表示を worker と中継の両方で本番に出し、人が携帯で確かめた"
        ),
        "rationale": (
            "投稿案 #27 の本物の承認リンクで日本語の欄と警告を人が確かめ、配備の後の同期も通った"
        ),
        "evidence": ["docs/operations/threads-localization.md"],
        "phrase": "**T6.4 は完了。**",
        "resulting_state": "T6.4 complete。次は T6.5 (Threads Trend Intelligence)",
        "follow_up": "T6.3.3b の Growth のトピックの本番の確認は、次の自然な Growth Post で",
    },
    {
        "id": "threads-growth-topic-t633b",
        "area": "threads/publication",
        "decision": (
            "T6.3.3b: 次の Growth Post から topic_tag \"インサイト祭り\" を付ける。"
            "記事は \"AI Threads\" のまま。前へ進むだけ (#25 は変えない)。"
            "断られたら Growth の枠だけを止める"
        ),
        "rationale": (
            "人の決定。トピックは本文ではなくメタデータなので、本文・hash・文字数は変わらない"
        ),
        "evidence": ["docs/operations/threads-growth-posts.md"],
        "phrase": "## Growth Post のトピック (T6.3.3b)",
        "resulting_state": (
            "実装済み。本番の Growth のトピックの受け入れは本番の確認点 (pending_canary)"
        ),
        "follow_up": (
            "本番の確認: worker の再起動の後の、次の自然な Growth Post で受け入れを確かめる"
        ),
    },
    {
        "id": "threads-trend-intelligence-t65ab",
        "area": "threads/analytics",
        "decision": (
            "T6.5A-B: 自分の投稿の特徴と、外の Threads の読むだけの観察の土台を作る。"
            "Playwright を第一の候補にし、読むだけ・小さなパイロットから・社会的な操作なし・"
            "自動の最適化なし"
        ),
        "rationale": (
            "伸びている形を知るには観察が要るが、アカウントの操作や少ない数からの自動調整は"
            "危ない。観察の表は自分の提案・公開の表と分け、生成には何も戻さない"
        ),
        "evidence": ["docs/operations/threads-trend-intelligence.md"],
        "phrase": (
            "**読むだけ。** いいね・返信・フォロー・再投稿・引用・DM・投稿・"
            "アカウントの変更はしない"
        ),
        "resulting_state": (
            "実装済み。migration 2cfa0ccb2059 は 2026-09-28 に本番へ適用済み。"
            "For You の selectors を確認し、保存つきの 5 件のパイロットを行った"
        ),
        "follow_up": (
            "本文に混ざる画面の部品 (「1/2」・「meta.ai」) と、保存されなかった 5 番目の投稿を"
            "直してから、ほかの画面 (検索・トレンド・カスタムフィード・アカウント) を"
            " dry-run で確かめる"
        ),
    },
    {
        "id": "threads-localization-t64",
        "area": "threads/presentation",
        "decision": (
            "T6.4: 人に見せる Threads の画面・メール・報告を日本語にする。"
            "内部の値 (状態・理由の ID・enum・ログ) は変えず、"
            "表示の層 (labels_ja) だけが日本語にする"
        ),
        "rationale": (
            "携帯の承認画面に英語の警告がそのまま出ていた。振る舞いを変えずに、人が読めるようにする"
        ),
        "evidence": ["docs/operations/threads-localization.md"],
        "phrase": "## 仕組み: 内部の値はそのまま、表示の層で日本語にする",
        "resulting_state": "実装済み。本番は worker の再起動と中継の配備 (別の確認点) で効く",
        "follow_up": "本番への展開: worker の再起動 → 中継 (WordPress) の配備は人の許可のもとで",
    },
    {
        "id": "threads-growth-t633-complete",
        "area": "threads/generation",
        "decision": "T6.3.3 は完了。Growth Post の足し分の枠を本番で確かめ、人が表示を確かめた",
        "rationale": (
            "公開 20 (#25) が記事の間隔を変えずに出て、"
            "人がアプリでトピックなし・リンクなしを確かめた"
        ),
        "evidence": ["docs/operations/threads-growth-posts.md"],
        "phrase": "**人の目の確認 (2026-09-28)**",
        "resulting_state": "T6.3.3 complete。次は T6.4 (日本語の表示)",
        "follow_up": "フォロー返しは人が手で続ける",
    },
    {
        "id": "threads-growth-lane-t633a",
        "area": "threads/publication",
        "decision": (
            "T6.3.3a: Growth Post は記事の 120 分の間隔と 1 回 1 本の枠を使わない足し分の枠で"
            "公開する。記事と同じ評価の中か、承認の後の評価で出す。1 日 1 本・失敗は記事から分ける"
        ),
        "rationale": (
            "人の決定。#25 は記事の queue の後ろで 120 分ずつ待つ並びになり、当日中に出られない"
            "おそれがあった。記事の本数と間隔は変えない"
        ),
        "evidence": ["docs/operations/threads-growth-posts.md"],
        "phrase": "## 足し分の公開の枠 (T6.3.3a)",
        "resulting_state": "実装済み。本番の worker は再起動で読む (人の確認点)",
        "follow_up": "本番の切り替え: worker を再起動し、#25 (今日の分なら) で枠を確かめる",
    },
    {
        "id": "threads-topic-tag-t632-complete",
        "area": "threads/publication",
        "decision": (
            "T6.3.2 は完了。本番の API が topic_tag \"AI Threads\" を受け入れ、人が Threads の"
            "アプリで表示を確かめた"
        ),
        "rationale": (
            "2026-09-28 の最初の通常の投稿 (提案 #21 → 公開 18) で、トピック付きの作成が成功し、"
            "本文は承認されたまま。人の目の確認を得た"
        ),
        "evidence": ["docs/operations/threads-autopublish.md"],
        "phrase": "### 本番での確認 (T6.3.2 完了、2026-09-28)",
        "resulting_state": "T6.3.2 complete。記事の投稿は AI Threads、Growth Post はトピックなし",
        "follow_up": "T6.3.3 の本番での有効化 (人の許可のもと)",
    },
    {
        "id": "threads-growth-posts-t633",
        "area": "threads/generation",
        "decision": (
            "T6.3.3: 記事の投稿に足して、JST の 1 日に 1 本まで Growth Post を用意する "
            "(取り戻さない・フォロワー 100 人が目標・人の承認・トピックなし・リンクなし)。"
            "目標に届いたら止まり、次の目標は人が決める"
        ),
        "rationale": (
            "人の決定。フォロー返しは人が手で行う。記事の本数・在庫・計画・公開の規則は変えない。"
            "記事を持たない投稿のために source_article_id を NULL にできるようにした (migration)"
        ),
        "evidence": ["docs/operations/threads-growth-posts.md"],
        "phrase": "# Threads の Growth Post (T6.3.3)",
        "resulting_state": (
            "実装済み。本番では未使用 (migration c4d2e8f1a9b3 は未適用、worker のフラグなし)。"
            "T6.3.2 の本番の確認の後に、人が有効にする"
        ),
        "follow_up": "本番への展開: migration の適用 → T6.3.2 の確認 → フラグを足して再起動",
    },
    {
        "id": "note-channel-build-diary",
        "area": "note",
        "decision": (
            "N0: note は開発日誌 (作りながら記録する) のチャネル。WordPress の記事や作業ログの"
            "写しにしない"
        ),
        "rationale": (
            "WordPress は検索と収益化、Threads は見つけてもらう入口。note は決めたこと・試行錯誤・"
            "結果・学びを初心者が読める形で残す"
        ),
        "evidence": ["docs/operations/note-channel.md"],
        "phrase": "開発日誌 (作りながら記録する)",
        "resulting_state": "N0 はローカルの土台 (候補・下書き・安全の検査) だけ。公開していない",
        "follow_up": "N1: 人が 1 本を選び、題名と本文を承認して、人の手で公開する",
    },
    {
        "id": "note-human-review-required",
        "area": "note",
        "decision": "note の公開は当面すべて人の確認つき。自動公開は作らない",
        "rationale": (
            "元の出来事を承認しても、記事の題名・本文・リンク・画像・公開の操作を承認したことには"
            "ならない。承認は本文の hash に結びつける"
        ),
        "evidence": ["docs/operations/note-channel.md"],
        "phrase": "公開は当面すべて人の確認つき",
        "resulting_state": "published は公開の証拠 (URL) が無ければ付けられない",
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
