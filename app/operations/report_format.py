"""運用レポートの text/plain レンダリング (C8.7)。

携帯で読めることを優先する:

- 1 行 1 事実。巨大な JSON をそのまま貼らない。
- 詳細が要るものは **id を出す** (run id / request id)。読み手はそれを使って
  ローカルの CLI を叩ける。
- 欠測は ``unavailable`` と書く。0 と書かない。

HTML は作らない (V1 は plain text のみ)。
"""

from __future__ import annotations

from app.services.operations_weekly_report_service import UNAVAILABLE, WeeklyReport

_SEP = "-" * 46


def weekly_subject(report: WeeklyReport) -> str:
    """件名 (接頭辞は :class:`EmailConfig` が付ける)。T6.4: 日本語。"""

    base = f"週次運用レポート {report.period_end}"
    return base if report.complete else f"{base}（不完全）"


def weekly_subject_severity(report: WeeklyReport) -> str:
    """不完全なときだけ WARNING を立てる。健全なら無印で送る。"""

    return "info" if report.complete else "warning"


#: T6.4: 実行の状態の見出し (内部の値は括弧で添える)。
_RUN_STATUS = {
    "succeeded": "成功",
    "failed": "失敗",
    "partial": "一部失敗",
    "running": "実行中",
    "skipped": "スキップ",
    "planned": "計画のみ",
}


def _run_status(value) -> str:
    if value is None:
        return _fmt(value)
    label = _RUN_STATUS.get(str(value))
    return f"{label}（{value}）" if label else str(value)


