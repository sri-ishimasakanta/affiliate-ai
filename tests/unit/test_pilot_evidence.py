"""N7: SaaS の検証の証拠のまとめ (決めない・数を作らない)。

**このテストの数はすべて、このプロジェクトのデータの形を確かめるための合成の fixture。**
本物の利用者の数ではない (本番に入れない)。

pin する契約:

- 試しの利用者がいなければ・足りなければ ``insufficient_evidence`` (推定しない)。
- 条件ごとに満たす / 満たさない。全体は go を支える / 反する / 混ざる。どれも判断ではない。
- SaaS / Managed / Hybrid の兆しは書くだけ。
- 声の記録に個人の情報 (メール・電話・URL) を入れられない。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.n_track import metrics as mm
from app.n_track import pilot

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
POLICY = pilot.load_policy()
GOOD = {"activated": 1, "time_to_first_value_hours": 24, "workflows_completed": 3,
        "active_days": 12, "onboarding_difficulty": 2, "support_minutes": 30,
        "value_rating": 5, "would_continue": 1, "operating_cost_jpy": 500, "api_cost_jpy": 300,
        "willingness_to_pay_jpy": 3000}  # fmt: skip
BAD = {"activated": 0, "time_to_first_value_hours": 200, "workflows_completed": 0,
       "active_days": 1, "onboarding_difficulty": 5, "support_minutes": 400,
       "value_rating": 2, "would_continue": 0, "operating_cost_jpy": 5000, "api_cost_jpy": 3000,
       "willingness_to_pay_jpy": 500}  # fmt: skip


def _rows(pilots: dict[str, dict]) -> list[dict]:
    rows, i = [], 0
    for ref, metrics in pilots.items():
        for metric, value in metrics.items():
            i += 1
            rows.append({"id": i, "subject_kind": "pilot", "subject_ref": ref, "metric": metric,
                         "value": value, "observed_at": NOW})
    return rows


def test_no_pilots_means_insufficient_evidence() -> None:
    result = pilot.evaluate([], policy=POLICY)
    assert result["overall"] == "insufficient_evidence" and result["pilots"] == 0
    assert result["model_signals"] == {"state": "insufficient_evidence"}
    assert all(c["status"] == "insufficient" for c in result["criteria"].values())
    assert "not a decision" in result["note"]


def test_too_few_pilots_are_never_a_go() -> None:
    result = pilot.evaluate(_rows({"pilot-01": GOOD, "pilot-02": GOOD}), policy=POLICY)
    assert result["overall"] == "insufficient_evidence"


def test_good_evidence_supports_go_and_reads_as_self_service() -> None:
    result = pilot.evaluate(_rows({f"pilot-0{i}": GOOD for i in range(1, 4)}), policy=POLICY)
    assert result["overall"] == "evidence_supports_go"
    assert result["model_signals"]["reading"] == "SaaS-leaning"


def test_bad_or_mixed_evidence() -> None:
    bad = pilot.evaluate(_rows({f"pilot-0{i}": BAD for i in range(1, 4)}), policy=POLICY)
    assert bad["overall"] == "evidence_against_go"
    two_good = pilot.evaluate(_rows({"pilot-01": GOOD, "pilot-02": GOOD, "pilot-03": BAD}),
                              policy=POLICY)
    assert two_good["criteria"]["activation"] == {"status": "met", "value": 0.67, "n": 3,
                                                  "threshold": 0.67}
    hard = {**GOOD, "onboarding_difficulty": 5, "support_minutes": 400}
    mixed = pilot.evaluate(_rows({f"pilot-0{i}": hard for i in range(1, 4)}), policy=POLICY)
    assert mixed["overall"] == "mixed"  # 9 条件のうち 2 つだけ満たさない
    assert mixed["criteria"]["support_burden"]["status"] == "not_met"


def test_high_value_but_hard_onboarding_reads_as_managed() -> None:
    managed = {**GOOD, "onboarding_difficulty": 5, "support_minutes": 240}
    result = pilot.evaluate(_rows({f"pilot-0{i}": managed for i in range(1, 4)}),
                            policy=POLICY)
    assert result["model_signals"]["reading"] == "Managed-leaning"


@pytest.mark.parametrize("over, match", [
    ({"note": "連絡先 someone@example.com"}, "personal data"),
    ({"note": "電話 090-1234-5678"}, "personal data"),
    ({"note": "https://example.com/profile"}, "personal data"),
    ({"metric": "value_rating", "value": 7}, "1-5"),
    ({"metric": "would_continue", "value": 2}, "0 or 1"),
    ({"subject_ref": "tanaka"}, "reference form"),
])
def test_pilot_records_refuse_personal_data_and_bad_values(over, match) -> None:
    base = {"subject_kind": "pilot", "subject_ref": "pilot-01", "metric": "value_rating",
            "value": 4, "observed_at": NOW, "source_description": "pilot interview notes",
            "entered_by": "human"}  # fmt: skip
    with pytest.raises(mm.MetricError, match=match):
        mm.validate(mm.MetricInput(**{**base, **over}))


def test_the_cli_reports_insufficient_evidence_on_an_empty_ledger(session, capsys) -> None:
    from sqlalchemy.orm import sessionmaker

    from scripts.pilot_evidence import main

    def factory():
        return sessionmaker(bind=session.get_bind())()

    assert main(["summary"], session_factory=factory) == 0
    assert '"insufficient_evidence"' in capsys.readouterr().out
