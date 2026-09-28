"""小さな DOM (標準ライブラリの html.parser だけ)。観察したページの HTML を読むためのもの。

ブラウザの中で JavaScript を動かさずに、保存した HTML から決定的に取り出せるようにする
(試験は手元の HTML で行う)。対応する選び方は、要素名・属性の値・属性の前方一致・部分一致だけ。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
         "track", "wbr"}  # fmt: skip


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)

    def elements(self) -> Iterator[Node]:
        """自分より下の要素 (深さ優先、文書の順)。"""

        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.elements()

    def find_all(self, tag: str | None = None, **conditions) -> list[Node]:
        return [n for n in self.elements() if n.matches(tag, **conditions)]

    def find(self, tag: str | None = None, **conditions) -> Node | None:
        return next((n for n in self.elements() if n.matches(tag, **conditions)), None)

    def matches(self, tag: str | None = None, **conditions) -> bool:
        """``attr="v"`` 完全一致、``attr__prefix``・``attr__contains``・``attr__regex``。"""

        if tag is not None and self.tag != tag:
            return False
        for key, want in conditions.items():
            name, _, op = key.partition("__")
            name = name.replace("_", "-")
            value = self.attrs.get(name)
            if value is None:
                return False
            if op == "" and value != want:
                return False
            if op == "prefix" and not value.startswith(want):
                return False
            if op == "contains" and want not in value:
                return False
            if op == "regex" and not re.search(want, value):
                return False
        return True

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag in ("br",):
                parts.append("\n")
            elif child.tag not in ("script", "style"):
                parts.append(child.text())
        return "".join(parts)

    def ancestors(self) -> Iterator[Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs},
                    parent=self._stack[-1])  # fmt: skip
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs},
                    parent=self._stack[-1])  # fmt: skip
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def parse_html(html: str) -> Node:
    builder = _Builder()
    builder.feed(html or "")
    builder.close()
    return builder.root


__all__ = ["Node", "parse_html"]