def render_weekly_report(report: WeeklyReport) -> str:
    """週次の運用レポート (T6.4: 見出しは日本語。節の名前は英語を括弧で残す)。"""

    from app.social.threads.labels_ja import format_jst

    data = report.as_dict()
    out: list[str] = []

    def section(title: str) -> None:
        out.extend(["", _SEP, title, _SEP])

    def kv(label: str, value, width: int = 16) -> None:
        out.append(f"{label:<{width}}: {_fmt(value)}")

    out.append(
        f"BizFluxLab 週次運用レポート（{data['period_start']} 〜 {data['period_end']}、"
        f"{data['reporting_timezone']}）"
    )
    out.append(f"作成日時        : {format_jst(data['generated_at'])}")
    if not data["complete"]:
        out.append("")
        out.append("*** このレポートは不完全である ***")
        for reason in data["incomplete_reasons"]:
            out.append(f"  - {reason}")

    threads = data.get("threads")
    if threads:
        section("Threads (THREADS)")
        out.extend(_threads_lines(threads, weekly=True))

    section("システム (SYSTEM)")
    system = data["system"]
    for label, key in (
        ("運用の実行", "operations_run_id"),
        ("プロファイル", "profile"),
        ("対象日", "effective_date"),
        ("ポリシーの版", "policy_version"),
        ("成功した手順", "step_succeeded"),
        ("失敗した手順", "step_failed"),
        ("スキップした手順", "step_skipped"),
    ):
        kv(label, system.get(key))
    kv("状態", _run_status(system.get("status")))
    kv("終了", format_jst(system.get("finished_at")) if system.get("finished_at") else None)
    for key, label in (
        ("failing_steps", "失敗した手順名"),
        ("skipped_steps", "スキップした手順名"),
        ("partial_steps", "一部だけの手順名"),
    ):
        if system.get(key):
            kv(label, ", ".join(system[key]))

    section("Search Console (SEARCH CONSOLE)")
    gsc = data["search_console"]
    kv("データの範囲", gsc.get("coverage_through"))
    kv("最新のデータ日", gsc.get("latest_observed_data_date"))
    kv("最後の取り込み", gsc.get("last_successful_import_at"))
    kv("期間の行数", gsc.get("rows_in_period"))
    kv("表示回数", gsc.get("impressions"))
    kv("クリック", gsc.get("clicks"))
    out.append(f"メモ: {gsc.get('note')}")

    section("GA4 (GA4)")
    ga4 = data["ga4"]
    kv("設定済み", ga4.get("configured"))
    kv("データの範囲", ga4.get("coverage_through"))
    kv("最新のデータ日", ga4.get("latest_observed_data_date"))
    kv("期間の行数", ga4.get("rows_in_period"))
    kv("セッション", ga4.get("sessions"))
    kv("自然検索セッション", ga4.get("organic_sessions"))
    kv("成熟度", ga4.get("maturity"))

    section("SEO 改善候補 (SEO / C6)")
    seo = data["seo"]
    kv("最新の実行", seo.get("latest_run_id"))
    kv("期間", seo.get("window"))
    kv("候補", seo.get("candidate_count"))
    kv("種類別", seo.get("by_type"))
    kv("優先度別", seo.get("by_priority"))
    kv("比べた実行", seo.get("compared_with_run_id"))
    kv("新規", seo.get("new"))
    kv("継続", seo.get("persisted"))
    kv("なくなった", seo.get("no_longer_present"))
    kv("優先度が上がった", seo.get("priority_increased"))
    kv("優先度が下がった", seo.get("priority_decreased"))
    if seo.get("no_longer_present_note"):
        out.append(f"メモ: {seo['no_longer_present_note']}")

    section("収益の改善候補 (REVENUE / C7)")
    rev = data["revenue"]
    kv("最新の実行", rev.get("latest_run_id"))
    kv("期間", rev.get("window"))
    kv("収益化した記事", rev.get("monetized_article_count"))
    kv("クリック (全体)", rev.get("raw_clicks"))
    kv("除外したクリック", rev.get("excluded_clicks"))
    kv("有効なクリック", rev.get("clean_clicks"))
    kv("計測の開始", rev.get("trusted_measurement_start_at"))
    kv("成果の行数", rev.get("commission_rows"))
    kv("候補", rev.get("candidate_count"))
    kv("種類別", rev.get("by_type"))
    kv("記事ごとの収益", rev.get("article_level_revenue"))
    if rev.get("attribution_note"):
        out.append(f"メモ: {rev['attribution_note']}")

    section("記事の変更 (CONTENT CHANGES / C9)")
    changes = data["content_changes"]
    kv("作った変更要求", changes.get("requests_created"))
    kv("承認", changes.get("approved"))
    kv("却下", changes.get("rejected"))
    kv("適用の試み", changes.get("applications_attempted"))
    kv("  成功", changes.get("applications_succeeded"))
    kv("  失敗", changes.get("applications_failed"))
    kv("承認待ち", changes.get("awaiting_approval"))
    for applied in changes.get("latest_applied") or []:
        out.append(
            f"  適用: 変更要求 {applied['change_request_id']} → 記事 "
            f"{applied['article_id']}（{applied['outcome']}、{format_jst(applied['attempted_at'])}）"
        )
    for effect in changes.get("effect_tracking") or []:
        out.append(
            f"  効果の追跡: 変更要求 {effect['change_request_id']} 記事 "
            f"{effect['article_id']} → {effect['maturity']} {effect['reasons']}"
        )
    out.append("メモ: 効果は因果を示さない (causal_claim=none)。改善したとは書かない。")

    section("記事の状態 (ARTICLE HEALTH)")
    health = data["article_health"]
    kv("公開中", health.get("published"))
    kv("下書き", health.get("drafts"))
    kv("収益化して公開中", health.get("monetized_published"))
    kv("URL の無い公開", health.get("published_without_url"))
    out.append(f"メモ: {health.get('indexability_note')}")

    section("アラート (ALERTS)")
    alerts = data["alerts"]
    kv("新しく開いた", alerts.get("newly_opened"))
    kv("続いている", alerts.get("persisted"))
    kv("期間中に解決", alerts.get("resolved_in_period"))
    kv("いま開いている", alerts.get("currently_open"))
    for alert in alerts.get("open_details") or []:
        out.append(
            f"  [{alert['severity']}] {alert['alert_type']}: {alert['title']} "
            f"（{alert['occurrence_count']} 回、最後 {format_jst(alert['last_seen_at'])}）"
        )

    if data["next_attention"]:
        section("次に見ること (NEXT ATTENTION)")
        for item in data["next_attention"]:
            out.append(f"  - {item}")

    out += [
        "",
        _SEP,
        "このメールは運用情報のみを含む。認証情報・tracking URL・/go/ token は含まない。",
    ]
    return "\n".join(out)


