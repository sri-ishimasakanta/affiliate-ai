"""outcome_unknown な content-update run を、live な観測から事後照合するための
比較プリミティブ (D-D5A.1 §16-17 の「Human 解決」を機械可読にする最小の土台)。

``request_content_hash`` (意図した content) と ``*_wordpress_raw_content_hash``
(WordPress が保存している content) は別 namespace であり、同じ入力からでも
WordPress の正当な sanitize によって一致しない (D-D5A.1 §3)。よって「意図した
payload が実際に反映されたか」は hash の等値比較では判定できない。

この module は、その判定を **sanitize に対して頑健な 3 つの観測可能な性質** に
分解する。3 つとも一致したときに限り「意図した content が live に反映されている」
と見なす:

1. ``visible_text`` -- tag を除去し entity を戻し空白を畳んだ可読テキスト。
   inline style / class / 属性順の差異を吸収する。
2. ``hrefs`` -- ``<a href="...">`` の **順序付き** 全リスト。リンク先の
   すり替え・欠落を visible_text だけでは検出できないため独立に比較する。
3. ``headings`` -- ``h1``-``h6`` 開始タグの **順序付き** 列。見出し階層の
   崩れを検出する。

いずれも「一致 = 反映済み」の十分条件として扱うだけで、``succeeded`` run の
normative な baseline (``response_content_raw_hash``) を置き換えるものではない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

_TAG_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r"<a\b[^>]*?\bhref=[\"']([^\"']*)[\"']", re.IGNORECASE | re.DOTALL)
_HEADING_RE = re.compile(r"<(h[1-6])\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_visible_text(html: str) -> str:
    """tag を除去し entity を戻し、空白を全て落とした可読テキスト。"""

    return _WHITESPACE_RE.sub("", unescape(_TAG_RE.sub("", html or "")))


def extract_hrefs(html: str) -> tuple[str, ...]:
    """``<a href>`` の順序付きリスト (重複も保持する)。"""

    return tuple(unescape(m) for m in _HREF_RE.findall(html or ""))


def extract_heading_sequence(html: str) -> tuple[str, ...]:
    """``h1``-``h6`` 開始タグの順序付き列 (小文字化)。"""

    return tuple(m.lower() for m in _HEADING_RE.findall(html or ""))


@dataclass(frozen=True)
class ContentEquivalence:
    """意図した content と live な content の照合結果 (本文は一切含まない)。"""

    visible_text_matches: bool
    hrefs_match: bool
    headings_match: bool
    intended_visible_text_length: int
    observed_visible_text_length: int
    intended_href_count: int
    observed_href_count: int

    @property
    def equivalent(self) -> bool:
        return self.visible_text_matches and self.hrefs_match and self.headings_match

    def as_dict(self) -> dict[str, object]:
        return {
            "visible_text_matches": self.visible_text_matches,
            "hrefs_match": self.hrefs_match,
            "headings_match": self.headings_match,
            "intended_visible_text_length": self.intended_visible_text_length,
            "observed_visible_text_length": self.observed_visible_text_length,
            "intended_href_count": self.intended_href_count,
            "observed_href_count": self.observed_href_count,
            "equivalent": self.equivalent,
        }


def compare_wordpress_content(intended_html: str, observed_raw_html: str) -> ContentEquivalence:
    """意図した HTML と WordPress が保存している ``content.raw`` を照合する。"""

    intended_text = normalize_visible_text(intended_html)
    observed_text = normalize_visible_text(observed_raw_html)
    intended_hrefs = extract_hrefs(intended_html)
    observed_hrefs = extract_hrefs(observed_raw_html)
    return ContentEquivalence(
        visible_text_matches=intended_text == observed_text,
        hrefs_match=intended_hrefs == observed_hrefs,
        headings_match=(
            extract_heading_sequence(intended_html) == extract_heading_sequence(observed_raw_html)
        ),
        intended_visible_text_length=len(intended_text),
        observed_visible_text_length=len(observed_text),
        intended_href_count=len(intended_hrefs),
        observed_href_count=len(observed_hrefs),
    )
