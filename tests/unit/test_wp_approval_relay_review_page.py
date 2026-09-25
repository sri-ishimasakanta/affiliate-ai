"""携帯の承認ページが、承認する本文そのものを表示すること (T6.1)。

本番の症状: 提案 #5 の承認ページで「挿入される段落」の見出しと警告は出たが、本文の
枠が空だった。原因は 2 層: 中継の公開用 snapshot の許可リストが C9 の形しか知らず
``publish_text`` を落としていた / ページが常に ``inserted_paragraph`` を描いていた。

このテストは本物のページ (PHP の ``BFL_Approval_Render::shell``) を描き、その script を
Node で実行する (DOM と fetch は最小の代役)。中継の exchange が返す snapshot は、本物と
同じく ``bfl_approval_public_snapshot`` を通した値を使う。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LIB = _ROOT / "wordpress" / "mu-plugins" / "bizfluxlab-approval-relay" / "lib-core.php"

_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const [scriptPath, snapshotPath, clicks] = process.argv.slice(2);
const src = fs.readFileSync(scriptPath, 'utf8');
const snapshot = JSON.parse(fs.readFileSync(snapshotPath, 'utf8'));
const escHtml = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const app = { innerHTML: '' };
const elements = {};
const posts = [];
const document = {
  getElementById(id) {
    if (id === 'app') return app;
    if (!app.innerHTML.includes('id="' + id + '"')) return null;
    return (elements[id] = elements[id] || { onclick: null, value: 'reason text' });
  },
  createElement() {
    return { t: '', appendChild(n) { this.t += n.t; },
      get innerHTML() { return escHtml(this.t); } };
  },
  createTextNode(s) { return { t: s }; },
};
const ok = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
const fetch = (url, opts) => {
  const body = JSON.parse(opts.body);
  if (url === '/x') return ok({ nonce: 'N', snapshot });
  posts.push(body);
  return ok({ state: 'decided' });
};
const tick = () => new Promise((r) => setTimeout(r, 5));
const ctx = { document, fetch, location: { hash: '#cap', pathname: '/bfl-approval/s', reload() {} },
  history: { replaceState() {} }, JSON, String, Promise, setTimeout };
(async () => {
  vm.runInNewContext(src, ctx);
  await tick(); await tick();
  const steps = [{ html: app.innerHTML, ids: Object.keys(elements) }];
  for (const id of (clicks || '').split(',').filter(Boolean)) {
    const target = document.getElementById(id);
    if (!target || !target.onclick) { steps.push({ missing: id }); break; }
    target.onclick();
    await tick(); await tick();
    steps.push({ html: app.innerHTML });
  }
  process.stdout.write(JSON.stringify({ steps, posts }));
})();
"""