def _threads_lines(summary: dict, *, weekly: bool) -> list[str]:
    """T6.4: Threads のまとめ (日次・週次で共通)。成績の結論は出さない (T6.5)。"""

    from app.social.threads.labels_ja import METRIC_LABELS, format_jst

    lines = [
        f"通常投稿      : {summary.get('article_posts', 0)}件",
        f"Growth Post   : {summary.get('growth_posts', 0)}件",
        f"合計          : {summary.get('total_posts', 0)}件",
        "",
        f"承認待ち      : {summary.get('awaiting_approval', 0)}件",
        f"承認済み・公開待ち: {summary.get('approved_unpublished', 0)}件",
        f"公開失敗      : {len(summary.get('failed_publications') or [])}件",
        f"要確認        : {len(summary.get('needs_check_publications') or [])}件",
        "",
    ]
    total = summary.get("metrics_total") or {}
    observed = summary.get("observed_posts", 0)
    scope = "今週" if weekly else "今日"
    if observed:
        lines.append(f"{scope}公開した投稿の最新の指標（{observed}件の合計。保存済みの値）")
        for name in ("views", "likes", "replies", "reposts", "quotes", "shares"):
            lines.append(f"  {METRIC_LABELS[name]:<6}: {_fmt(total.get(name))}")
    else:
        lines.append(f"{scope}公開した投稿の指標: まだ取得していません")
    growth = summary.get("growth") or {}
    observation = growth.get("follower_observation")
    lines += ["", f"フォロワーの目標: {growth.get('follower_target')}人"]
    if observation:
        lines.append(
            f"フォロワー数  : {observation['followers_count']}人"
            f"（{format_jst(observation['observed_at'])} に取得）"
        )
    else:
        read = growth.get("follower_read") or {}
        reason = read.get("label") if isinstance(read, dict) else None
        suffix = f"（理由: {reason}）" if reason else ""
        lines.append(f"フォロワー数  : 取得できませんでした{suffix}")
    lines += ["", "対応が必要:"]
    attention = summary.get("needs_attention") or []
    if attention:
        lines.extend(f"  - {item}" for item in attention)
    else:
        lines.append("  なし")
    if summary.get("posts"):
        lines += ["", "公開した投稿:"]
        for post in summary["posts"]:
            kind = "Growth Post" if post["kind"] == "account_growth" else "通常記事"
            views = (post.get("metrics") or {}).get("views")
            lines.append(
                f"  {format_jst(post['published_at'])}  {kind}  提案 #{post['proposal_id']}"
                f"（公開 {post['publication_id']}、表示数 {_fmt(views)}）"
            )
    if weekly:
        lines += [
            "",
            "来週も自動で続くこと: 通常投稿の在庫の保守・承認のまとめ送り・承認済みの公開・"
            "Growth Post (1 日 1 本、承認が必要)",
        ]
    return lines


def render_threads_daily_summary(summary: dict) -> str:
    """T6.4: 今日の Threads 運用 (日本語。読むだけの材料から作る)。"""

    from app.social.threads.labels_ja import format_jst

    out = [
        f"今日の Threads 運用（{summary.get('period_end')}、JST）",
        f"作成日時: {format_jst(summary.get('generated_at'))}",
        "",
    ]
    out.extend(_threads_lines(summary, weekly=False))
    out += ["", "このまとめは読むだけで作っています。Threads・OpenAI には問い合わせていません。"]
    return "\n".join(out)


def render_daily_incident(*, run_summary: dict, alerts: list[dict], notes: list[str]) -> str:
    """日次のインシデント 1 通ぶん (同じ run の問題をまとめる)。T6.4: 日本語。"""

    out = [
        "BizFluxLab の日次運用で確認が必要です。",
        "",
        f"運用の実行      : {run_summary.get('operations_run_id')}",
        f"プロファイル    : {run_summary.get('profile')}",
        f"状態            : {_run_status(run_summary.get('status'))}",
        f"対象日          : {run_summary.get('effective_date')}",
        f"手順            : 成功 {run_summary.get('step_succeeded')}、失敗 "
        f"{run_summary.get('step_failed')}、スキップ {run_summary.get('step_skipped')}",
    ]
    for key, label in (
        ("failing_steps", "失敗した手順"),
        ("skipped_steps", "スキップした手順"),
        ("partial_steps", "一部だけの手順"),
    ):
        if run_summary.get(key):
            out.append(f"{label:<14}: {', '.join(run_summary[key])}")

    if alerts:
        out += ["", _SEP, f"対応が必要なアラート（{len(alerts)}件）", _SEP]
        for alert in alerts:
            out.append(f"[{alert['severity']}] {alert['alert_type']}: {alert['title']}")
            if alert.get("summary"):
                out.append(f"    {alert['summary']}")
            if alert.get("article_id"):
                out.append(f"    記事: {alert['article_id']}")
    if notes:
        out += ["", "メモ:"]
        out.extend(f"  - {note}" for note in notes)

    out += [
        "",
        "確かめる (PC で):",
        "  uv run python scripts/run_operations.py --profile daily",
        "  uv run python scripts/report_weekly_operations.py",
        "",
        "このメールは運用情報のみを含む。認証情報・tracking URL・/go/ token は含まない。",
    ]
    return "\n".join(out)


