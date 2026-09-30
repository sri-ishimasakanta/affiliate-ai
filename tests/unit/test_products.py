"""N4 / N5: 自前の知識の商品 (手元だけ。公開・出品・販売・アップロードをしない)。

pin する契約:

- 候補は確かめた根拠から。サイト固有の候補は商品にしない。
- 検査: 出どころが無い / 言い回しが消えた・秘密・サイト固有の言葉・書きかけ・宣言していない
  ファイル・サイト固有の資産・配布の指定・壊れた JSON・記録の無い収益の主張は誤り。
- 内容の hash は版に依らず、資産が変われば変わる。承認は hash に結びつき、順番がある
  (品質の確認 → 内容の承認 → リリースの承認)。同じ版を 2 度リリースしない。
- 生成は再現できる (同じバイト)。manifest に時刻が無い。CHANGELOG の版の節が要る。
- 古さ: 出どころが変われば stale、資産が変われば patch の案。
- ネットワークを使わない。本物の商品 (products/) は検査を通る。
"""

from __future__ import annotations

import json
import shutil
import socket
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from app.products import build as pb
from app.products import records as rec
from app.products.candidates import discover
from app.products.spec import ProductError, check, load_policy, load_product
from app.social.note.sources import load_sources

REPO = Path(__file__).resolve().parents[2]
PID = "approval-gated-automation-kit"
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs/operations").mkdir(parents=True)
    shutil.copy2(REPO / "docs/project-roadmap.json", root / "docs/project-roadmap.json")
    shutil.copytree(REPO / "docs/decision-log", root / "docs/decision-log")
    for name in ("site-growth-operations.md", "threads-performance-analysis.md"):
        shutil.copy2(REPO / "docs/operations" / name, root / "docs/operations" / name)
    shutil.copytree(REPO / "products" / PID, root / "products" / PID)
    (root / "products" / PID / "records.json").unlink(missing_ok=True)
    return root


def _check(root: Path, policy=None):
    product = load_product(root, PID)
    return product, check(product, load_sources(root), policy=policy or load_policy())


def test_the_committed_product_passes_its_checks() -> None:
    product = load_product(REPO, PID)
    result = check(product, load_sources(REPO), policy=load_policy())
    assert result["ok"], result["errors"]
    assert len(result["sources"]) >= 4


def test_candidates_are_evidence_backed_and_skip_site_specific(tmp_path) -> None:
    root = _repo(tmp_path)
    found = {c["id"]: c for c in discover(root, load_sources(root))}
    assert found[PID]["eligible"] and found[PID]["product_exists"]
    assert not found["this-site-operations-runbook"]["eligible"]
    assert "site-specific" in found["this-site-operations-runbook"]["reason"]


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / "products" / PID / rel
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.mark.parametrize("rel, old, new, match", [
    ("assets/guide.md", "## 進め方", "## 進め方\n\nTODO: 書く", "unfinished"),
    ("assets/guide.md", "## 進め方", "## 進め方\n\nBizFluxLab の例", "site-specific term"),
    ("assets/guide.md", "## 進め方", "## 進め方\n\nC:\\Users\\me\\x.txt を開く",
     "secret or internal"),
    ("assets/guide.md", "## 進め方", "## 進め方\n\nこの方法で収益が伸びた。", "revenue"),
    ("assets/approval-policy.example.json", '"enabled": false\n    },\n    {\n      "name": "first',
     '"enabled": false,\n    },\n    {\n      "name": "first', "not valid JSON"),
    ("product.json", '"distribution": null', '"distribution": "note"', "distribution"),
    ("product.json", '"site_specific": false', '"site_specific": true', "site-specific assets"),
    ("product.json", '"phrase": "Automation boundary."', '"phrase": "no such phrase"',
     "no longer there"),
    ("product.json", '{"ref": "phase:C10"}', '{"ref": "phase:Z9"}', "not a complete phase"),
])
def test_checks_catch_unsafe_or_untraceable_content(tmp_path, rel, old, new, match) -> None:
    root = _repo(tmp_path)
    _edit(root, rel, old, new)
    _product, result = _check(root)
    assert not result["ok"] and any(match in e for e in result["errors"]), result["errors"]


def test_undeclared_files_and_missing_sources_are_errors(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / "products" / PID / "assets" / "notes.md").write_text("# x\n", encoding="utf-8")
    spec_path = root / "products" / PID / "product.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["assets"][0]["sources"] = []
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    _product, result = _check(root)
    assert any("not declared" in e for e in result["errors"])
    assert any("no source" in e for e in result["errors"])


def test_the_content_hash_ignores_the_version_but_not_the_assets(tmp_path) -> None:
    root = _repo(tmp_path)
    before = load_product(root, PID).content_hash
    _edit(root, "product.json", '"version": "0.1.0"', '"version": "0.1.1"')
    assert load_product(root, PID).content_hash == before
    _edit(root, "assets/guide.md", "## 進め方", "## 進め方 (改)")
    assert load_product(root, PID).content_hash != before


