"""Just enough HTML parsing to assert on the dashboard by structure.

Checking rendered pages with substring matches breaks on whitespace and can't
tell text from markup - which is the whole point when the question is whether
user-supplied chat was escaped. `parse` builds a small element tree with the
standard library's parser; `Element.find` and `.text` are all the tests need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_NOT_TEXT = {"script", "style"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[Element | str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    @property
    def text(self) -> str:
        """Visible text of this element and everything inside it, with runs
        of whitespace collapsed."""
        return " ".join(" ".join(self._strings()).split())

    def _strings(self):
        for child in self.children:
            if isinstance(child, str):
                yield child
            elif child.tag not in _NOT_TEXT:
                yield from child._strings()

    def _has(self, name: str, value: str | bool) -> bool:
        name = name.replace("_", "-")
        if value is True:
            return name in self.attrs
        return self.attrs.get(name) == value

    def find(self, tag: str | None = None, class_: str | None = None, **attrs: str | bool) -> list[Element]:
        """Every descendant matching all the given criteria, in document order.

        An attribute is matched by its value; `True` asks only that it is
        there, which is how to find one that takes no value (`disabled`,
        `hidden`, a bare `data-` hook)."""
        found = []
        for child in self.children:
            if isinstance(child, str):
                continue
            if (
                (tag is None or child.tag == tag)
                and (class_ is None or class_ in child.classes)
                and all(child._has(name, value) for name, value in attrs.items())
            ):
                found.append(child)
            found.extend(child.find(tag, class_, **attrs))
        return found

    def one(self, tag: str | None = None, class_: str | None = None, **attrs: str | bool) -> Element:
        matches = self.find(tag, class_, **attrs)
        assert len(matches) == 1, f"expected exactly one <{tag or '*'} class={class_!r} {attrs}>, found {len(matches)}"
        return matches[0]


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self._open = [self.root]

    def handle_starttag(self, tag, attrs):
        element = Element(tag, dict(attrs))
        self._open[-1].children.append(element)
        if tag not in _VOID:
            self._open.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, 0, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return

    def handle_data(self, data):
        self._open[-1].children.append(data)


def parse(html: str) -> Element:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root
