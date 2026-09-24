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
    """件名 (接頭辞は :class:`EmailConfig` が付ける)。"""

    base = f"Weekly Operations Report - {report.period_end}"
    return base if report.complete else f"{base} (incomplete)"


def weekly_subject_severity(report: WeeklyReport) -> str:
    """不完全なときだけ WARNING を立てる。健全なら無印で送る。"""

    return "info" if report.complete else "warning"


def render_weekly_report(report: WeeklyReport) -> str:
    data = report.as_dict()
    out: list[str] = []

    def section(title: str) -> None:
        out.extend(["", _SEP, title, _SEP])

    def kv(label: str, value, width: int = 22) -> None:
        out.append(f"{label:<{width}}: {_fmt(value)}")

    out.append(
        f"BizFluxLab weekly operations report ({data['period_start']} .. "
        f"{data['period_end']}, {data['reporting_timezone']})"
    )
    out.append(f"generated at          : {data['generated_at']}")
    if not data["complete"]:
        out.append("")
        out.append("*** このレポートは不完全である ***")
        for reason in data["incomplete_reasons"]:
            out.append(f"  - {reason}")

    section("SYSTEM")
    system = data["system"]
    for label, key in (
        ("operations run", "operations_run_id"),
        ("profile", "profile"),
        ("status", "status"),
        ("effective date", "effective_date"),
        ("policy version", "policy_version"),
        ("steps succeeded", "step_succeeded"),
        ("steps failed", "step_failed"),
        ("steps skipped", "step_skipped"),
        ("finished at", "finished_at"),
    ):
        kv(label, system.get(key))
    for key, label in (
        ("failing_steps", "failing steps"),
        ("skipped_steps", "skipped steps"),
        ("partial_steps", "partial steps"),
    ):
        if system.get(key):
            kv(label, ", ".join(system[key]))

    section("SEARCH CONSOLE")
    gsc = data["search_console"]
    kv("coverage through", gsc.get("coverage_through"))
    kv("latest observed data", gsc.get("latest_observed_data_date"))
    kv("last import at", gsc.get("last_successful_import_at"))
    kv("rows in period", gsc.get("rows_in_period"))
    kv("impressions", gsc.get("impressions"))
    kv("clicks", gsc.get("clicks"))
    out.append(f"note: {gsc.get('note')}")

    section("GA4")
    ga4 = data["ga4"]
    kv("configured", ga4.get("configured"))
    kv("coverage through", ga4.get("coverage_through"))
    kv("latest observed data", ga4.get("latest_observed_data_date"))
    kv("rows in period", ga4.get("rows_in_period"))
    kv("sessions", ga4.get("sessions"))
    kv("organic sessions", ga4.get("organic_sessions"))
    kv("maturity", ga4.get("maturity"))

    section("SEO / C6")
    seo = data["seo"]
    kv("latest run", seo.get("latest_run_id"))
    kv("window", seo.get("window"))
    kv("candidates", seo.get("candidate_count"))
    kv("by type", seo.get("by_type"))
    kv("by priority", seo.get("by_priority"))
    kv("compared with run", seo.get("compared_with_run_id"))
    kv("new", seo.get("new"))
    kv("persisted", seo.get("persisted"))
    kv("no longer present", seo.get("no_longer_present"))
    kv("priority increased", seo.get("priority_increased"))
    kv("priority decreased", seo.get("priority_decreased"))
    if seo.get("no_longer_present_note"):
        out.append(f"note: {seo['no_longer_present_note']}")

    section("REVENUE / C7")
    rev = data["revenue"]
    kv("latest run", rev.get("latest_run_id"))
    kv("window", rev.get("window"))
    kv("monetized articles", rev.get("monetized_article_count"))
    kv("raw clicks", rev.get("raw_clicks"))
    kv("excluded clicks", rev.get("excluded_clicks"))
    kv("clean clicks", rev.get("clean_clicks"))
    kv("trusted clicks from", rev.get("trusted_measurement_start_at"))
    kv("commission rows", rev.get("commission_rows"))
    kv("candidates", rev.get("candidate_count"))
    kv("by type", rev.get("by_type"))
    kv("article-level revenue", rev.get("article_level_revenue"))
    if rev.get("attribution_note"):
        out.append(f"note: {rev['attribution_note']}")

    section("CONTENT CHANGES / C9")
    changes = data["content_changes"]
    kv("requests created", changes.get("requests_created"))
    kv("approved", changes.get("approved"))
    kv("rejected", changes.get("rejected"))
    kv("applications", changes.get("applications_attempted"))
    kv("  succeeded", changes.get("applications_succeeded"))
    kv("  failed", changes.get("applications_failed"))
    kv("awaiting approval", changes.get("awaiting_approval"))
    for applied in changes.get("latest_applied") or []:
        out.append(
            f"  applied: request {applied['change_request_id']} -> article "
            f"{applied['article_id']} ({applied['outcome']}, {applied['attempted_at']})"
        )
    for effect in changes.get("effect_tracking") or []:
        out.append(
            f"  effect: request {effect['change_request_id']} article "
            f"{effect['article_id']} -> {effect['maturity']} {effect['reasons']}"
        )
    out.append("note: 効果は因果を示さない (causal_claim=none)。改善したとは書かない。")

    section("ARTICLE HEALTH")
    health = data["article_health"]
    kv("published", health.get("published"))
    kv("drafts", health.get("drafts"))
    kv("monetized published", health.get("monetized_published"))
    kv("published without URL", health.get("published_without_url"))
    out.append(f"note: {health.get('indexability_note')}")

    section("ALERTS")
    alerts = data["alerts"]
    kv("newly opened", alerts.get("newly_opened"))
    kv("persisted", alerts.get("persisted"))
    kv("resolved in period", alerts.get("resolved_in_period"))
    kv("currently open", alerts.get("currently_open"))
    for alert in alerts.get("open_details") or []:
        out.append(
            f"  [{alert['severity']}] {alert['alert_type']}: {alert['title']} "
            f"(x{alert['occurrence_count']}, last {alert['last_seen_at']})"
        )

    if data["next_attention"]:
        section("NEXT ATTENTION")
        for item in data["next_attention"]:
            out.append(f"  - {item}")

    out += [
        "",
        _SEP,
        "このメールは運用情報のみを含む。認証情報・tracking URL・/go/ token は含まない。",
    ]
    return "\n".join(out)


