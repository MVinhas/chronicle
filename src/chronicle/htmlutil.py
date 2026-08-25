"""HTML parsing, main-content extraction and sanitisation.

Produces the clean semantic subset the reader renders. Two extraction paths:
a source may supply explicit selectors, otherwise a readability-style density
scorer finds the article body. Everything then passes through the same
sanitiser, so the reader only ever sees a known-good tag vocabulary.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

ALLOWED_TAGS = {
    "p", "br", "hr", "div", "span", "section",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd",
    "blockquote", "q", "cite",
    "pre", "code", "kbd", "samp", "var",
    "em", "strong", "i", "b", "u", "s", "small", "mark", "sub", "sup", "abbr",
    "a", "img", "figure", "figcaption", "picture", "source",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "ruby", "rt", "rp", "time", "del", "ins", "wbr",
}

ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height", "srcset", "sizes"},
    "source": {"srcset", "type", "media"},
    "abbr": {"title"},
    "time": {"datetime"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "col": {"span"},
    "ol": {"start", "reversed", "type"},
    "blockquote": {"cite"},
    "q": {"cite"},
    "del": {"cite"}, "ins": {"cite"},
    "*": {"id"},
}

# Elements that never belong in a reading view.
STRIP_TAGS = {
    "script", "style", "noscript", "iframe", "object", "embed", "applet",
    "form", "input", "button", "select", "textarea", "label", "fieldset",
    "nav", "aside", "footer", "header", "menu", "dialog", "template",
    "svg", "canvas", "audio", "video", "map", "area", "link", "meta", "base",
}

_JUNK_WORDS = (
    "share", "sharedaddy", "social", "sidebar", "widget", "advert", "adsense",
    "adspace", "promo", "newsletter", "subscribe", "signup", "sign-up",
    "comment", "commentlist", "respond", "trackback", "pingback",
    "related", "recommend", "popular", "author-bio", "authorbox",
    "breadcrumb", "pagination", "pager", "nav-links", "post-navigation",
    "meta-nav", "skip-link", "screen-reader", "sr-only", "visually-hidden",
    "cookie", "gdpr", "banner", "toolbar", "menu", "masthead", "colophon",
    "jp-relatedposts", "addtoany", "sharedaddy", "wp-block-buttons",
    "footer", "site-header", "site-footer", "print-only", "noprint",
)
_JUNK_RE = re.compile("|".join(re.escape(w) for w in _JUNK_WORDS), re.I)

# Class/id fragments that suggest the *actual* article body.
_GOOD_RE = re.compile(
    r"article|body|content|entry|main|page|post|story|text|essay|markdown|prose",
    re.I)
_BAD_RE = re.compile(
    r"combx|comment|contact|foot|masthead|media|meta|outbrain|promo|related|"
    r"scroll|share|shopping|sidebar|sponsor|shoutbox|tags|tool|widget|nav|menu|"
    r"popup|modal|newsletter|subscribe|banner|ad-|-ad|advert", re.I)

BLOCK_TAGS = {"p", "div", "section", "article", "td", "blockquote", "pre", "li"}


def parse(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def _node_text_len(node: Tag) -> int:
    return len(node.get_text(" ", strip=True))


def _link_density(node: Tag) -> float:
    total = _node_text_len(node)
    if total == 0:
        return 1.0
    linked = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
    return min(1.0, linked / total)


def _alive(node) -> bool:
    """False once a node has been decomposed or detached by an earlier pass."""
    return isinstance(node, Tag) and getattr(node, "attrs", None) is not None \
        and not getattr(node, "_decomposed", False)


def _class_id_blob(node: Tag) -> str:
    if not _alive(node):
        return ""
    cls = node.get("class") or []
    if isinstance(cls, str):
        cls = [cls]
    return " ".join(cls) + " " + (node.get("id") or "")


def _class_score(node: Tag) -> float:
    blob = _class_id_blob(node)
    score = 0.0
    if _GOOD_RE.search(blob):
        score += 25
    if _BAD_RE.search(blob):
        score -= 25
    return score


def _tag_base_score(node: Tag) -> float:
    return {"article": 20.0, "section": 8.0, "div": 5.0, "main": 15.0,
            "pre": 3.0, "td": 3.0, "blockquote": 3.0}.get(node.name, 0.0)


def score_candidates(soup: BeautifulSoup) -> tuple[dict[int, float], dict[int, Tag]]:
    """Readability-style density scoring; returns ({id: score}, {id: node})."""
    scores: dict[int, float] = {}
    nodes: dict[int, Tag] = {}

    for para in soup.find_all(["p", "pre", "blockquote", "li", "td"]):
        text = para.get_text(" ", strip=True)
        if len(text) < 25:
            continue
        base = 1.0 + text.count(",") + text.count("，")
        base += min(len(text) / 100.0, 3.0)

        ancestors = [a for a in para.parents if isinstance(a, Tag)][:3]
        for depth, anc in enumerate(ancestors):
            if anc.name in ("html", "[document]"):
                continue
            key = id(anc)
            if key not in scores:
                scores[key] = _tag_base_score(anc) + _class_score(anc)
                nodes[key] = anc
            scores[key] += base / (1 + depth)

    for key, node in nodes.items():
        scores[key] *= (1.0 - _link_density(node))
    return scores, nodes


def extract_main(soup: BeautifulSoup, selectors: list[str] | None = None) -> Tag | None:
    """Find the article body: explicit selectors first, then density scoring."""
    for sel in selectors or []:
        try:
            found = soup.select_one(sel)
        except Exception:
            continue
        if found is not None and _node_text_len(found) > 120:
            return found

    for name in ("article", "main"):
        node = soup.find(name)
        if node is not None and _node_text_len(node) > 400:
            return node

    scores, nodes = score_candidates(soup)
    if not scores:
        return soup.body or soup

    best_key = max(scores, key=lambda k: scores[k])
    best = nodes[best_key]
    best_score = scores[best_key]

    # Prefer a parent if it scores nearly as well (avoids clipping the article).
    parent = best.parent
    while isinstance(parent, Tag) and parent.name not in ("body", "html", "[document]"):
        pkey = id(parent)
        if pkey in scores and scores[pkey] >= best_score * 0.92:
            best, best_score = parent, scores[pkey]
            parent = parent.parent
            continue
        break
    return best


# --------------------------------------------------------------------------
# sanitisation
# --------------------------------------------------------------------------

def _drop(node: Tag) -> None:
    node.decompose()


def _is_junk(node: Tag) -> bool:
    if not _alive(node):
        return False
    blob = _class_id_blob(node)
    if not blob.strip():
        return False
    if not _JUNK_RE.search(blob):
        return False
    # Don't discard something that is clearly carrying the article text.
    if _node_text_len(node) > 900 and _link_density(node) < 0.25:
        return False
    return True


_SRCSET_RE = re.compile(r"\s*([^\s,]+)(?:\s+([\d.]+)([wx]))?\s*(?:,|$)")


def _best_from_srcset(srcset: str) -> str | None:
    best, best_w = None, -1.0
    for m in _SRCSET_RE.finditer(srcset or ""):
        url, num, unit = m.group(1), m.group(2), m.group(3)
        if not url:
            continue
        w = float(num) if num else 1.0
        if unit == "x":
            w *= 1000
        if w > best_w:
            best, best_w = url, w
    return best


LAZY_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-srcset",
              "data-full-url", "data-orig-file", "data-large-file", "data-hi-res-src")


def _resolve_img(img: Tag, base_url: str) -> None:
    """Un-lazy images and pick the largest available source."""
    src = img.get("src")
    if not src or src.startswith("data:image/gif") or "blank" in (src or "").lower():
        for attr in LAZY_ATTRS:
            val = img.get(attr)
            if val:
                src = _best_from_srcset(val) if "srcset" in attr else val
                break
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        cand = _best_from_srcset(srcset)
        if cand and (not src or src.startswith("data:")):
            src = cand
        elif cand:
            src = cand
    if src:
        img["src"] = urljoin(base_url, src.strip())
    for attr in list(img.attrs):
        if attr not in ("src", "alt", "title", "width", "height"):
            del img[attr]


def sanitise(node: Tag, base_url: str, *, soup: BeautifulSoup | None = None) -> str:
    """Reduce an extracted node to the reader's allowed HTML vocabulary."""
    if node is None:
        return ""
    holder = parse("<div></div>")
    root = holder.div
    root.append(node.extract() if node.parent else node)

    for c in root.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for tag in list(root.find_all(list(STRIP_TAGS))):
        if _alive(tag):
            _drop(tag)
    for tag in list(root.find_all(True)):
        if not _alive(tag):
            continue
        if tag.name in ("figure", "figcaption", "picture"):
            continue
        if _is_junk(tag):
            _drop(tag)

    # Wayback toolbars and rewritten links.
    for tag in list(root.find_all(id=re.compile(r"^wm-|^donato", re.I))):
        if _alive(tag):
            _drop(tag)

    for img in list(root.find_all("img")):
        if not _alive(img):
            continue
        _resolve_img(img, base_url)
        if not img.get("src"):
            _drop(img)

    for a in list(root.find_all("a")):
        if not _alive(a):
            continue
        href = a.get("href")
        keep = {}
        if href and not href.lower().startswith(("javascript:", "data:", "vbscript:")):
            keep["href"] = _unwayback(urljoin(base_url, href.strip()))
        if a.get("title"):
            keep["title"] = a["title"]
        a.attrs = keep
        if "href" not in keep:
            a.unwrap()

    for tag in list(root.find_all(True)):
        if not _alive(tag):
            continue
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        allowed = ALLOWED_ATTRS.get(tag.name, set()) | ALLOWED_ATTRS["*"]
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag[attr]

    _promote_headings(root)
    _wrap_captions(root)
    _prune_empty(root)
    return root.decode_contents()


