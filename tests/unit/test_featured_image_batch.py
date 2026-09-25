"""W1.5B: 1 バッチ分の featured image 制作パッケージ (pure。WordPress にも DB にも触らない)。

pin する契約:

- パッケージは W1.5 manifest の写し。設計の値 (見出し・補助語・色・モチーフ・構図・alt・
  media の title・ファイル名・見分け方) は 1 文字も変えない。
- バッチ 1 は 7 → 2 → 6 → 8 → 9 の順 (一覧の見本も同じ順)。
- 画像生成の文は manifest の文 + 決まった「文字なし」の文。日本語を含まない。
- 組版の座標は manifest の ``typesetting`` だけから決まり、それは試作 4 枚から較正した本番の
  値 (印 128×18 at (72, 322)・92px・上端 367・行間 1.0・補助語 Bold 44px 紺・帯 (0, 657, 18))
  と同じ。下の余白に入らない。古い仕様のままの manifest は止まる。
- manifest と違うパッケージ (古い版・並べ替え・書き換え) は検査で止まる。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from app.wordpress.featured_image_batch import (
    NEGATIVE_GUARD,
    TEXTLESS_SUFFIX,
    build_batch_package,
    layout_positions,
    manifest_sha256,
    render_checklist_markdown,
    render_handoff_markdown,
    sha256_of,
    shape_summary,
    typesetting_plan,
    validate_batch_package,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "docs" / "operations" / "featured-image-w1.5-manifest.json"
JAPANESE = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def digest() -> str:
    return manifest_sha256(MANIFEST_PATH)


@pytest.fixture
def package(manifest, digest) -> dict:
    return build_batch_package(
        manifest, 1, manifest_path="docs/operations/x.json", manifest_sha256=digest
    )


def test_batch_1_is_the_five_meeting_notes_articles_in_order(package, manifest, digest) -> None:
    assert [i["article_id"] for i in package["items"]] == [7, 2, 6, 8, 9]
    assert package["contact_sheet_order"] == [7, 2, 6, 8, 9]
    assert [i["order"] for i in package["items"]] == [1, 2, 3, 4, 5]
    assert validate_batch_package(package, manifest, manifest_sha256=digest) == []


def test_design_values_are_copied_exactly(package, manifest) -> None:
    by_id = {a["article_id"]: a for a in manifest["articles"]}
    for item in package["items"]:
        article = by_id[item["article_id"]]
        for key in (
            "slug",
            "title",
            "headline",
            "sublabel",
            "accent",
            "motif",
            "composition",
            "alt_text",
            "media_title",
            "planned_file",
            "distinguish_from",
            "generation_prompt",
            "typesetting",
        ):
            assert item[key] == article[key], (item["article_id"], key)
        stem = article["planned_file"].removesuffix(".webp")
        assert item["files"] == {
            "background": f"backgrounds/{stem}-bg.png",
            "proof": f"proofs/{stem}-typeset-proof.png",
            "master_png": f"{stem}.png",
            "final_webp": article["planned_file"],
        }


def test_the_five_images_keep_their_distinct_shapes(package) -> None:
    shapes = {i["article_id"]: shape_summary(i) for i in package["items"]}
    assert "概念図" in shapes[7]
    assert "2 枚のカード" in shapes[2]
    assert "比較表" in shapes[6]
    assert "∞" in shapes[8] and "砂時計" in shapes[8]
    assert "見積もりシート" in shapes[9] and "切り替え" in shapes[9]
    motifs = {i["article_id"]: i["motif"] for i in package["items"]}
    assert "concept" in motifs[7].lower() or "satellite" in motifs[7]
    assert "infinity" in motifs[8] and "hourglass" in motifs[8]
    assert "estimate sheet" in motifs[9] and "toggle" in motifs[9]


def test_generation_prompts_are_textless_and_keep_the_committed_text(package) -> None:
    for item in package["items"]:
        gen = item["generation"]
        assert gen["textless"] is True
        assert gen["prompt"] == f"{item['generation_prompt']}; {TEXTLESS_SUFFIX}"
        assert not JAPANESE.search(gen["prompt"] + gen["negative_prompt"])
        # 個々の禁止事項に「, 」を含むもの ("$, ¥, amounts") があるので、文字列として確かめる。
        assert gen["negative_prompt"].startswith(", ".join(item["negative_constraints"]))
        negatives = gen["negative_prompt"].split(", ")
        for term in (*NEGATIVE_GUARD, "text", "logo", "product screenshot", "people", "robot"):
            assert term in negatives
        assert gen["single_prompt"].startswith(gen["prompt"])
        # 見出し・補助語を画像モデルに描かせない。
        for line in item["headline"]["lines"]:
            assert line not in gen["single_prompt"]
        assert item["sublabel"] not in gen["single_prompt"]


def test_the_typesetting_plan_follows_the_calibrated_production_layout(package) -> None:
    plan = package["items"][0]["typesetting_plan"]
    assert (plan["mark"]["x"], plan["mark"]["y"]) == (72, 322)
    assert (plan["mark"]["width"], plan["mark"]["height"]) == (128, 18)
    head = plan["headline"]
    assert (head["size_px"], head["line_pitch_px"], head["weight"]) == (92, 92.0, 700)
    assert head["palt"] is True
    assert [line["em_top"] for line in head["lines"]] == [367, 459]
    assert [line["baseline_y"] for line in head["lines"]] == [448, 540]
    assert [line["x"] for line in head["lines"]] == [72, 72]
    # 印の下端から 1 行目の em box の上端まで 27px (試作 4 枚の中央値)。
    assert head["lines"][0]["em_top"] - (plan["mark"]["y"] + plan["mark"]["height"]) == 27
    sub = plan["sublabel"]
    assert (sub["size_px"], sub["weight"], sub["color"]) == (44, 700, "#12263F")
    assert (sub["em_top"], sub["baseline_y"], sub["em_bottom"]) == (565, 604, 609)
    assert sub["em_bottom"] <= plan["safe_bottom_y"] == 615
    assert plan["accent_bar"] == {"x": 0, "y": 657, "width": 1200, "height": 18, "color": "#0284C7"}
    for item in package["items"]:
        assert item["typesetting_plan"]["mark"]["color"] == item["accent"]["color"]
        assert item["typesetting_plan"]["headline"] | {"lines": None} == head | {"lines": None}


def _typesetting(**overrides) -> dict:
    from app.wordpress.featured_image_batch import PRODUCTION_TYPESETTING

    value = copy.deepcopy(PRODUCTION_TYPESETTING)
    value["headline_mark"]["color"] = value["accent_bar"]["color"] = "#000000"
    value.update(overrides)
    return value


def test_a_one_line_headline_moves_the_sublabel_up() -> None:
    plan = typesetting_plan(_typesetting(), ["AI議事録"], "補助語")
    assert plan["sublabel"]["em_top"] == 367 + 92 + 14


def test_the_plan_reads_every_coordinate_from_the_manifest_typesetting() -> None:
    plan = typesetting_plan(_typesetting(headline_top_y=300, line_height=1.1), ["一", "二"], "補")
    assert [line["em_top"] for line in plan["headline"]["lines"]] == [300, 401.2]


def test_a_manifest_still_on_the_old_spec_is_refused(manifest, digest) -> None:
    from app.wordpress.featured_image_batch import (
        PREVIOUS_TYPESETTING,
        production_typesetting_problems,
    )

    old = copy.deepcopy(manifest)
    for article in old["articles"]:
        for key, value in PREVIOUS_TYPESETTING.items():
            if isinstance(value, dict):
                value = {**value, "color": article["accent"]["color"]}
            article["typesetting"][key] = value
    assert production_typesetting_problems(old["articles"][0]["typesetting"])
    package = build_batch_package(old, 1, manifest_path="x", manifest_sha256=digest)
    problems = validate_batch_package(package, old, manifest_sha256=digest)
    assert any("manifest typesetting headline_size_px" in p for p in problems)
    assert any("manifest typesetting headline_mark" in p for p in problems)


def test_layout_positions_apply_palt_adjustments() -> None:
    metrics = {"の": (1000, -1, -14), "A": (600, 0, 0)}
    positions, width = layout_positions("Aの", metrics, size=100, units_per_em=1000)
    assert positions == [("A", 0.0), ("の", 59.9)]
    assert width == 158.6


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p["items"][0]["headline"]["lines"].__setitem__(0, "別の見出し"), "headline"),
        (lambda p: p["items"].reverse(), "do not match batch order"),
        (lambda p: p["source"].__setitem__("sha256", "0" * 64), "different manifest version"),
        (lambda p: p["items"][1]["generation"].__setitem__("prompt", "a robot"), "generation"),
        (
            lambda p: p["items"][2]["typesetting_plan"]["mark"].__setitem__("y", 300),
            "typesetting_plan",
        ),
        (lambda p: p["items"][3]["files"].__setitem__("final_webp", "x.webp"), "final file"),
        (lambda p: p.__setitem__("contact_sheet_order", [2, 7, 6, 8, 9]), "contact_sheet"),
    ],
)
def test_validation_stops_a_package_that_drifted(package, manifest, digest, change, message):
    drifted = copy.deepcopy(package)
    change(drifted)
    problems = validate_batch_package(drifted, manifest, manifest_sha256=digest)
    assert any(message in p for p in problems), problems


def test_the_handoff_and_checklist_carry_every_prompt(package) -> None:
    handoff = render_handoff_markdown(package)
    checklist = render_checklist_markdown(package)
    assert "7 → 2 → 6 → 8 → 9" in handoff and "7 → 2 → 6 → 8 → 9" in checklist
    for item in package["items"]:
        assert item["generation"]["prompt"] in handoff
        assert item["generation"]["negative_prompt"] in handoff
        assert item["files"]["final_webp"] in checklist


def test_the_cli_writes_the_package_and_validates_it(tmp_path, capsys) -> None:
    from scripts.prepare_featured_image_batch import main

    assert main(["package", "--batch", "1", "--out-root", str(tmp_path)]) == 0
    out = tmp_path / "batch-1"
    assert (out / "batch-manifest.json").exists()
    assert (out / "README.md").exists() and (out / "validation-checklist.md").exists()
    assert len(list((out / "prompts").glob("*.prompt.txt"))) == 5
    assert (out / "backgrounds").is_dir() and (out / "proofs").is_dir()
    assert "validation: ok" in capsys.readouterr().out

    package = json.loads((out / "batch-manifest.json").read_text(encoding="utf-8"))
    package["items"][0]["sublabel"] = "書き換え"
    (out / "batch-manifest.json").write_text(json.dumps(package), encoding="utf-8")
    assert main(["validate", "--batch", "1", "--out-root", str(tmp_path)]) == 1
    assert "sublabel differs" in capsys.readouterr().out


# == batch 2 ===================================================================
@pytest.fixture
def package_2(manifest, digest) -> dict:
    return build_batch_package(
        manifest, 2, manifest_path="docs/operations/x.json", manifest_sha256=digest
    )


def test_batch_2_is_transcription_and_project_articles_in_order(package_2, manifest, digest):
    ids = [i["article_id"] for i in package_2["items"]]
    assert ids == [3, 12, 13, 15]
    assert package_2["contact_sheet_order"] == [3, 12, 13, 15]
    assert not set(ids) & ({7, 2, 6, 8, 9} | {20, 23, 24, 25})
    assert package_2["source"]["sha256"] == digest
    assert validate_batch_package(package_2, manifest, manifest_sha256=digest) == []
    colors = {i["article_id"]: i["accent"]["color"] for i in package_2["items"]}
    assert colors == {3: "#0369A1", 12: "#0369A1", 13: "#DB2777", 15: "#DB2777"}
    for item in package_2["items"]:
        plan = item["typesetting_plan"]
        assert [line["baseline_y"] for line in plan["headline"]["lines"]] == [448, 540]
        assert plan["sublabel"]["baseline_y"] == 604
        assert (plan["mark"]["y"], plan["accent_bar"]["y"]) == (322, 657)
        assert plan["mark"]["color"] == plan["accent_bar"]["color"] == item["accent"]["color"]


def test_batch_2_pairs_are_told_apart_by_shape(package_2) -> None:
    shapes = {i["article_id"]: shape_summary(i) for i in package_2["items"]}
    assert "波形" in shapes[3] and "クリップボード" in shapes[12]
    assert "ボード" in shapes[13] and "データベースの表" in shapes[15]
    motifs = {i["article_id"]: i["motif"] for i in package_2["items"]}
    assert "waveform" in motifs[3] and "clipboard" in motifs[12]
    assert "kanban" in motifs[13] and "database table" in motifs[15]
    checklist = render_checklist_markdown(package_2)
    assert "3 と 12 が 126×71 でも形で" in checklist
    assert "13 と 15 が 126×71 でも形で" in checklist
    assert "新しいアクセント `#DB2777` (13, 15)" in checklist


def test_batch_1_checklist_has_no_new_accent_line(package) -> None:
    assert "新しいアクセント" not in render_checklist_markdown(package)


def test_the_manifest_hash_ignores_checkout_line_endings(tmp_path) -> None:
    data = MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n")
    lf, crlf = tmp_path / "lf.json", tmp_path / "crlf.json"
    lf.write_bytes(data)
    crlf.write_bytes(data.replace(b"\n", b"\r\n"))
    assert manifest_sha256(lf) == manifest_sha256(crlf) == manifest_sha256(MANIFEST_PATH)
    assert manifest_sha256(lf) != sha256_of(crlf)


# == batch 3 ===================================================================
def test_batch_3_is_general_and_crm_in_order_and_checks_every_pair(manifest, digest) -> None:
    package = build_batch_package(manifest, 3, manifest_path="x", manifest_sha256=digest)
    ids = [i["article_id"] for i in package["items"]]
    assert ids == package["contact_sheet_order"] == [1, 4, 5, 14]
    assert len({i["slug"] for i in package["items"]}) == 4
    assert validate_batch_package(package, manifest, manifest_sha256=digest) == []
    colors = {i["article_id"]: i["accent"]["color"] for i in package["items"]}
    assert colors == {1: "#0D9488", 4: "#EA580C", 5: "#EA580C", 14: "#EA580C"}
    # article 1 の日本語の slug は manifest の値のまま (WordPress の形には解決しない)。
    assert package["items"][0]["slug"] == "業務効率化-ツール-おすすめ-roundup"
    for item in package["items"]:
        assert not JAPANESE.search(item["generation"]["single_prompt"]), item["article_id"]
    checklist = render_checklist_markdown(package)
    for first, second in ((4, 14), (4, 5), (5, 14), (1, 4), (1, 5), (1, 14)):
        assert f"{first} と {second} が 126×71 でも形で" in checklist
    assert "(見出しの語や色だけに頼らない): (manifest に規則なし) 5 = " in checklist
    assert "1 を 25 (試作または別のバッチ) と並べても形で" in checklist


# == batch 4 ===================================================================
def test_batch_4_is_the_green_automation_family_and_checks_every_pair(manifest, digest) -> None:
    package = build_batch_package(manifest, 4, manifest_path="x", manifest_sha256=digest)
    ids = [i["article_id"] for i in package["items"]]
    assert ids == package["contact_sheet_order"] == [10, 11, 16, 17, 18]
    assert len({i["slug"] for i in package["items"]}) == 5
    assert not set(ids) & {1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 15, 20, 23, 24, 25}
    assert validate_batch_package(package, manifest, manifest_sha256=digest) == []
    assert {i["accent"]["color"] for i in package["items"]} == {"#16A34A"}
    for item in package["items"]:
        gen = item["generation"]
        assert not JAPANESE.search(gen["single_prompt"]), item["article_id"]
        assert "robot" in gen["negative_prompt"].split(", ")
        assert "Make logo" in gen["negative_prompt"] or item["article_id"] not in (10, 11)
    shapes = {i["article_id"]: shape_summary(i) for i in package["items"]}
    assert "モジュール" in shapes[10] and "階段" in shapes[18]
    assert "漏斗" in shapes[16] and "格子" in shapes[17] and "プランカード" in shapes[11]
    checklist = render_checklist_markdown(package)
    # 同じ緑の 5 枚は形で見分ける: 指定の 5 組を含む、バッチの中の 10 組すべて。
    for first, second in ((10, 18), (10, 16), (16, 17), (11, 17), (11, 18)):
        assert f"{first} と {second} が 126×71 でも形で" in checklist
    assert checklist.count("126×71 でも形で見分けられる (見出しの語") == 10


# == batch 5 ===================================================================
def test_batch_5_is_genai_and_governance_and_keeps_the_shield_out_of_22(manifest, digest):
    package = build_batch_package(manifest, 5, manifest_path="x", manifest_sha256=digest)
    ids = [i["article_id"] for i in package["items"]]
    assert ids == package["contact_sheet_order"] == [19, 21, 22]
    assert len({i["slug"] for i in package["items"]}) == 3
    assert validate_batch_package(package, manifest, manifest_sha256=digest) == []
    colors = {i["article_id"]: i["accent"]["color"] for i in package["items"]}
    assert colors == {19: "#4F46E5", 21: "#475569", 22: "#475569"}
    by_id = {i["article_id"]: i for i in package["items"]}
    for item in package["items"]:
        assert not JAPANESE.search(item["generation"]["single_prompt"]), item["article_id"]
    # 22 は盾を使わない (盾は試作 20 と 21 のもの)。
    assert "shield" not in by_id[22]["generation"]["prompt"]
    assert "shield or lock" in by_id[22]["generation"]["negative_prompt"]
    assert "shield" in by_id[21]["motif"]  # manifest の設計どおり (21 は隅に小さな盾)
    checklist = render_checklist_markdown(package)
    assert "21 と 22 が 126×71 でも形で" in checklist
    for article_id, other in ((19, 24), (21, 20), (22, 20)):
        assert f"{article_id} を {other} (試作または別のバッチ) と並べても形で" in checklist
