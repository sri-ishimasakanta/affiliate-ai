"""app.seo.live_probe の単体テスト (C5.1)。

``httpx.MockTransport`` で実ネットワークに触れずに観測ロジックを検証する。
この module は **判定をしない** -- 事実だけを取り出すことを pin する。
"""

from __future__ import annotations

import httpx

from app.seo.live_probe import probe_url, visible_text_length

_HTML = """<!doctype html><html><head>
<title>RPAおすすめ｜選び方</title>
<link rel="canonical" href="https://bizfluxlab.com/rpa-tools/" />
<meta name="description" content="RPA の選び方" />
<meta name="robots" content="index, follow, max-image-preview:large" />
</head><body><h1>RPAおすすめ</h1><p>本文</p>
<script>var noise = "should not count";</script></body></html>"""


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, html=_HTML)


def test_head_facts_are_extracted() -> None:
    with _client(_ok) as client:
        probe = probe_url("https://bizfluxlab.com/rpa-tools/", client=client)
    assert probe.final_status == 200
    assert probe.canonical == "https://bizfluxlab.com/rpa-tools/"
    assert probe.robots_meta.startswith("index")
    assert probe.title == "RPAおすすめ｜選び方"
    assert probe.meta_description == "RPA の選び方"
    assert probe.h1_count == 1
    assert probe.h1_first == "RPAおすすめ"
    assert probe.declares_noindex is False
    assert probe.redirected is False


def test_noindex_meta_is_detected() -> None:
    def handler(request):
        return httpx.Response(
            200,
            html='<html><head><meta name="robots" content="noindex"></head><body>x</body></html>',
        )

    with _client(handler) as client:
        assert probe_url("https://x.test/", client=client).declares_noindex is True


def test_noindex_via_googlebot_meta_is_detected() -> None:
    def handler(request):
        return httpx.Response(
            200,
            html='<html><head><meta name="googlebot" content="noindex">'
            "</head><body>x</body></html>",
        )

    with _client(handler) as client:
        assert probe_url("https://x.test/", client=client).declares_noindex is True


def test_noindex_via_x_robots_tag_header_is_detected() -> None:
    def handler(request):
        return httpx.Response(
            200, html="<html><body>x</body></html>", headers={"X-Robots-Tag": "noindex, nofollow"}
        )

    with _client(handler) as client:
        probe = probe_url("https://x.test/", client=client)
    assert probe.x_robots_tag == "noindex, nofollow"
    assert probe.declares_noindex is True


def test_index_follow_is_not_mistaken_for_noindex() -> None:
    with _client(_ok) as client:
        assert probe_url("https://x.test/", client=client).declares_noindex is False


def test_redirect_chain_is_captured() -> None:
    def handler(request):
        if request.url.host.startswith("www."):
            return httpx.Response(301, headers={"Location": "https://x.test/a/"})
        return httpx.Response(200, html=_HTML)

    with _client(handler) as client:
        probe = probe_url("https://www.x.test/a/", client=client)
    assert probe.redirected is True
    assert probe.redirect_chain[0][0] == 301
    assert probe.final_url == "https://x.test/a/"


def test_transport_failure_is_reported_not_raised() -> None:
    def handler(request):
        raise httpx.ConnectError("boom")

    with _client(handler) as client:
        probe = probe_url("https://x.test/", client=client)
    assert probe.error == "ConnectError"
    assert probe.final_status is None


def test_visible_text_length_excludes_scripts_and_markup() -> None:
    assert visible_text_length("<p>abc</p><script>xxxxxxxxxx</script>") == 3
