"""アフィリエイトの成果の帰属の準備度 (C10-A、C11 のための棚卸し、pure)。

**帰属はしない。** いまのデータで、何がどこまで決定的に結べるかを分類するだけ:

- ``A_direct``: 決定的な鍵で記事まで結べる (例: ``/go/{token}`` のクリック → リンク先 → 記事)。
- ``B_provider``: 提供元 (ASP) の単位まで (例: Make の成果。取り込みはプログラムで絞らず、
  プログラムの ID は取り込みの繰り返しが付ける)。
- ``C_program``: プログラムの単位まで。
- ``D_unattributed``: 結べない (鍵が無い)。

鍵が無いものは記事に配らない。比例配分・最後のクリックの仮定・収益の分割はしない。
提供元の機能 (SubID 等) は、このリポジトリの取り込みとクライアントで確かめられることだけを
言う (確かめられないものは ``unknown``)。追跡の URL は変えない (変更は人の判断)。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

LEVEL_DIRECT = "A_direct"
LEVEL_PROVIDER = "B_provider"
LEVEL_PROGRAM = "C_program"
LEVEL_UNATTRIBUTED = "D_unattributed"
LEVELS = (LEVEL_DIRECT, LEVEL_PROVIDER, LEVEL_PROGRAM, LEVEL_UNATTRIBUTED)

UNKNOWN = "unknown"

#: 取り込みを実装している提供元の、コードから確かめられる性質 (外の資料で確かめたものではない)。
#: ``subid_supported`` は、取り込みのクライアントが成果のクリックの参照を読んでいるかだけで決める。
KNOWN_IMPORTERS = {
    "make": {
        "commission_import": True,
        "provider_transaction_id": True,  # source_commission_id (Make の commission id)
        "click_reference_in_import": False,  # validate_make_commission_row は読まない
        "program_filter_in_import": False,  # 取り込みは全部の成果を読む (プログラムで絞らない)
    },
}


@dataclass(frozen=True)
class ProgramReadiness:
    program_id: int
    program_name: str
    provider: str | None
    has_tracking_url: bool
    click_tracking_available: bool
    article_link_available: bool
    provider_transaction_id_available: bool | str
    subid_supported: bool | str
    subid_currently_used: bool
    deterministic_join_possible: bool
    configuration_change_required: bool
    historical_backfill_possible: bool | str
    click_attribution_level: str
    commission_attribution_level: str
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {**asdict(self), "reasons": list(self.reasons)}


def program_readiness(*, program_id: int, name: str, provider: str | None,
                      has_tracking_url: bool, active_targets: int, targets_with_article: int,
                      commission_rows: int, commissions_with_click_reference: int = 0,
                      subid_in_tracking_url: bool = False) -> ProgramReadiness:
    """1 つのプログラムの準備度 (決定論的)。"""

    importer = KNOWN_IMPORTERS.get((provider or "").lower())
    reasons: list[str] = []
    click_tracking = has_tracking_url and active_targets > 0
    article_link = targets_with_article > 0
    if not has_tracking_url:
        reasons.append("no tracking URL: no /go/ link target can be created")
    elif not click_tracking:
        reasons.append("tracking URL present but no active /go/ link target")
    if importer is None:
        transaction = UNKNOWN
        subid = UNKNOWN
        commission_level = LEVEL_UNATTRIBUTED
        reasons.append("no commission importer for this provider")
    else:
        transaction = importer["provider_transaction_id"]
        subid = UNKNOWN if not importer["click_reference_in_import"] else True
        commission_level = LEVEL_PROVIDER if not importer["program_filter_in_import"] else (
            LEVEL_PROGRAM)
        if not importer["program_filter_in_import"]:
            reasons.append("commission import is not filtered by program (provider level)")
        if not importer["click_reference_in_import"]:
            reasons.append("the commission import stores no click reference / SubID")
    join = commissions_with_click_reference > 0 and article_link
    if join:
        commission_level = LEVEL_DIRECT
    if commission_rows == 0:
        reasons.append("no commission rows imported yet")
    return ProgramReadiness(
        program_id=program_id, program_name=name, provider=provider,
        has_tracking_url=has_tracking_url, click_tracking_available=click_tracking,
        article_link_available=article_link, provider_transaction_id_available=transaction,
        subid_supported=subid, subid_currently_used=subid_in_tracking_url,
        deterministic_join_possible=join, configuration_change_required=not join,
        historical_backfill_possible=False if not join else UNKNOWN,
        click_attribution_level=LEVEL_DIRECT if (click_tracking and article_link)
        else LEVEL_UNATTRIBUTED,
        commission_attribution_level=commission_level, reasons=tuple(reasons))


#: 帰属の真理値表 (データの種類 → 段階・結ぶ鍵)。
TRUTH_TABLE = (
    {"data": "outbound click (after trusted_measurement_start_at)", "level": LEVEL_DIRECT,
     "join": "click.token → affiliate_link_targets.token → article_id, affiliate_program_id"},
    {"data": "outbound click without a local link target", "level": LEVEL_UNATTRIBUTED,
     "join": "none (e.g. the synthetic probe)"},
    {"data": "outbound click before trusted_measurement_start_at", "level": "excluded",
     "join": "instrumentation period: not reader behaviour"},
    {"data": "Make commission", "level": LEVEL_PROVIDER,
     "join": "provider only (import is not filtered by program; no click reference stored)"},
    {"data": "commission of a provider without an importer", "level": LEVEL_UNATTRIBUTED,
     "join": "no data"},
    {"data": "commission → article", "level": LEVEL_UNATTRIBUTED,
     "join": "none: no click reference / SubID / order key links a commission to a click"},
)

#: C11 に要る変更 (いずれも人の判断。ここでは変えない)。
C11_REQUIREMENTS = (
    "choose a per-click reference that the ASP passes back on commissions (SubID / clickref); "
    "confirm the ASP's rules first (docs/operations/measurement-layer.md)",
    "append the reference in the /go/ redirect or the tracking URL (changes the live "
    "redirect and the publication href contract; human decision)",
    "store the reference on each outbound click and read it in the commission import",
    "filter or tag commission imports by program (today every Make commission is imported "
    "under the program whose run happens first)",
    "historical commissions cannot be backfilled to articles (no reference was ever sent)",
)


__all__ = ["C11_REQUIREMENTS", "KNOWN_IMPORTERS", "LEVELS", "LEVEL_DIRECT", "LEVEL_PROGRAM",
           "LEVEL_PROVIDER", "LEVEL_UNATTRIBUTED", "ProgramReadiness", "TRUTH_TABLE",
           "UNKNOWN", "program_readiness"]
