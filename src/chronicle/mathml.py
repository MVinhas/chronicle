"""MathML to readable inline HTML (best effort).

The reader has no MathML renderer and the sanitiser (rightly) drops unknown
tags, so a book's equations would otherwise vanish or collapse into
unreadable runs of letters. This turns the common constructs into Unicode
text with <sup>/<sub>, e.g. ``E = mc<sup>2</sup>``. Anything unrecognised
degrades to its text content. Output is built from escaped text and a fixed
set of tags, so it is safe to hand to the sanitiser.
"""
from __future__ import annotations

from html import escape

from bs4 import NavigableString, Tag

# Accents that become combining characters on the base.
_COMBINING = {
    "→": "⃗", "⃗": "⃗",                # vector arrow
    "¯": "̄", "‾": "̄", "―": "̄", "_": "̄",
    "^": "̂", "ˆ": "̂", "~": "̃", "˜": "̃",
    "˙": "̇", ".": "̇", "¨": "̈",
}
_SKIP = {"annotation", "annotation-xml", "mphantom", "mprescripts", "none"}


def _name(node: Tag) -> str:
    return (node.name or "").split(":")[-1].lower()


def _kids(node: Tag) -> list[Tag]:
    return [c for c in node.children if isinstance(c, Tag)]


def _text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def to_html(math: Tag) -> str:
    try:
        out = _walk(math).strip()
    except RecursionError:
        # Absurdly deep nesting: keep the words, lose the layout.
        out = escape(_text(math))
    if not out:
        alt = math.get("alttext") or ""
        out = escape(alt)
    return out


def _seq(nodes) -> str:
    return "".join(_walk(n) for n in nodes)


def _group(node: Tag) -> str:
    """Render a numerator, base or similar; parenthesise if compound."""
    html = _walk(node).strip()
    plain = _text(node)
    if len(plain) <= 1 or plain.replace(".", "", 1).isalnum() and " " not in plain:
        return html
    return f"({html})"


def _walk(node) -> str:
    if isinstance(node, NavigableString):
        return escape(str(node)) if str(node).strip() else ""
    name = _name(node)
    kids = _kids(node)
    if name in _SKIP:
        return ""
    if name == "semantics":
        return _walk(kids[0]) if kids else ""
    if name == "mi":
        t = _text(node)
        return f"<i>{escape(t)}</i>" if len(t) == 1 and t.isalpha() else escape(t)
    if name in ("mn", "mtext", "ms"):
        return escape(_text(node))
    if name == "mo":
        t = _text(node)
        if t in ("(", ")", "[", "]", "{", "}", ",", "!", "|", "'", "′", "″"):
            return escape(t) + (" " if t == "," else "")
        return f" {escape(t)} " if t else ""
    if name == "mspace":
        return " "
    if name == "msup" and len(kids) >= 2:
        return f"{_walk(kids[0])}<sup>{_walk(kids[1])}</sup>"
    if name == "msub" and len(kids) >= 2:
        return f"{_walk(kids[0])}<sub>{_walk(kids[1])}</sub>"
    if name == "msubsup" and len(kids) >= 3:
        return (f"{_walk(kids[0])}<sub>{_walk(kids[1])}</sub>"
                f"<sup>{_walk(kids[2])}</sup>")
    if name in ("mover", "munder") and len(kids) >= 2:
        accent = _text(kids[1])
        if name == "mover" and accent in _COMBINING:
            base = _walk(kids[0])
            return base + _COMBINING[accent]
        tag = "sup" if name == "mover" else "sub"
        return f"{_walk(kids[0])}<{tag}>{_walk(kids[1])}</{tag}>"
    if name == "munderover" and len(kids) >= 3:
        return (f"{_walk(kids[0])}<sub>{_walk(kids[1])}</sub>"
                f"<sup>{_walk(kids[2])}</sup>")
    if name == "mmultiscripts" and kids:
        return _walk(kids[0])
    if name == "mfrac" and len(kids) >= 2:
        return f"{_group(kids[0])}/{_group(kids[1])}"
    if name == "msqrt":
        inner = _seq(kids).strip()
        return f"√{inner}" if len(_text(node)) <= 1 else f"√({inner})"
    if name == "mroot" and len(kids) >= 2:
        return f"<sup>{_walk(kids[1])}</sup>√({_walk(kids[0]).strip()})"
    if name == "mfenced":
        opn = escape(node.get("open", "("))
        cls = escape(node.get("close", ")"))
        sep = escape((node.get("separators") or ",")[:1])
        return opn + f"{sep} ".join(_walk(k).strip() for k in kids) + cls
    if name == "mtable":
        return "<br/>".join(_walk(r) for r in kids)
    if name in ("mtr", "mlabeledtr"):
        return "  ".join(_walk(c).strip() for c in kids)
    if name == "math":
        return _seq(node.children)
    # mrow, mstyle, mpadded, menclose, mtd, unknown: just the children
    return _seq(node.children)
