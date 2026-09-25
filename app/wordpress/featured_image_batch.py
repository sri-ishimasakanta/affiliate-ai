"""W1.5B: featured image の 1 バッチ分の制作パッケージ (設計の写しと、組版の座標)。

入力は W1.5 の manifest (``docs/operations/featured-image-w1.5-manifest.json``) だけ。
設計は変えずに写し、次を足す:

- 画像生成の文 (文字なしの背景とモチーフだけ)。manifest の ``generation_prompt`` に、
  文字を描かせないための決まった文を足しただけ。
- 組版の座標 (見出しの印・見出しの各行・補助語・下端のアクセント帯)。W1 の規則から計算する。

WordPress にも DB にも触らない。標準ライブラリだけを使う (Pillow の無い環境でも import できる。
画像を実際に組むのは ``scripts/compose_featured_image.py``)。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "featured-image-w1.5-batch/1"
W15_SCHEMA = "featured-image-w1.5/1"

CANVAS = (1200, 675)
MARGIN = {"top": 60, "bottom": 60, "left": 72, "right": 72}
LABEL_ZONE = (0, 0, 560, 170)
MARK = (72, 250, 64, 8)
HEADLINE_TOP = 282
LINE_HEIGHT = 1.2
SUBLABEL_GAP = 28
ACCENT_BAR = (0, 663, 1200, 12)
BACKGROUND = "#F5F7FA"
# Noto Sans CJK / JP の em box: 上端から 0.88em 下が baseline (OS/2 sTypoAscender 880 /
# sTypoDescender -120, unitsPerEm 1000)。「上端 y」は em box の上端として扱う。
EM_ASCENT = 0.88
EM_DESCENT = 0.12

# 画像生成の文に足す決まった文 (設計ではなく、文字を描かせないための共通の指示)。
TEXTLESS_SUFFIX = (
    "textless image: every card, sheet, panel, table cell and line is a blank shape; "
    "the left half and the top-left corner stay plain soft light grey background; "
    "main motif only on the right half; 16:9, 1200x675"
)
NEGATIVE_GUARD = (
    "pseudo-text",
    "scribbles resembling writing",
    "labels",
    "signage",
    "digits",
    "prices",
    "currency symbols",
    "dates",
    "calendar numbers",
    "mascot",
)
# 製品の設計から写す項目 (値は manifest と完全に同じでなければならない)。
COPIED_FIELDS = (
    "article_id",
    "slug",
    "title",
    "url",
    "article_type",
    "family",
    "headline",
    "sublabel",
    "accent",
    "motif",
    "composition",
    "list_intent",
    "distinguish_from",
    "notes",
    "alt_text",
    "media_title",
    "planned_file",
    "generation_prompt",
    "negative_constraints",
)
_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")


# 一覧で見分ける形 (W1.5A の設計の要約。これを別の構図に置き換えない)。
SHAPE_SUMMARY = {
    7: "概念図 (中心の文書から 3 つの節点)",
    2: "2 枚のカード",
    6: "比較表",
    8: "∞ のカードと砂時計のカード",
    9: "見積もりシートと月払い/年払いの切り替え",
}


def shape_summary(item: Mapping) -> str:
    return SHAPE_SUMMARY.get(item["article_id"]) or item["motif"].split(". ")[0]


def layout_positions(
    text: str, metrics: Mapping[str, tuple[int, int, int]], *, size: int, units_per_em: int
) -> tuple[list[tuple[str, float]], float]:
    """1 文字ずつの x 位置と全体の幅 (px)。

    ``metrics`` は文字 → (advance, palt の XPlacement, palt の XAdvance) (font units)。
    palt を使わない行は XPlacement / XAdvance を 0 にして渡す。
    """

    scale = size / units_per_em
    pen = 0.0
    positions = []
    for char in text:
        advance, x_placement, x_advance = metrics[char]
        positions.append((char, round(pen + x_placement * scale, 3)))
        pen += (advance + x_advance) * scale
    return positions, round(pen, 3)


class BatchPackageError(ValueError):
    """manifest からパッケージを作れない (設計の側を直す)。"""


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _round(value: float) -> int:
    """四捨五入 (銀行丸めにしない)。"""

    return math.floor(value + 0.5)


def typesetting_plan(typesetting: Mapping, lines: list[str], sublabel: str) -> dict:
    """W1 の規則から、組版の座標を決める (同じ入力なら必ず同じ座標)。"""

    size = int(typesetting["headline_size_px"])
    sub_size = int(typesetting["sublabel_size_px"])
    pitch = size * LINE_HEIGHT
    x = MARK[0]
    headline = []
    for index, text in enumerate(lines):
        em_top = HEADLINE_TOP + index * pitch
        headline.append(
            {
                "text": text,
                "x": x,
                "em_top": round(em_top, 2),
                "baseline_y": _round(em_top + EM_ASCENT * size),
            }
        )
    sub_em_top = HEADLINE_TOP + len(lines) * pitch + SUBLABEL_GAP
    sub_baseline = _round(sub_em_top + EM_ASCENT * sub_size)
    return {
        "canvas": {"width": CANVAS[0], "height": CANVAS[1], "background": BACKGROUND},
        "mark": {
            "x": MARK[0],
            "y": MARK[1],
            "width": MARK[2],
            "height": MARK[3],
            "color": typesetting["headline_mark"]["color"],
        },
        "headline": {
            "font": "Noto Sans JP Bold (wght 700)",
            "size_px": size,
            "color": typesetting["headline_color"],
            "line_pitch_px": round(pitch, 2),
            "anchor": "left-baseline (em box top + 0.88em)",
            "palt": True,
            "max_width_px": int(typesetting["column_width_px"]),
            "lines": headline,
        },
        "sublabel": {
            "font": "Noto Sans JP Medium (wght 500)",
            "size_px": sub_size,
            "color": typesetting["sublabel_color"],
            "text": sublabel,
            "x": x,
            "em_top": round(sub_em_top, 2),
            "baseline_y": sub_baseline,
            "em_bottom": round(sub_em_top + sub_size, 2),
            "palt": False,
            "max_width_px": int(typesetting["column_width_px"]),
        },
        "accent_bar": {
            "x": ACCENT_BAR[0],
            "y": ACCENT_BAR[1],
            "width": ACCENT_BAR[2],
            "height": ACCENT_BAR[3],
            "color": typesetting["accent_bar"]["color"],
        },
        "safe_bottom_y": CANVAS[1] - MARGIN["bottom"],
    }


def generation_brief(article: Mapping) -> dict:
    """文字なしの背景とモチーフを作るための文 (manifest の文 + 決まった文)。"""

    committed = article["generation_prompt"]
    prompt = f"{committed}; {TEXTLESS_SUFFIX}"
    negatives = list(article["negative_constraints"])
    negatives += [term for term in NEGATIVE_GUARD if term not in negatives]
    negative = ", ".join(negatives)
    return {
        "textless": True,
        "prompt": prompt,
        "negative_prompt": negative,
        # negative prompt を受け付けない画像モデル用に、1 つにつないだもの。
        "single_prompt": f"{prompt}. Do not include: {negative}.",
        "output": (
            "16:9 PNG, 1200x675 preferred "
            "(larger 16:9 is resized; at most 3% off 16:9 is centre-cropped)"
        ),
    }


def _stem(planned_file: str) -> str:
    if not planned_file.endswith(".webp"):
        raise BatchPackageError(f"planned_file must be .webp: {planned_file}")
    return planned_file[: -len(".webp")]


def build_batch_package(
    manifest: Mapping, batch_number: int, *, manifest_path: str, manifest_sha256: str
) -> dict:
    """manifest のバッチ 1 つ分を、制作に渡す形にする (設計の値はそのまま写す)。"""

    if manifest.get("schema") != W15_SCHEMA:
        raise BatchPackageError("unexpected W1.5 manifest schema")
    batch = next((b for b in manifest["batches"] if b["batch"] == batch_number), None)
    if batch is None:
        raise BatchPackageError(f"batch {batch_number} is not in the manifest")
    by_id = {a["article_id"]: a for a in manifest["articles"]}
    items = []
    for order, article_id in enumerate(batch["article_ids"], start=1):
        article = by_id.get(article_id)
        if article is None:
            raise BatchPackageError(f"article {article_id} is in the batch but not in the manifest")
        if article["batch"] != batch_number:
            raise BatchPackageError(f"article {article_id} belongs to batch {article['batch']}")
        stem = _stem(article["planned_file"])
        item = {"order": order}
        item.update({key: article[key] for key in COPIED_FIELDS})
        item["files"] = {
            "background": f"backgrounds/{stem}-bg.png",
            "proof": f"proofs/{stem}-typeset-proof.png",
            "master_png": f"{stem}.png",
            "final_webp": article["planned_file"],
        }
        item["generation"] = generation_brief(article)
        item["typesetting"] = dict(article["typesetting"])
        item["typesetting_plan"] = typesetting_plan(
            article["typesetting"], article["headline"]["lines"], article["sublabel"]
        )
        items.append(item)
    return {
        "schema": SCHEMA,
        "phase": "W1.5B",
        "batch": batch_number,
        "family": batch["family"],
        "status": "ready for background generation (nothing generated, nothing uploaded)",
        "source": {"manifest": manifest_path, "sha256": manifest_sha256},
        "contact_sheet_order": list(batch["article_ids"]),
        "canvas": manifest["canvas"],
        "shared_prompt": manifest["shared_prompt"],
        "textless_suffix": TEXTLESS_SUFFIX,
        "negative_guard": list(NEGATIVE_GUARD),
        "items": items,
    }


def validate_batch_package(package: Mapping, manifest: Mapping, *, manifest_sha256: str) -> list:
    """パッケージが manifest の写しのままかを確かめる。問題の一覧 (空なら問題なし)。"""

    problems: list[str] = []
    if package.get("schema") != SCHEMA:
        problems.append("unexpected package schema")
    if package.get("source", {}).get("sha256") != manifest_sha256:
        problems.append("the package was built from a different manifest version")
    batch_number = package.get("batch")
    batch = next((b for b in manifest["batches"] if b["batch"] == batch_number), None)
    if batch is None:
        return [*problems, f"batch {batch_number} is not in the manifest"]
    ids = [item.get("article_id") for item in package.get("items", [])]
    if ids != batch["article_ids"]:
        problems.append(f"items {ids} do not match batch order {batch['article_ids']}")
    if package.get("contact_sheet_order") != batch["article_ids"]:
        problems.append("contact_sheet_order does not match the batch order")
    by_id = {a["article_id"]: a for a in manifest["articles"]}
    for item in package.get("items", []):
        article_id = item.get("article_id")
        article = by_id.get(article_id)
        if article is None:
            problems.append(f"article {article_id} is not in the manifest")
            continue
        for key in COPIED_FIELDS:
            if item.get(key) != article[key]:
                problems.append(f"article {article_id}: {key} differs from the manifest")
        if item.get("typesetting") != article["typesetting"]:
            problems.append(f"article {article_id}: typesetting differs from the manifest")
        expected_plan = typesetting_plan(
            article["typesetting"], article["headline"]["lines"], article["sublabel"]
        )
        plan = item.get("typesetting_plan")
        if plan != expected_plan:
            problems.append(f"article {article_id}: typesetting_plan is not the W1 layout")
        elif plan["sublabel"]["em_bottom"] > plan["safe_bottom_y"]:
            problems.append(f"article {article_id}: the sublabel runs into the bottom margin")
        if item.get("generation") != generation_brief(article):
            problems.append(f"article {article_id}: generation brief is not the committed prompt")
        for key in ("prompt", "negative_prompt"):
            if _JAPANESE.search(item.get("generation", {}).get(key, "")):
                problems.append(f"article {article_id}: {key} contains Japanese text")
        if item.get("files", {}).get("final_webp") != article["planned_file"]:
            problems.append(f"article {article_id}: final file is not the planned file")
    return problems


def render_handoff_markdown(package: Mapping) -> str:
    """制作する人に渡す説明 (パッケージの JSON から作る)。"""

    order = " → ".join(str(i) for i in package["contact_sheet_order"])
    source = package["source"]
    batch_dir = f"artifacts/featured-images/w1.5/batch-{package['batch']}"
    out = [
        f"# W1.5B バッチ {package['batch']} — featured image の制作パッケージ",
        "",
        f"- 系統: {package['family']}",
        f"- 元の設計: `{source['manifest']}` (sha256 `{source['sha256'][:12]}…`)",
        f"- 一覧 (contact sheet) の順: {order}",
        "- 状態: 背景はまだ作っていない。WordPress には何も書いていない。",
        "",
        "## 手順",
        "",
        "1. 各画像の **背景とモチーフだけ** を、下の文で作る (文字なし)。16:9 の PNG を",
        "   `backgrounds/<file>-bg.png` に置く (名前は各節)。",
        "2. 組版する (文字は画像モデルに描かせない):",
        "",
        "   ```",
        "   uv run --no-project --with pillow --with fonttools \\",
        f"     python scripts/compose_featured_image.py --dir {batch_dir} compose",
        "   ```",
        "",
        "3. 一覧の見本を作る: 同じコマンドの `contact-sheet`。文字だけの試し刷りは `proof`。",
        "4. `validation-checklist.md` を 1 枚ずつ確かめてから、人に見せる。",
        "",
        "## 共通",
        "",
        "- 1200×675、W1 の配置 (見出しは左下、モチーフは右、背景 `#F5F7FA`、下端に細い帯)。",
        "- 左上 (0, 0, 560, 170) はカテゴリラベルが重なるので空けておく。",
        "- 文字・擬似文字・ロゴ・製品画面・人物・ロボット・金額・日付を入れない。",
        f"- 画像生成の文の最後に、決まった文を足してある: `{package['textless_suffix']}`",
        "",
    ]
    for item in package["items"]:
        plan = item["typesetting_plan"]
        lines = " / ".join(f"「{line['text']}」" for line in plan["headline"]["lines"])
        dist = "; ".join(f"{d['article_id']}: {d['rule']}" for d in item["distinguish_from"])
        gen = item["generation"]
        files = item["files"]
        out += [
            f"## {item['order']}. article {item['article_id']} — {item['title']}",
            "",
            f"- slug: `{item['slug']}` / URL: {item['url']}",
            f"- 見出し: {lines} (104px) / 補助語: 「{item['sublabel']}」 (44px)",
            f"- アクセント色: `{item['accent']['color']}` ({item['accent']['family_name']})",
            f"- 背景のファイル: `{item['files']['background']}`",
            f"- 完成のファイル: `{files['final_webp']}` (master `{files['master_png']}`)",
            f"- alt: {item['alt_text']}",
            f"- media の title: {item['media_title']}",
            f"- モチーフ: {item['motif']}",
            f"- 見分ける相手: {dist}",
            "",
            "prompt:",
            "",
            "```text",
            gen["prompt"],
            "```",
            "",
            "negative prompt:",
            "",
            "```text",
            gen["negative_prompt"],
            "```",
            "",
        ]
    return "\n".join(out) + "\n"


def render_checklist_markdown(package: Mapping) -> str:
    """1 枚ずつ確かめる項目 (人が印を付ける)。"""

    out = [
        f"# W1.5B バッチ {package['batch']} — 確認項目",
        "",
        "背景 (文字なし) を受け取ったとき、組版したあと、一覧の見本を作ったときに確かめる。",
        "",
    ]
    for item in package["items"]:
        box = item["composition"]["motif_box_px"]
        out += [
            f"## article {item['article_id']} — `{item['files']['final_webp']}`",
            "",
            "背景:",
            "",
            "- [ ] 16:9 (1200×675 か、それより大きい 16:9)",
            "- [ ] 文字・数字・擬似文字・ロゴ・製品画面・人物・顔・手・ロボットが無い",
            "- [ ] 金額・通貨の記号・日付・カレンダーの数字が無い",
            "- [ ] 左半分と左上 (0, 0, 560, 170) が無地に近い",
            f"- [ ] 主モチーフが ({box[0]}, {box[1]}, {box[2]}, {box[3]}) の中、中心が "
            f"{tuple(item['composition']['motif_centre_px'])} 付近",
            f"- [ ] モチーフが設計どおり: {item['motif']}",
            "",
            "組版のあと:",
            "",
            "- [ ] 見出し・補助語が manifest のとおり (compose の報告で幅が列 568px 以内)",
            "- [ ] 320×180・160×90・126×71・120×68 で見出しが読める",
            "- [ ] カテゴリラベルの箱 (PC 62×22 / スマホ 58×17) に文字・モチーフの中心が隠れない",
            "- [ ] 1200×630 と 1200×600 に上下を切っても見出しとモチーフが欠けない",
            "- [ ] 1200×675・WebP・0 バイトでない・ファイル名が一致",
            "",
        ]
    order = " → ".join(str(i) for i in package["contact_sheet_order"])
    out += [
        "## 一覧の見本 (バッチ全体)",
        "",
        f"- [ ] {order} の順に並べ、試作 4 枚と並べても統一感がある",
        "- [ ] グレースケールでも形だけで見分けられる: "
        + "、".join(f"{item['article_id']} = {shape_summary(item)}" for item in package["items"]),
        "- [ ] 見出しが一覧のタイトルの繰り返しになっていない",
        "",
    ]
    return "\n".join(out) + "\n"