def _fmt(value) -> str:
    if value is None:
        return f"不明（{UNAVAILABLE}）"
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items()) or "(none)"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(none)"
    return str(value)


def render_approval_request_text(*, snapshot: dict, review_url: str, expires_at_local: str) -> str:
    """承認依頼メールの text/plain (C8.8)。

    **ワンクリック承認のリンクは載せない。** 載せるのはレビューページを開く導線
    だけで、決定はページ上の明示的な操作でしか起きない。
    """

    out = [
        "BizFluxLab の記事変更について、承認が必要です。",
        "",
        f"変更要求      : #{snapshot.get('subject_id')}",
        f"記事          : {snapshot.get('article_title') or snapshot.get('article_id')}",
        f"リンク先      : {snapshot.get('target_article_title') or '-'}",
        f"変更種別      : {snapshot.get('change_type')}",
        f"候補          : {snapshot.get('candidate_type')} ({snapshot.get('priority')})",
        f"提案          : v{snapshot.get('subject_version')} {snapshot.get('subject_hash_short')}",
        f"有効期限      : {expires_at_local}",
        "",
        "理由:",
        f"  {snapshot.get('rationale')}",
        "",
        "内容を確認する:",
        f"  {review_url}",
        "",
        "このリンクは内容を表示するだけで、開いても承認にはならない。",
        "承認・却下はページ上で明示的に操作したときだけ記録される。",
        "リンクは 1 回限りで、期限を過ぎると使えなくなる。他人に転送しないこと。",
    ]
    return "\n".join(out)


def render_approval_request_html(*, snapshot: dict, review_url: str, expires_at_local: str) -> str:
    """承認依頼メールの text/html (小さく保つ)。

    動的な値はすべてエスケープする。外部アセットは読み込まない。
    """

    from html import escape

    def e(value) -> str:
        return escape(str(value if value is not None else "-"), quote=True)

    return (
        '<div style="font-family:sans-serif;max-width:560px;line-height:1.6">'
        '<h2 style="font-size:18px">BizFluxLab: 記事変更の承認が必要です</h2>'
        f"<p>変更要求 <strong>#{e(snapshot.get('subject_id'))}</strong> "
        f"/ 提案 v{e(snapshot.get('subject_version'))} "
        f"<code>{e(snapshot.get('subject_hash_short'))}</code></p>"
        '<table cellpadding="4" style="border-collapse:collapse;font-size:14px">'
        f"<tr><td>記事</td><td>{e(snapshot.get('article_title'))}</td></tr>"
        f"<tr><td>リンク先</td><td>{e(snapshot.get('target_article_title'))}</td></tr>"
        f"<tr><td>変更種別</td><td>{e(snapshot.get('change_type'))}</td></tr>"
        f"<tr><td>候補</td><td>{e(snapshot.get('candidate_type'))} "
        f"({e(snapshot.get('priority'))})</td></tr>"
        f"<tr><td>有効期限</td><td>{e(expires_at_local)}</td></tr>"
        "</table>"
        f'<p style="font-size:14px">{e(snapshot.get("rationale"))}</p>'
        f'<p><a href="{e(review_url)}" '
        'style="display:inline-block;padding:12px 20px;background:#1a4d8f;color:#fff;'
        'text-decoration:none;border-radius:6px;font-size:16px">内容を確認</a></p>'
        f'<p style="font-size:12px;color:#555">開くだけでは承認になりません。'
        "承認・却下はページ上で明示的に操作したときだけ記録されます。"
        "リンクは 1 回限りで、期限を過ぎると使えません。</p>"
        f'<p style="font-size:12px;color:#555">リンクが開けない場合: {e(review_url)}</p>'
        "</div>"
    )


# == T4.2: approval digest =====================================================
#: T6.4: まとめ送りの 1 件に出す欄 (表示名, item の鍵)。値の無い欄は出さない。
_DIGEST_FIELDS = (
    ("投稿種別", "kind"),
    ("状態", "status"),
    ("元記事", "article_title"),
    ("切り口", "angle_label"),
    ("会話フック", "hook"),
    ("目標", "goal"),
    ("リンク", "link"),
    ("トピック", "topic"),
    ("文字数", "characters"),
    ("時期", "timing"),
)


def _digest_rows(item: dict) -> list[tuple[str, str]]:
    rows = []
    for label, key in _DIGEST_FIELDS:
        value = item.get(key)
        if key == "angle_label" and value is None:
            value = item.get("angle")
        if value is None or value == "":
            continue
        rows.append((label, f"{value}文字" if key == "characters" else str(value)))
    return rows


