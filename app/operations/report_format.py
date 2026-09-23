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