def render_daily_incident(*, run_summary: dict, alerts: list[dict], notes: list[str]) -> str:
    """日次のインシデント 1 通ぶん (同じ run の問題をまとめる)。"""

    out = [
        "BizFluxLab daily operations needs attention.",
        "",
        f"operations run   : {run_summary.get('operations_run_id')}",
        f"profile          : {run_summary.get('profile')}",
        f"status           : {run_summary.get('status')}",
        f"effective date   : {run_summary.get('effective_date')}",
        f"steps            : {run_summary.get('step_succeeded')} succeeded, "
        f"{run_summary.get('step_failed')} failed, {run_summary.get('step_skipped')} skipped",
    ]
    for key, label in (
        ("failing_steps", "failing steps"),
        ("skipped_steps", "skipped steps"),
        ("partial_steps", "partial steps"),
    ):
        if run_summary.get(key):
            out.append(f"{label:<17}: {', '.join(run_summary[key])}")

    if alerts:
        out += ["", _SEP, f"alerts requiring attention ({len(alerts)})", _SEP]
        for alert in alerts:
            out.append(f"[{alert['severity']}] {alert['alert_type']}: {alert['title']}")
            if alert.get("summary"):
                out.append(f"    {alert['summary']}")
            if alert.get("article_id"):
                out.append(f"    article: {alert['article_id']}")
    if notes:
        out += ["", "notes:"]
        out.extend(f"  - {note}" for note in notes)

    out += [
        "",
        "suggested check:",
        "  uv run python scripts/run_operations.py --profile daily",
        "  uv run python scripts/report_weekly_operations.py",
        "",
        "このメールは運用情報のみを含む。認証情報・tracking URL・/go/ token は含まない。",
    ]
    return "\n".join(out)


def _fmt(value) -> str:
    if value is None:
        return UNAVAILABLE
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
        out += [
            f"[{index}] 提案 #{item['proposal_id']}  ({item['angle']})",
            f"    記事   : {item['article_title']}",
            f"    内容   : {item['preview']}",
        ]
        if item.get("timing"):
            out.append(f"    時期   : {item['timing']}")
        out += [f"    確認   : {item['review_url']}", ""]
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
        timing = (
            f'<div style="font-size:13px;color:#8a4b00">時期: {e(item["timing"])}</div>'
            if item.get("timing")
            else ""
        )
        rows.append(
            '<div style="border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0">'
            f'<div style="font-size:13px;color:#555">[{index}] 提案 '
            f"<strong>#{e(item['proposal_id'])}</strong> / {e(item['angle'])}</div>"
            f'<div style="font-size:14px;margin:4px 0">{e(item["article_title"])}</div>'
            '<div style="font-size:14px;color:#222;white-space:pre-wrap">'
            f"{e(item['preview'])}</div>"
            f"{timing}"
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
