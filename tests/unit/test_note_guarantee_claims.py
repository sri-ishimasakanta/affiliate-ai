"""保証に読める言い切りの検出 (2026-10-01。H4 の前の人の確認で見つかった形)。

pin する契約:

- 「安全に広げられます」「防げます」のような、安全・事故の防止・成果を約束する言い切りを、文ごとに
  見つける (product の check と note の検査の警告)。
- 同じ文に否定 (「とは言えません」「ものではありません」) があれば数えない。手順の「必ず確認して
  ください」のような文も数えない。
- 4 つの product は今の文面で 0 件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.products.spec import check, list_products, load_policy, load_product
from app.social.note import safety
from app.social.note.sources import load_sources

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("text", [
    "先に決めておくと、任せる範囲を安全に広げられます。",
    "これで「承認した後に誰か (または AI) が書き換えた」ことを防げます。",
    "この設定で事故をなくせます。",
    "必ず事故を防ぐ仕組みです。",
    "確実に安全です。",
])
def test_affirmative_guarantees_are_found(text) -> None:
    assert safety.guarantee_claims(text)
    warnings = safety.check_body(text, commissions_known=False)[1]
    assert any("guarantee wording" in w for w in warnings)


@pytest.mark.parametrize("text", [
    "この分け方で事故がなくなる、あるいは収益が上がる、とは言えません。",
    "防げるとは言えません。",
    "特定の成果や安全を約束するものではありません。",
    "人が確認する境界を明示したまま、任せる範囲を広げやすくなります。",
    "以前の承認のまま使うことを避けやすくなります。",
    "公開の前に必ず確認してください。",
])
def test_negated_or_neutral_sentences_are_not_flagged(text) -> None:
    assert safety.guarantee_claims(text) == []


def test_the_committed_products_have_no_guarantee_wording() -> None:
    for pid in list_products(REPO):
        product = load_product(REPO, pid)
        result = check(product, load_sources(REPO), policy=load_policy())
        assert not [w for w in result["warnings"] if "guarantee wording" in w], pid
        for text in product.assets.values():
            assert safety.guarantee_claims(text) == [], pid