def test_builds_are_reproducible_and_manifests_have_no_timestamps(tmp_path) -> None:
    root = _repo(tmp_path)
    product, result = _check(root)
    policy = load_policy()
    first = pb.build_candidate(product, result["sources"], policy=policy, terms=[])
    second = pb.build_candidate(load_product(root, PID), result["sources"], policy=policy,
                                terms=[])
    assert first["package"] == second["package"] and not first["validation_errors"]
    text = json.dumps(first["manifest"])
    assert "2026" not in text and "at\"" not in text
    names = zipfile.ZipFile(BytesIO(first["package"])).namelist()
    assert f"{PID}-0.1.0/MANIFEST.json" in names and f"{PID}-0.1.0/assets/guide.md" in names
    (root / "products" / PID / "CHANGELOG.md").write_text("# 変更の記録\n", encoding="utf-8")
    broken = pb.build_candidate(load_product(root, PID), result["sources"], policy=policy,
                                terms=[])
    assert any("CHANGELOG" in e for e in broken["validation_errors"])


def _released(root: Path):
    product, result = _check(root)
    rec.record_quality_review(product, result, by="human", now=NOW,
                              checklist={k: True for k in rec.CHECKLIST})
    rec.record_content_approval(product, result, content_hash=product.content_hash, by="human",
                                now=NOW)
    candidate = pb.build_candidate(product, result["sources"], policy=load_policy(), terms=[])
    rec.record_release_approval(product, candidate, manifest_hash=candidate["manifest_hash"],
                                by="human", now=NOW)
    return candidate


def test_human_gates_are_ordered_and_hash_bound(tmp_path) -> None:
    root = _repo(tmp_path)
    product, result = _check(root)
    with pytest.raises(ProductError, match="checklist"):
        rec.record_quality_review(product, result, by="human", now=NOW, checklist={})
    with pytest.raises(ProductError, match="no quality review"):
        rec.record_content_approval(product, result, content_hash=product.content_hash,
                                    by="human", now=NOW)
    rec.record_quality_review(product, result, by="human", now=NOW,
                              checklist={k: True for k in rec.CHECKLIST})
    with pytest.raises(ProductError, match="different content hash"):
        rec.record_content_approval(product, result, content_hash="0" * 64, by="human", now=NOW)
    candidate = pb.build_candidate(product, result["sources"], policy=load_policy(), terms=[])
    with pytest.raises(ProductError, match="N4 comes first"):
        rec.record_release_approval(product, candidate, manifest_hash=candidate["manifest_hash"],
                                    by="human", now=NOW)
    rec.record_content_approval(product, result, content_hash=product.content_hash, by="human",
                                now=NOW)
    with pytest.raises(ProductError, match="different manifest"):
        rec.record_release_approval(product, candidate, manifest_hash="f" * 64, by="human",
                                    now=NOW)
    done = rec.record_release_approval(product, candidate,
                                       manifest_hash=candidate["manifest_hash"], by="human",
                                       now=NOW)
    assert "distribution is a separate" in done["meaning"]
    with pytest.raises(ProductError, match="already released"):
        rec.record_release_approval(product, candidate, manifest_hash=candidate["manifest_hash"],
                                    by="human", now=NOW)
    with pytest.raises(ProductError, match="no email"):
        rec.record_quality_review(product, result, by="a@b.c", now=NOW,
                                  checklist={k: True for k in rec.CHECKLIST})


def test_stale_detection_after_a_release(tmp_path) -> None:
    root = _repo(tmp_path)
    _released(root)
    product, result = _check(root)
    assert pb.stale_plan(product, result["sources"])["stale"] is False
    doc = root / "docs/operations/site-growth-operations.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    product, result = _check(root)
    plan = pb.stale_plan(product, result["sources"])
    assert plan["stale"] and plan["suggested_bump"] == "review" and plan["changed_sources"]
    _edit(root, "assets/guide.md", "## 進め方", "## 進め方 (改)")
    product, result = _check(root)
    plan = pb.stale_plan(product, result["sources"])
    assert plan["content_changed"] and plan["suggested_bump"] == "patch"
    assert "assets/guide.md" in plan["changed_files"]
    again = pb.build_candidate(product, result["sources"], policy=load_policy(), terms=[])
    assert any("not newer" in e for e in again["validation_errors"])  # 版を上げるのは人


def test_the_cli_runs_the_local_lifecycle_without_network(tmp_path, monkeypatch, capsys) -> None:
    from scripts.manage_products import main

    def refuse(*_a, **_k):
        raise AssertionError("products must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    root = _repo(tmp_path)

    def run(*argv):
        return main(list(argv), root=root, now=NOW, settings=None)

    assert run("check", PID) == 0
    assert run("build", PID) == 0
    assert run("verify", PID) == 0
    assert run("approve-release", PID, "--manifest-hash", "x", "--by", "human") == 2
    assert run("review", PID, "--by", "human", "--confirm-all") == 0
    content_hash = load_product(root, PID).content_hash
    assert run("approve-content", PID, "--content-hash", content_hash, "--by", "human") == 0
    candidate = json.loads((root / "reports/products" / PID / "0.1.0" / "candidate.json")
                           .read_text(encoding="utf-8"))
    assert run("approve-release", PID, "--manifest-hash", candidate["manifest_hash"],
               "--by", "human") == 0
    capsys.readouterr()
    assert run("status", PID) == 0
    status = capsys.readouterr().out
    assert '"0.1.0"' in status and "not decided" in status


def test_the_products_code_has_no_distribution_paths() -> None:
    for path in [*(REPO / "app/products").glob("*.py"), REPO / "scripts/manage_products.py"]:
        text = path.read_text(encoding="utf-8")
        for word in ("requests", "httpx", "urllib.request", "stripe", "gumroad", "shopify",
                     "smtplib", "subprocess", "app.wordpress", "app.social.threads"):
            assert word not in text.lower(), f"{path.name} contains {word!r}"
