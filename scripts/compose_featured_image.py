"""管理用 CLI: W1.5 の featured image を組版する (文字なしの背景 + 決まった位置の日本語)。

    uv run --no-project --with pillow --with fonttools python scripts/compose_featured_image.py \\
        --dir artifacts/featured-images/w1.5/batch-1 proof
    uv run --no-project --with pillow --with fonttools python scripts/compose_featured_image.py \\
        --dir artifacts/featured-images/w1.5/batch-1 compose [--article 7] [--force]
    uv run --no-project --with pillow --with fonttools python scripts/compose_featured_image.py \\
        --dir artifacts/featured-images/w1.5/batch-1 contact-sheet [--source proofs] [--with-pilots]
        [--compare-batch 1]

- 入力は ``prepare_featured_image_batch.py package`` が作った ``batch-manifest.json`` と、
  ``backgrounds/`` に置いた文字なしの背景。組む前に、パッケージが今の W1.5 manifest の
  写しのままかを確かめる (違えば何も書かない)。
- 書体の太さ・大きさ・色・座標はすべて ``typesetting_plan`` (= manifest の ``typesetting``) の
  とおり。palt (プロポーショナル詰め) は font の GPOS から読んで適用する (見出しだけ)。
- 出力: master の PNG と WebP (``<dir>/`` 直下。W1.4 の apply の道具の ``--dir`` と同じ置き方)、
  ``compose-report.json``。既にある完成のファイルは ``--force`` が無ければ上書きしない
  (承認済みの画像を黙って差し替えないため)。

WordPress にも DB にも触らない。Pillow と fontTools はこの道具のためだけに ``--with`` で使う
(プロジェクトの依存には足していない)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.wordpress.featured_image import webp_dimensions  # noqa: E402
from app.wordpress.featured_image_batch import (  # noqa: E402
    BACKGROUND,
    CANVAS,
    LABEL_ZONE,
    layout_positions,
    manifest_sha256,
    sha256_of,
    validate_batch_package,
)

MANIFEST = ROOT / "docs" / "operations" / "featured-image-w1.5-manifest.json"
FONT = Path(r"C:\Windows\Fonts\NotoSansJP-VF.ttf")
PILOT_DIR = ROOT / "artifacts" / "featured-images" / "pilot"
PILOT_FILES = (
    "featured-25-ai-business-efficiency.webp",
    "featured-23-chatgpt-business-plans.webp",
    "featured-24-ai-agents.webp",
    "featured-20-chatgpt-enterprise.webp",
)
HEADLINE_COLUMN = (72, 230, 568, 385)
QUIET_CHANNEL_DELTA = 28  # 背景色からこの差を超える画素を「何かが描かれている」とみなす
QUIET_LIMITS = {"label_zone": 0.05, "headline_column": 0.08}
ASPECT_TOLERANCE = 0.03
WEBP_OPTIONS = {"quality": 90, "method": 6}
# 一覧のカードの大きさと、重なるカテゴリラベルの箱 (featured-image-pipeline.md §1)。
THUMBS = ((320, 180, (62, 22)), (160, 90, None), (126, 71, (58, 17)), (120, 68, None))


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Fonts:
    """wght を固定した Noto Sans JP (fontTools で作って cache する) と palt の値。"""

    def __init__(self, source: Path, cache_dir: Path) -> None:
        from fontTools.ttLib import TTFont

        self.source = source
        self.source_sha256 = sha256_of(source)
        self._cache_dir = cache_dir
        self.paths: dict[int, Path] = {}
        self._fonts: dict[int, TTFont] = {}
        self.version = TTFont(source)["name"].getDebugName(5)

    def _font(self, weight: int):
        if weight not in self._fonts:
            from fontTools.ttLib import TTFont

            self.paths[weight] = self._instance(weight, self._cache_dir)
            self._fonts[weight] = TTFont(self.paths[weight])
        return self._fonts[weight]

    def _instance(self, weight: int, cache_dir: Path) -> Path:
        target = cache_dir / f"NotoSansJP-wght{weight}-{self.source_sha256[:12]}.ttf"
        if not target.exists():
            from fontTools.ttLib import TTFont
            from fontTools.varLib import instancer

            cache_dir.mkdir(parents=True, exist_ok=True)
            instance = instancer.instantiateVariableFont(TTFont(self.source), {"wght": weight})
            instance.save(target)
        return target

    def pil(self, weight: int, size: int):
        from PIL import ImageFont

        self._font(weight)
        return ImageFont.truetype(str(self.paths[weight]), size)

    def metrics(self, weight: int, text: str, *, palt: bool) -> tuple[dict, int]:
        font = self._font(weight)
        cmap = font.getBestCmap()
        hmtx = font["hmtx"]
        lookups = self._palt_lookups(font) if palt else []
        out = {}
        for char in set(text):
            glyph = cmap.get(ord(char))
            if glyph is None:
                raise SystemExit(f"the font has no glyph for {char!r}")
            x_placement = x_advance = 0
            for subtables in lookups:
                for sub in subtables:
                    glyphs = sub.Coverage.glyphs
                    if glyph in glyphs:
                        value = sub.Value if sub.Format == 1 else sub.Value[glyphs.index(glyph)]
                        x_placement += getattr(value, "XPlacement", 0) or 0
                        x_advance += getattr(value, "XAdvance", 0) or 0
                        break  # 1 つの lookup では最初に当たった subtable だけ
            out[char] = (hmtx[glyph][0], x_placement, x_advance)
        return out, font["head"].unitsPerEm

    @staticmethod
    def _palt_lookups(font) -> list[list]:
        gpos = font["GPOS"].table
        records = gpos.FeatureList.FeatureRecord
        script = next(s for s in gpos.ScriptList.ScriptRecord if s.ScriptTag == "DFLT")
        indices = sorted(
            {
                lookup
                for i in script.Script.DefaultLangSys.FeatureIndex
                if records[i].FeatureTag == "palt"
                for lookup in records[i].Feature.LookupListIndex
            }
        )
        lookups = []
        for index in indices:
            lookup = gpos.LookupList.Lookup[index]
            subs = [s.ExtSubTable if lookup.LookupType == 9 else s for s in lookup.SubTable]
            if any(getattr(s, "LookupType", 1) != 1 for s in subs):
                raise SystemExit("unexpected palt lookup type (expected single adjustment)")
            lookups.append(subs)
        return lookups


def _draw_text(draw, fonts: Fonts, weight, size, text, x, baseline, color, *, palt) -> float:
    metrics, upem = fonts.metrics(weight, text, palt=palt)
    positions, width = layout_positions(text, metrics, size=size, units_per_em=upem)
    pil_font = fonts.pil(weight, size)
    for char, dx in positions:
        draw.text((x + dx, baseline), char, font=pil_font, fill=_rgb(color), anchor="ls")
    return width


def typeset(image, item: dict, fonts: Fonts) -> dict:
    """背景の上に、見出しの印・見出し・補助語・下端の帯を置く (座標は plan のとおり)。"""

    from PIL import ImageDraw

    plan = item["typesetting_plan"]
    draw = ImageDraw.Draw(image)
    mark, bar = plan["mark"], plan["accent_bar"]
    draw.rectangle(
        (mark["x"], mark["y"], mark["x"] + mark["width"] - 1, mark["y"] + mark["height"] - 1),
        fill=_rgb(mark["color"]),
    )
    head = plan["headline"]
    widths = []
    for line in head["lines"]:
        width = _draw_text(
            draw,
            fonts,
            head["weight"],
            head["size_px"],
            line["text"],
            line["x"],
            line["baseline_y"],
            head["color"],
            palt=head["palt"],
        )
        if width > head["max_width_px"]:
            raise SystemExit(f"article {item['article_id']}: {line['text']!r} is {width}px wide")
        widths.append(width)
    sub = plan["sublabel"]
    sub_width = _draw_text(
        draw,
        fonts,
        sub["weight"],
        sub["size_px"],
        sub["text"],
        sub["x"],
        sub["baseline_y"],
        sub["color"],
        palt=sub["palt"],
    )
    if sub_width > sub["max_width_px"]:
        raise SystemExit(f"article {item['article_id']}: the sublabel is {sub_width}px wide")
    draw.rectangle(
        (bar["x"], bar["y"], bar["x"] + bar["width"] - 1, bar["y"] + bar["height"] - 1),
        fill=_rgb(bar["color"]),
    )
    return {"headline_width_px": widths, "sublabel_width_px": sub_width}


def prepare_background(path: Path):
    """16:9 の背景を 1200×675 にする。16:9 から 3% より外れたものは使わない。"""

    from PIL import Image

    with Image.open(path) as source:
        source.load()
        image = source.convert("RGB")
    width, height = image.size
    target = CANVAS[0] / CANVAS[1]
    if abs((width / height) / target - 1) > ASPECT_TOLERANCE:
        raise SystemExit(f"{path.name}: {width}x{height} is not 16:9 (within 3%)")
    info = {"source_size": [width, height], "cropped": False, "resized": False}
    if width / height > target:
        new_width = round(height * target)
        left = (width - new_width) // 2
        image = image.crop((left, 0, left + new_width, height))
        info["cropped"] = new_width != width
    elif width / height < target:
        new_height = round(width / target)
        top = (height - new_height) // 2
        image = image.crop((0, top, width, top + new_height))
        info["cropped"] = new_height != height
    if image.size != CANVAS:
        image = image.resize(CANVAS, Image.Resampling.LANCZOS)
        info["resized"] = True
    return image, info


def busy_fraction(image, box) -> float:
    """box の中で、背景色から目立って違う画素の割合 (0〜1)。"""

    from PIL import Image, ImageChops

    x, y, w, h = box
    region = image.crop((x, y, x + w, y + h))
    diff = ImageChops.difference(region, Image.new("RGB", region.size, _rgb(BACKGROUND)))
    r, g, b = diff.split()
    strongest = ImageChops.lighter(ImageChops.lighter(r, g), b)
    histogram = strongest.histogram()
    busy = sum(histogram[QUIET_CHANNEL_DELTA + 1 :])
    return round(busy / (w * h), 4)


def quiet_zone_report(image) -> dict:
    zones = {"label_zone": LABEL_ZONE, "headline_column": HEADLINE_COLUMN}
    report = {}
    for name, box in zones.items():
        fraction = busy_fraction(image, box)
        report[name] = {
            "busy_fraction": fraction,
            "limit": QUIET_LIMITS[name],
            "ok": fraction <= QUIET_LIMITS[name],
        }
    return report


def _save_outputs(image, directory: Path, item: dict, *, force: bool) -> dict:
    png = directory / item["files"]["master_png"]
    webp = directory / item["files"]["final_webp"]
    if not force and (png.exists() or webp.exists()):
        raise SystemExit(f"{webp.name} already exists; pass --force to replace it")
    image.save(png, "PNG")
    image.save(webp, "WEBP", **WEBP_OPTIONS)
    data = webp.read_bytes()
    if not data or webp_dimensions(data) != CANVAS:
        raise SystemExit(f"{webp.name} is not a 1200x675 WebP")
    return {
        "master_png": {"file": png.name, "sha256": sha256_of(png), "bytes": png.stat().st_size},
        "final_webp": {"file": webp.name, "sha256": _sha256_bytes(data), "bytes": len(data)},
    }


def _load_package(directory: Path) -> dict:
    package = json.loads((directory / "batch-manifest.json").read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems = validate_batch_package(package, manifest, manifest_sha256=manifest_sha256(MANIFEST))
    if problems:
        for problem in problems:
            print(f"problem: {problem}")
        raise SystemExit("the batch package does not match the W1.5 manifest; nothing written")
    return package


def _environment(fonts: Fonts) -> dict:
    import fontTools
    import PIL

    return {
        "pillow": PIL.__version__,
        "fonttools": fontTools.version,
        "font": {
            "file": fonts.source.name,
            "sha256": fonts.source_sha256,
            "version": fonts.version,
        },
        "webp": WEBP_OPTIONS,
    }


def _write_report(directory: Path, key: str, entries: dict, environment: dict) -> None:
    path = directory / "compose-report.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    report["environment"] = environment
    report.setdefault(key, {}).update(entries)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command_proof(directory: Path, package: dict, fonts: Fonts) -> int:
    """文字だけの試し刷り (無地の背景)。組版の座標と幅を先に確かめる。"""

    from PIL import Image, ImageDraw

    entries = {}
    for item in package["items"]:
        image = Image.new("RGB", CANVAS, _rgb(BACKGROUND))
        widths = typeset(image, item, fonts)
        proof = directory / item["files"]["proof"]
        proof.parent.mkdir(parents=True, exist_ok=True)
        image.save(proof, "PNG")
        guides = image.copy()
        draw = ImageDraw.Draw(guides)
        x, y, w, h = LABEL_ZONE
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(220, 38, 38), width=2)
        x, y, w, h = item["composition"]["motif_box_px"]
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(37, 99, 235), width=2)
        cx, cy = item["composition"]["motif_centre_px"]
        draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), outline=(37, 99, 235), width=2)
        x, y, w, h = HEADLINE_COLUMN
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(22, 163, 74), width=1)
        guides.save(proof.with_name(proof.stem + "-guides.png"), "PNG")
        entries[str(item["article_id"])] = {"file": item["files"]["proof"], **widths}
        print(f"proof {item['article_id']}: headline {widths['headline_width_px']} px")
    _write_report(directory, "proofs", entries, _environment(fonts))
    return 0


def command_compose(directory: Path, package: dict, fonts: Fonts, articles, force: bool) -> int:
    entries, missing = {}, []
    for item in package["items"]:
        if articles and item["article_id"] not in articles:
            continue
        background = directory / item["files"]["background"]
        if not background.exists():
            missing.append(item["files"]["background"])
            continue
        image, info = prepare_background(background)
        quiet = quiet_zone_report(image)
        widths = typeset(image, item, fonts)
        outputs = _save_outputs(image, directory, item, force=force)
        entries[str(item["article_id"])] = {
            "background": {"file": item["files"]["background"], "sha256": sha256_of(background)}
            | info,
            "quiet_zones": quiet,
            **widths,
            **outputs,
        }
        warn = [name for name, zone in quiet.items() if not zone["ok"]]
        note = f" (check: busy {', '.join(warn)})" if warn else ""
        print(f"composed {item['article_id']}: {outputs['final_webp']['file']}{note}")
    if entries:
        _write_report(directory, "composed", entries, _environment(fonts))
    for path in missing:
        print(f"missing background: {path}")
    return 1 if missing else 0


def command_contact_sheet(
    directory: Path, package: dict, fonts: Fonts, source, pilots, compare_batches=()
) -> int:
    from PIL import Image, ImageDraw, ImageOps

    tiles = []
    for item in package["items"]:
        name = item["files"]["proof"] if source == "proofs" else item["files"]["final_webp"]
        path = directory / name
        if not path.exists():
            raise SystemExit(f"missing {name}")
        tiles.append((str(item["article_id"]), path))
    for number in compare_batches:
        # 承認済みの別のバッチの完成画像 (同じ系列に見えるかを並べて確かめる)。
        other_dir = directory.parent / f"batch-{number}"
        other = json.loads((other_dir / "batch-manifest.json").read_text(encoding="utf-8"))
        for item in other["items"]:
            path = other_dir / item["files"]["final_webp"]
            if not path.exists():
                raise SystemExit(f"missing batch {number} final {path.name}")
            tiles.append((f"b{number}: {item['article_id']}", path))
    if pilots:
        tiles += [(f"pilot {p.split('-')[1]}", PILOT_DIR / p) for p in PILOT_FILES]
    images = []
    for label, path in tiles:
        with Image.open(path) as opened:
            images.append((label, opened.convert("RGB")))
    pad, caption = 16, 20
    rows = [*THUMBS, (320, 180, None)]  # 最後の行はグレースケール
    width = pad + len(images) * (320 + pad)
    height = pad + sum(h + caption + pad for _, h, _ in rows)
    sheet = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    caption_font = fonts.pil(500, 14)
    y = pad
    for row_index, (tw, th, label_box) in enumerate(rows):
        grey = row_index == len(rows) - 1
        for col, (label, image) in enumerate(images):
            x = pad + col * (320 + pad)
            thumb = image.resize((tw, th), Image.Resampling.LANCZOS)
            if grey:
                thumb = ImageOps.grayscale(thumb).convert("RGB")
            if label_box:
                box = Image.new("RGB", label_box, (51, 51, 51))
                thumb.paste(box, (0, 0))
            sheet.paste(thumb, (x, y))
            tag = f"{label}  {tw}x{th}" + ("  grey" if grey else "")
            draw.text((x, y + th + 3), tag, font=caption_font, fill=(74, 91, 112))
        y += th + caption + pad
    stem = f"contact-sheet-batch-{package['batch']}-{source}"
    outputs = [
        directory / f"{stem}.png",
        directory / f"{stem}-compare.png",
        directory / f"{stem}-mobile.png",
    ]
    sheet.save(outputs[0], "PNG")
    _compare_sheet(images, package, caption_font).save(outputs[1], "PNG")
    _mobile_sheet(images, caption_font).save(outputs[2], "PNG")
    for out in outputs:
        print(f"wrote {out}")
    return 0


def _guide_rows(package: dict) -> list[tuple[str, int]]:
    """比べやすくする横線: 印の上端・各行の baseline・補助語の baseline・帯の上端 (1200 基準)。"""

    plan = package["items"][0]["typesetting_plan"]
    rows = [("mark", plan["mark"]["y"])]
    rows += [
        (f"line {i + 1}", line["baseline_y"]) for i, line in enumerate(plan["headline"]["lines"])
    ]
    rows += [("sublabel", plan["sublabel"]["baseline_y"]), ("bar", plan["accent_bar"]["y"])]
    return rows


def _compare_sheet(images, package: dict, caption_font):
    """400×225 で並べ、全列に同じ横線を引く (上の段がこのバッチ、下の段が試作)。"""

    from PIL import Image, ImageDraw

    tw, th, pad, caption, per_row = 400, 225, 16, 20, 5
    rows = (len(images) + per_row - 1) // per_row
    sheet = Image.new(
        "RGB", (pad + per_row * (tw + pad), pad + rows * (th + caption + pad)), (255, 255, 255)
    )
    draw = ImageDraw.Draw(sheet)
    scale = tw / CANVAS[0]
    for index, (label, image) in enumerate(images):
        col, row = index % per_row, index // per_row
        x, y = pad + col * (tw + pad), pad + row * (th + caption + pad)
        sheet.paste(image.resize((tw, th), Image.Resampling.LANCZOS), (x, y))
        for _name, guide_y in _guide_rows(package):
            gy = y + round(guide_y * scale)
            draw.line((x, gy, x + tw - 1, gy), fill=(236, 72, 153), width=1)
        draw.text((x, y + th + 3), label, font=caption_font, fill=(74, 91, 112))
    names = ", ".join(f"{name} y={value}" for name, value in _guide_rows(package))
    draw.text((pad, sheet.height - 18), f"guides: {names}", font=caption_font, fill=(236, 72, 153))
    return sheet


def _mobile_sheet(images, caption_font):
    """126×71 (スマホの一覧) にラベルの箱を重ね、3 倍に拡大して並べる (画素の見え方を確かめる)。"""

    from PIL import Image, ImageDraw

    tw, th, zoom, pad, caption, per_row = 126, 71, 3, 16, 20, 6
    cell_w, cell_h = tw * zoom + pad, th * zoom + caption + pad
    columns = min(per_row, len(images))
    rows = (len(images) + per_row - 1) // per_row
    sheet = Image.new("RGB", (pad + columns * cell_w, pad + rows * cell_h), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    for index, (label, image) in enumerate(images):
        thumb = image.resize((tw, th), Image.Resampling.LANCZOS)
        thumb.paste(Image.new("RGB", (58, 17), (51, 51, 51)), (0, 0))
        x = pad + (index % per_row) * cell_w
        y = pad + (index // per_row) * cell_h
        sheet.paste(thumb.resize((tw * zoom, th * zoom), Image.Resampling.NEAREST), (x, y))
        draw.text(
            (x, y + th * zoom + 3), f"{label}  126x71 x3", font=caption_font, fill=(74, 91, 112)
        )
    return sheet


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--font", type=Path, default=FONT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("proof")
    compose = commands.add_parser("compose")
    compose.add_argument("--article", type=int, action="append", default=[])
    compose.add_argument("--force", action="store_true")
    sheet = commands.add_parser("contact-sheet")
    sheet.add_argument("--source", choices=("final", "proofs"), default="final")
    sheet.add_argument("--with-pilots", action="store_true")
    sheet.add_argument(
        "--compare-batch",
        type=int,
        action="append",
        default=[],
        help="別のバッチ (承認済み) の完成画像も並べる",
    )
    args = parser.parse_args(argv)

    package = _load_package(args.dir)
    fonts = Fonts(args.font, args.dir.parent / ".font-cache")
    if args.command == "proof":
        code = command_proof(args.dir, package, fonts)
    elif args.command == "compose":
        code = command_compose(args.dir, package, fonts, set(args.article), args.force)
    else:
        code = command_contact_sheet(
            args.dir, package, fonts, args.source, args.with_pilots, args.compare_batch
        )
    print("no WordPress or database access")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