def approval_digest_subject(count: int) -> str:
    """T6.4: 承認のまとめ送りの件名 (日本語)。"""

    return f"【Threads承認】投稿案{count}件の確認をお願いします"
def render_approval_digest_text(*, items: list[dict], expires_at_local: str) -> str:
    """承認依頼のまとめ送り (text/plain)。

    ``items`` は提案ごとに ``proposal_id`` / ``article_title`` / ``angle`` /
    ``preview`` / ``timing`` / ``review_url`` を持つ。

    **一括承認のリンクは載せない。** 提案ごとに自分のレビューページがあり、
    承認・却下はそのページで 1 件ずつ明示的に操作したときだけ記録される。
    """

    out = [
        f"BizFluxLab の Threads 投稿案 {len(items)} 件について、確認をお願いします。",
        "",
        "1 件ずつ別々に判断できます (一括承認はありません)。",
        "承認しても、すぐには投稿されません。承認済みの queue に入るだけです。",
        "",
    ]
    for index, item in enumerate(items, start=1):
        out.append(f"[{index}] 投稿案 #{item['proposal_id']}")
        for label, value in _digest_rows(item):
            out.append(f"    {label}: {value}")
        body = item.get("body")
        if body:
            out.append("    本文:")
            out.extend(f"      {line}" for line in str(body).splitlines())
        else:
            out.append(f"    内容: {item.get('preview')}")
        notes = item.get("warnings_ja") or []
        if notes:
            out.append("    注意:")
            for note in notes:
                out.append(f"      ⚠ {note['label']}")
                if note.get("detail"):
                    out.append(f"        {note['detail']}")
        out += [f"    確認: {item['review_url']}", ""]
    out += [
        f"有効期限      : {expires_at_local}",
        "",
        "リンクは内容を表示するだけで、開いても承認にはならない。",
        "承認・却下はページ上で明示的に操作したときだけ記録される。",
        "リンクは 1 件につき 1 回限りで、期限を過ぎると使えなくなる。他人に転送しないこと。",
        "1 件を却下しても、他の提案には影響しない。",
    ]
    return "\n".join(out)


def render_approval_digest_html(*, items: list[dict], expires_at_local: str) -> str:
    """承認依頼のまとめ送り (text/html)。動的な値はすべてエスケープする。"""

    from html import escape

    def e(value) -> str:
        return escape(str(value if value is not None else "-"), quote=True)

    rows = []
    for index, item in enumerate(items, start=1):
        facts = "".join(
            f'<div style="font-size:13px;color:#555">{e(label)}: {e(value)}</div>'
            for label, value in _digest_rows(item)
        )
        notes = "".join(
            '<div style="font-size:13px;background:#fff4e5;border-left:3px solid #d98324;'
            f'padding:4px 8px;margin:4px 0">⚠ {e(note["label"])}'
            + (f'<br><span style="color:#555">{e(note["detail"])}</span>' if note.get("detail")
               else "")  # fmt: skip
            + "</div>"
            for note in (item.get("warnings_ja") or [])
        )
        rows.append(
            '<div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0">'
            f'<div style="font-size:13px;color:#555">[{index}] 投稿案 '
            f"<strong>#{e(item['proposal_id'])}</strong></div>"
            f"{facts}"
            '<div style="font-size:14px;color:#222;white-space:pre-wrap;margin:6px 0">'
            f"{e(item.get('body') or item.get('preview'))}</div>"
            f"{notes}"
            f'<p style="margin:10px 0 0"><a href="{e(item["review_url"])}" '
            'style="display:inline-block;padding:10px 16px;background:#1a4d8f;color:#fff;'
            'text-decoration:none;border-radius:6px;font-size:15px">この提案を確認</a></p>'
            "</div>"
        )
    return (
        '<div style="font-family:sans-serif;max-width:560px;line-height:1.6">'
        f'<h2 style="font-size:18px">BizFluxLab: Threads 投稿案 {len(items)} 件の確認</h2>'
        '<p style="font-size:14px">1 件ずつ別々に判断できます (一括承認はありません)。'
        "承認しても、すぐには投稿されません。</p>"
        + "".join(rows)
        + f'<p style="font-size:13px">有効期限: {e(expires_at_local)}</p>'
        '<p style="font-size:12px;color:#555">開くだけでは承認になりません。'
        "承認・却下はページ上で明示的に操作したときだけ記録されます。"
        "リンクは 1 件につき 1 回限りで、期限を過ぎると使えません。"
        "1 件を却下しても、他の提案には影響しません。</p>"
        "</div>"
    )