_WAYBACK_RE = re.compile(r"^https?://web\.archive\.org/web/\d+(?:id_|if_|cs_|js_)?/")


def _unwayback(url: str) -> str:
    """Rewrite archived links back to their original targets."""
    m = _WAYBACK_RE.match(url or "")
    return url[m.end():] if m else url


def _promote_headings(root: Tag) -> None:
    """Article bodies shouldn't contain h1 — the reader supplies the title."""
    for h in root.find_all("h1"):
        h.name = "h2"


_CAPTION_HINT = re.compile(r"caption|wp-caption-text|figcaption|image-?desc", re.I)


def _wrap_captions(root: Tag) -> None:
    """Turn WordPress-style caption divs into semantic <figure>."""
    for div in list(root.find_all(["div", "span", "p"])):
        if not _alive(div):
            continue
        blob = _class_id_blob(div)
        if not blob or not _CAPTION_HINT.search(blob):
            continue
        img = div.find("img")
        if img is None:
            continue
        text = div.get_text(" ", strip=True)
        fig = BeautifulSoup("<figure></figure>", "html.parser").figure
        fig.append(img.extract())
        if text:
            cap = BeautifulSoup("<figcaption></figcaption>", "html.parser").figcaption
            cap.string = text
            fig.append(cap)
        div.replace_with(fig)