def _find(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if name == "php":
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        for exe in packages.glob("PHP.PHP*/php.exe"):
            return str(exe)
    return None


_PHP, _NODE = _find("php"), _find("node")
pytestmark = pytest.mark.skipif(
    not (_PHP and _NODE), reason="php and node are needed to execute the review page"
)


def _php(code: str) -> str:
    result = subprocess.run(
        [_PHP, "-r", f"require {json.dumps(str(_LIB))}; {code}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def _public(snapshot: dict) -> dict:
    """本物の中継と同じ許可リストを通す。"""

    encoded = json.dumps(snapshot, ensure_ascii=False)
    return json.loads(
        _php(
            "echo json_encode(bfl_approval_public_snapshot("
            f"json_decode({json.dumps(encoded)}, true), '2026-09-26 16:05'), "
            "JSON_UNESCAPED_UNICODE);"
        )
    )


@pytest.fixture(scope="module")
def page_script(tmp_path_factory) -> Path:
    html = _php("echo BFL_Approval_Render::shell(str_repeat('a', 32), '/x', '/y', 'n0nce');")
    script = re.search(r"<script[^>]*>(.*)</script>", html, re.S).group(1)
    path = tmp_path_factory.mktemp("relay") / "page.js"
    path.write_text(script, encoding="utf-8")
    return path


def _run(page_script: Path, tmp_path: Path, snapshot: dict, clicks: str = "") -> dict:
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps(_public(snapshot), ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run(
        [_NODE, str(harness), str(page_script), str(snap), clicks],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(out.stdout)


_URL = "https://bizfluxlab.com/ai-transcription-tools/?utm_source=threads&utm_medium=social&utm_campaign=article-3-question"
#: 提案 #5 と同じ性質: 日本語の本文、記事への URL、警告あり。
_PROPOSAL_5 = (
    "手元の録音ファイル、会議中じゃなくても文字起こしAIに任せられる？\n"
    "Krisp と Fireflies.ai は、どちらもファイルのアップロード文字起こしを"
    "公式に記載（2026年9月時点）。\n"
    "日本語の精度は公式の記載だけでは分からない。実際の音声で試してから決めるのが安全。\n"
    f"{_URL}"
)


def _threads(text=_PROPOSAL_5, warnings=None, **extra) -> dict:
    snapshot = {
        "subject_type": "threads_post",
        "subject_id": 5,
        "subject_version": 1,
        "subject_hash_short": "c0ffee0123456789",
        "article_title": "文字起こしAIおすすめ｜選び方と目的別の比較",
        "angle": "question",
        "link_mode": "article",
        "character_count": len(text) if isinstance(text, str) else 0,
        "warnings": ["2 questions; one natural hook is enough"] if warnings is None else warnings,
        "publish_text": text,
    }
    snapshot.update(extra)
    return snapshot


def _escaped(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# == the production failure =====================================================
def test_the_proposal_5_review_page_shows_the_exact_text(page_script, tmp_path) -> None:
    html = _run(page_script, tmp_path, _threads())["steps"][0]["html"]
    assert "投稿される本文" in html
    assert "挿入される段落" not in html
    assert f'<pre id="t">{_escaped(_PROPOSAL_5)}</pre>' in html  # 改行も含めてそのまま
    assert _escaped(_URL) in html  # URL は文字として見える
    assert "2 questions; one natural hook is enough" in html  # 警告と共存する
    assert 'id="a"' in html and 'id="r"' in html


def test_zero_warnings_still_render_the_text(page_script, tmp_path) -> None:
    html = _run(page_script, tmp_path, _threads(warnings=[]))["steps"][0]["html"]
    assert _escaped(_PROPOSAL_5) in html
    assert 'class="warn"' not in html


def test_html_in_the_text_is_escaped_not_executed(page_script, tmp_path) -> None:
    text = "比較 <script>alert(1)</script> & <b>太字</b> > 以上"
    html = _run(page_script, tmp_path, _threads(text=text))["steps"][0]["html"]
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;太字&lt;/b&gt; &gt; 以上" in html


@pytest.mark.parametrize("text", [None, "", "   \n  "])
def test_missing_text_fails_closed(page_script, tmp_path, text) -> None:
    snapshot = _threads()
    if text is None:
        snapshot.pop("publish_text")
    else:
        snapshot["publish_text"] = text
    result = _run(page_script, tmp_path, snapshot, clicks="a")
    html = result["steps"][0]["html"]
    assert "投稿される本文を表示できません。この画面からは承認できません。" in html
    assert 'id="a"' not in html  # 承認ボタンを出さない
    assert 'id="r"' in html  # 却下はできる
    assert result["steps"][1] == {"missing": "a"}
    assert result["posts"] == []


# == approve / reject flows ======================================================
def test_the_approve_flow_confirms_with_the_same_text(page_script, tmp_path) -> None:
    result = _run(page_script, tmp_path, _threads(), clicks="a,y")
    confirm = result["steps"][1]["html"]
    assert "この投稿案を承認しますか" in confirm
    assert f"<pre>{_escaped(_PROPOSAL_5)}</pre>" in confirm
    assert result["posts"] == [
        {
            "relay_session_id": "a" * 32,
            "decision": "approved",
            "reason": None,
            "nonce": "N",
        }
    ]
    assert "承認を受け付けました。" in result["steps"][2]["html"]


def test_the_reject_flow_still_works(page_script, tmp_path) -> None:
    result = _run(page_script, tmp_path, _threads(), clicks="r,y")
    assert result["posts"][0]["decision"] == "rejected"
    assert result["posts"][0]["reason"] == "reason text"
    assert "却下を受け付けました。" in result["steps"][2]["html"]


def test_the_c9_change_request_page_is_unchanged(page_script, tmp_path) -> None:
    snapshot = {
        "subject_type": "change_request",
        "subject_id": 12,
        "subject_version": 2,
        "subject_hash_short": "abcdef0123456789",
        "article_title": "記事",
        "rationale": "内部リンクを足す",
        "inserted_paragraph": "あわせて読みたい: AI議事録の選び方",
        "warnings": [],
    }
    html = _run(page_script, tmp_path, snapshot)["steps"][0]["html"]
    assert "挿入される段落" in html
    assert '<pre id="t">あわせて読みたい: AI議事録の選び方</pre>' in html
    assert "<dt>変更要求</dt><dd>#12</dd>" in html
    assert 'id="a"' in html