_KEEP_EMPTY = {"img", "br", "hr", "td", "th", "source", "col", "wbr"}


def _prune_empty(root: Tag) -> None:
    for _ in range(3):
        removed = False
        for tag in list(root.find_all(True)):
            if not _alive(tag):
                continue
            if tag.name in _KEEP_EMPTY:
                continue
            if tag.find(list(_KEEP_EMPTY)):
                continue
            if not tag.get_text(strip=True):
                tag.decompose()
                removed = True
        if not removed:
            break


# --------------------------------------------------------------------------
# post-processing helpers
# --------------------------------------------------------------------------

def word_count(html: str) -> int:
    return len(parse(html).get_text(" ", strip=True).split())


def count_images(html: str) -> int:
    return len(parse(html).find_all("img"))


def make_excerpt(html: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", parse(html).get_text(" ", strip=True)).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut + "…"


def content_hash(html: str) -> str:
    norm = re.sub(r"\s+", " ", parse(html).get_text(" ", strip=True)).strip().lower()
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()


def meta_content(soup: BeautifulSoup, *names: str) -> str | None:
    """First matching <meta name=…> or <meta property=…> content value."""
    for name in names:
        tag = (soup.find("meta", attrs={"name": name})
               or soup.find("meta", attrs={"property": name})
               or soup.find("meta", attrs={"itemprop": name}))
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def page_title(soup: BeautifulSoup) -> str:
    for getter in (lambda: meta_content(soup, "og:title", "twitter:title"),
                   lambda: soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else None,
                   lambda: soup.title.get_text(strip=True) if soup.title else None):
        try:
            val = getter()
        except AttributeError:
            val = None
        if val:
            return re.sub(r"\s+", " ", val).strip()
    return "Untitled"


# Only separators that in practice introduce a site name. Dashes are excluded
# on purpose: "A Tale of Two Cities - Chapter One" is one title, not two.
_TITLE_SEP = re.compile(r"\s+[|\u00b7\u2022\u00bb]\s+")


def clean_title(title: str, *suffixes: str) -> str:
    """Strip the site name that page titles conventionally append.

    Almost every CMS renders <title> as "Post title | Site Name". Left alone
    that suffix repeats on every row of the reading queue, so remove it: first
    any site name we already know, then a trailing segment short enough to be a
    site name rather than part of the title.
    """
    t = re.sub(r"\s+", " ", title or "").strip()

    for suf in suffixes:
        if not suf:
            continue
        for sep in ("|", "\u00b7", "\u2013", "\u2014", "-", "\u2022"):
            for pattern in (f" {sep} {suf}", f"{suf} {sep} "):
                if pattern.strip() and t.endswith(pattern):
                    t = t[: -len(pattern)].strip()
                elif pattern.strip() and t.startswith(pattern):
                    t = t[len(pattern):].strip()

    parts = [p.strip() for p in _TITLE_SEP.split(t) if p.strip()]
    if len(parts) > 1:
        head, tail = " | ".join(parts[:-1]), parts[-1]
        # A trailing segment of a few short words is a site name, not a title.
        if len(head) >= 8 and len(tail) <= 40 and len(tail.split()) <= 5:
            t = head

    return t or "Untitled"


def image_urls(html: str) -> list[str]:
    out, seen = [], set()
    for img in parse(html).find_all("img"):
        src = img.get("src")
        if src and not src.startswith("data:") and src not in seen:
            seen.add(src)
            out.append(src)
    return out


def rewrite_image_srcs(html: str, mapping: dict[str, str]) -> str:
    """Point <img src> at locally cached copies."""
    soup = parse(html)
    for img in soup.find_all("img"):
        src = img.get("src")
        if src in mapping:
            img["src"] = mapping[src]
        elif src and src.startswith("http"):
            img["data-remote"] = "1"
    return soup.decode()
