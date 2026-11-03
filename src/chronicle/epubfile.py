"""Reading a local EPUB file safely.

An EPUB is a zip of XHTML, images and XML metadata supplied by whoever made
the file, so it is treated as untrusted input:

* nothing is ever extracted to disk (no zip-slip); members are read in memory
  and any member name that is absolute or climbs out with ``..`` rejects the
  whole file;
* entry count, per-entry size, total size and compression ratio (per member
  and for the archive as a whole) are capped, every read is bounded, and the
  bytes actually decompressed are totalled against a per-book budget, so a
  zip bomb cannot exhaust memory or time;
* XML metadata is parsed with the standard library, whose parser never loads
  external entities; a pre-pass with expat's own handlers refuses any entity
  declaration or internal DTD subset whatever the file's encoding;
  chapter markup goes through the lenient HTML parser, which has no entity
  machinery at all, and absurdly deep nesting is refused;
* scripts are never run, and every chapter passes through the shared
  ``htmlutil.sanitise`` before it can reach the reader;
* DRM-protected books are refused. Chronicle does not remove DRM.

Chapters come from the nav document (EPUB 3), else the NCX (EPUB 2), else the
spine order. One chapter per top-level table-of-contents entry. Where several top-level
entries point into one file (a Project Gutenberg book is a handful of big
files with hundreds of #anchors) the file is cut at those anchors, before the
ids are stripped. Entries nested below a top-level one in the same file are
part of it; nested entries in files of their own are chapters of their own.
Spine files the contents never name continue the chapter before them, and
pure front matter (cover, title page, copyright, contents page) is skipped:
a name, epub:type or guide entry only makes a page suspect, and short or
link-only text then confirms it.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import threading
import zipfile
from xml.parsers import expat
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from bs4 import (Comment, Declaration, Doctype, NavigableString,
                 ProcessingInstruction, Tag)
from bs4.dammit import UnicodeDammit

from . import htmlutil, mathml

MAX_ENTRIES = 20_000
MAX_ENTRY_BYTES = 64_000_000          # one uncompressed member
MAX_TOTAL_BYTES = 1_000_000_000       # all members, as declared
MAX_TEXT_BYTES = 16_000_000           # one XML/XHTML member
MAX_READ_BYTES = 200_000_000          # decompressed bytes read for one book
FRONT_MAX_WORDS = 150                 # longer than this is never "just" front matter
MAX_NEST = 200                        # element nesting in a chapter
MAX_RATIO = 200                       # uncompressed : compressed ...
RATIO_FLOOR = 1_000_000               # ... but only past this size

ASSET_PREFIX = "epub-asset:/"

# Font obfuscation is part of the format and is not DRM.
_FONT_ALGORITHMS = {"http://www.idpf.org/2008/embedding",
                    "http://ns.adobe.com/pdf/enc#RC"}
_FRONT_TYPES = {"cover", "titlepage", "halftitlepage", "copyright-page",
                "toc", "colophon"}
_GUIDE_FRONT = {"cover", "title-page", "titlepage", "copyright-page", "toc",
                "half-title"}
_FRONT_STEM = re.compile(
    r"^(cover|title[-_ ]?page|half[-_ ]?title|copyright|toc|contents|nav)"
    r"[-_ ]?\d*$")
_FRONT_TITLE = re.compile(
    r"^(cover|title page|half title|copyright( page)?|table of contents|"
    r"contents|colophon)$", re.I)


class EpubError(Exception):
    """The file cannot be read as a book; the message is meant for the user."""


class _Zip(zipfile.ZipFile):
    """A ZipFile that totals the bytes actually decompressed from it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.budget = MAX_READ_BYTES


@dataclass
class Chapter:
    href: str                  # path of its (first) document inside the zip
    title: str
    html: str                  # sanitised reader HTML
    order: int = 0
    key: str = ""              # identity in the book: href, or href#anchor

    def __post_init__(self):
        self.key = self.key or self.href


@dataclass
class Book:
    path: str
    title: str = ""
    author: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _asset_budget: int = field(default=0, repr=False)

    def __post_init__(self):
        self._asset_budget = MAX_READ_BYTES

    def read_asset(self, name: str) -> bytes | None:
        """Bytes of a member (an image), or None if absent or refused.

        The file is opened and re-checked for every call and closed again, so
        nothing stays open between syncs and a file swapped since it was
        imported is held to the same limits as when it was opened.
        """
        with self._lock:
            try:
                with _Zip(self.path) as zf:
                    _check_archive(zf)
                    zf.budget = self._asset_budget
                    data = _read(zf, name, MAX_ENTRY_BYTES)
                    self._asset_budget = zf.budget
                    return data
            except (EpubError, OSError, KeyError, zipfile.BadZipFile):
                return None

    def close(self) -> None:
        """Nothing is held open; kept so callers may treat a Book as a resource."""


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except FileNotFoundError:
        raise EpubError(f"Book file not found: {path}") from None
    except OSError as exc:
        raise EpubError(f"Cannot read {path}: {exc.strerror or exc}") from None
    return h.hexdigest()


def store_copy(src: str, digest: str) -> str:
    """Copy a book into the library (books/<sha256>.epub); return the path.

    The library keeps its own copy so a book survives the original being
    moved, deleted or (under Flatpak) reachable only through a session-bound
    document-portal path. Idempotent: an existing copy is reused.
    """
    from . import paths
    target = paths.BOOK_DIR / f"{digest}.epub"
    if target.exists():
        return str(target)
    try:
        paths.BOOK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
    except FileNotFoundError:
        raise EpubError(f"Book file not found: {src}") from None
    except OSError as exc:
        raise EpubError(f"Cannot copy {src} into the library: "
                        f"{exc.strerror or exc}") from None
    return str(target)


def prune_copies(keep: set[str]) -> int:
    """Delete library copies no source refers to (paths in `keep` stay)."""
    from . import paths
    removed = 0
    if not paths.BOOK_DIR.is_dir():
        return 0
    wanted = {os.path.realpath(k) for k in keep}
    for f in paths.BOOK_DIR.glob("*.epub"):
        if os.path.realpath(f) not in wanted:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# -- zip safety -----------------------------------------------------------

def _safe_name(name: str) -> bool:
    n = name.replace("\\", "/")
    if not n or n.startswith("/") or re.match(r"^[A-Za-z]:", n) or "\x00" in n:
        return False
    return ".." not in n.split("/")


def _check_archive(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise EpubError(f"Refusing this file: {len(infos)} entries is more "
                        f"than a book should have")
    total = packed = 0
    for info in infos:
        if not _safe_name(info.filename):
            raise EpubError(f"Refusing this file: unsafe path "
                            f"{info.filename!r} inside the archive")
        if info.flag_bits & 0x1:
            raise EpubError("This EPUB is encrypted (DRM-protected or "
                            "password-protected); Chronicle cannot read it")
        if info.file_size > MAX_ENTRY_BYTES:
            raise EpubError(f"Refusing this file: {info.filename!r} is "
                            f"implausibly large")
        if (info.file_size > RATIO_FLOOR
                and info.file_size > MAX_RATIO * max(1, info.compress_size)):
            raise EpubError(f"Refusing this file: {info.filename!r} looks "
                            f"like a compression bomb")
        total += info.file_size
        packed += max(1, info.compress_size)
        if total > MAX_TOTAL_BYTES:
            raise EpubError("Refusing this file: its contents are "
                            "implausibly large")
    # Many members each under the per-member floor can still add up to a bomb.
    if total > RATIO_FLOOR and total > MAX_RATIO * packed:
        raise EpubError("Refusing this file: it looks like a compression bomb")


def _read(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    budget = getattr(zf, "budget", None)
    if budget is not None:
        limit = min(limit, budget)
    try:
        with zf.open(name) as fh:
            data = fh.read(limit + 1)      # bounded whatever the header says
    except KeyError:
        raise EpubError(f"The book is missing {name}") from None
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise EpubError(f"Cannot read {name} from the book: {exc}") from None
    if len(data) > limit:
        raise EpubError(f"Refusing this file: {name!r} is implausibly large")
    if budget is not None:
        zf.budget -= len(data)
    return data


# -- XML helpers ----------------------------------------------------------

def _xml(data: bytes) -> ET.Element:
    # expat never fetches external entities; refusing declarations also
    # closes the internal-entity expansion ("billion laughs") route. The check
    # runs inside expat, after it has decoded the bytes, so no encoding
    # (UTF-16, say) can hide a declaration from it.
    refused = []

    def refuse(*_args):
        refused.append(True)
        raise expat.ExpatError("entity declaration")

    guard = expat.ParserCreate()
    guard.EntityDeclHandler = refuse
    guard.StartDoctypeDeclHandler = (
        lambda _n, _s, _p, internal: refuse() if internal else None)
    try:
        guard.Parse(data, True)
        return ET.fromstring(data)
    except (expat.ExpatError, ET.ParseError) as exc:
        if refused:
            raise EpubError("Refusing this file: its metadata declares XML "
                            "entities") from None
        raise EpubError(f"The book's metadata is malformed: {exc}") from None


def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _all(el: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in el.iter() if _local(e) == name]


def _first(el: ET.Element, name: str) -> ET.Element | None:
    found = _all(el, name)
    return found[0] if found else None


def _resolve(base: str, ref: str) -> str | None:
    """Zip path a relative reference points at, or None if external/unsafe."""
    if not ref:
        return None
    parsed = urlparse(ref)
    if parsed.scheme or parsed.netloc:
        return None
    rel = unquote(parsed.path)
    if not rel:
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), rel)
                              if not rel.startswith("/") else rel.lstrip("/"))
    if path.startswith("../") or path == ".." or path.startswith("/"):
        return None
    return path


# -- opening --------------------------------------------------------------

def open_book(path: str) -> Book:
    try:
        zf = _Zip(path)
    except FileNotFoundError:
        raise EpubError(f"Book file not found: {path}") from None
    except zipfile.BadZipFile:
        raise EpubError("This is not a valid EPUB file (not a zip archive)") from None
    except OSError as exc:
        raise EpubError(f"Cannot read {path}: {exc.strerror or exc}") from None
    with zf:
        _check_archive(zf)
        names = set(zf.namelist())
        _reject_drm(zf, names)
        if "META-INF/container.xml" not in names:
            raise EpubError("This is not a valid EPUB file (no container.xml)")
        container = _xml(_read(zf, "META-INF/container.xml", MAX_TEXT_BYTES))
        root_el = _first(container, "rootfile")
        opf_path = _resolve("", root_el.get("full-path", "")) if root_el is not None else None
        if not opf_path or opf_path not in names:
            raise EpubError("This is not a valid EPUB file (no package document)")
        opf = _xml(_read(zf, opf_path, MAX_TEXT_BYTES))
        try:
            return _build(zf, names, path, opf_path, opf)
        except RecursionError:
            raise EpubError("Refusing this file: its markup is nested "
                            "implausibly deep") from None


def _reject_drm(zf: zipfile.ZipFile, names: set[str]) -> None:
    if "META-INF/encryption.xml" in names:
        enc = _xml(_read(zf, "META-INF/encryption.xml", MAX_TEXT_BYTES))
        for m in _all(enc, "EncryptionMethod"):
            if m.get("Algorithm") not in _FONT_ALGORITHMS:
                raise EpubError(
                    "This EPUB is DRM-protected. Chronicle can only read "
                    "DRM-free books and does not remove DRM.")
    if "META-INF/rights.xml" in names:
        raise EpubError("This EPUB is DRM-protected. Chronicle can only read "
                        "DRM-free books and does not remove DRM.")


def _build(zf, names, path, opf_path, opf) -> Book:
    book = Book(path=path)
    meta = _first(opf, "metadata")
    if meta is not None:
        t, a = _first(meta, "title"), _first(meta, "creator")
        book.title = " ".join((t.text or "").split()) if t is not None else ""
        book.author = " ".join((a.text or "").split()) if a is not None else ""

    manifest = {}
    for item in _all(opf, "item"):
        href = _resolve(opf_path, item.get("href", ""))
        if item.get("id") and href:
            manifest[item.get("id")] = (href, item.get("media-type", ""),
                                        (item.get("properties") or "").split())
    spine_el = _first(opf, "spine")
    spine: list[str] = []
    if spine_el is not None:
        for ref in _all(spine_el, "itemref"):
            item = manifest.get(ref.get("idref"))
            if (item and item[0] in names and item[0] not in spine
                    and ("xhtml" in item[1] or item[1] == "text/html")):
                spine.append(item[0])
    if not spine:
        raise EpubError("This book has no readable chapters (empty reading "
                        "order)")

    toc: list[_Entry] = []
    front: set[str] = set()
    nav = next((h for h, _m, p in manifest.values() if "nav" in p), None)
    ncx_candidates = []
    if spine_el is not None and spine_el.get("toc") in manifest:
        ncx_candidates.append(manifest[spine_el.get("toc")][0])
    ncx_candidates += [h for h, m, _p in manifest.values()
                       if m == "application/x-dtbncx+xml"]
    ncx = next((c for c in ncx_candidates if c in names), None)
    if nav and nav in names:
        toc, front = _read_nav(zf, nav)
    if not toc and ncx:
        toc = _read_ncx(zf, ncx)
    guide = _first(opf, "guide")
    if guide is not None:
        for ref in _all(guide, "reference"):
            if (ref.get("type") or "").lower() in _GUIDE_FRONT:
                target = _resolve(opf_path, (ref.get("href") or "").split("#")[0])
                if target:
                    front.add(target)

    by_doc: dict[str, list[_Entry]] = {}
    for e in toc:
        by_doc.setdefault(e.href, []).append(e)

    chapters: list[Chapter] = []
    for doc in spine:
        entries = by_doc.get(doc, [])
        # One chapter per top-level entry: where a file holds several
        # top-level entries (one big file, a contents of #anchors) it is cut
        # at them; deeper entries only count in files that have no shallower
        # one (a part page listing chapter files of their own).
        if entries:
            top = min(e.depth for e in entries)
            entries = [e for e in entries if e.depth == top]
        raw = _read(zf, doc, MAX_TEXT_BYTES)
        pieces, doc_title, types = _prepare(raw, doc, names, entries)
        for piece in pieces:
            if _is_front(piece, doc, doc_title, front, types, nav):
                continue
            label = piece.title or piece.heading or doc_title
            continuation = (bool(toc) and not piece.from_toc
                            and bool(chapters))
            # A continuation keeps its own heading: it is not the chapter's
            # title repeated, it is the next part of the chapter.
            html = _render(piece.body, "" if continuation else label)
            if not html:
                continue
            if continuation:
                chapters[-1].html += html
                continue
            chapters.append(Chapter(
                doc, label or f"Part {len(chapters) + 1}", html,
                key=f"{doc}#{piece.frag}" if piece.frag else doc))

    if not chapters:
        raise EpubError("This book has no readable chapters")
    for i, ch in enumerate(chapters):
        ch.order = i
    book.chapters = chapters
    if not book.title:
        book.title = posixpath.splitext(posixpath.basename(path))[0]
    return book


def _is_front(piece: "_Piece", doc: str, doc_title: str, front: set[str],
              types: set[str], nav: str | None) -> bool:
    """Is this piece cover/title/copyright/contents rather than the book?

    Names, types and guide entries only raise suspicion; the text decides. A
    page of real prose stays whatever it is called, and a page the table of
    contents gives a real title is never judged by its file name.
    """
    if doc == nav and not piece.from_toc:
        return True
    label = piece.title or piece.heading or doc_title or ""
    explicit = bool(piece.title) and not _FRONT_TITLE.match(piece.title)
    stem = posixpath.splitext(posixpath.basename(doc))[0].lower()
    suspect = (doc in front or bool(types & _FRONT_TYPES)
               or bool(_FRONT_TITLE.match(label))
               or (not explicit and not piece.frag
                   and bool(_FRONT_STEM.match(stem))))
    if not suspect:
        return False
    if piece.link_fraction >= 0.5 and piece.words:
        return True                    # a contents list, however long
    return piece.words < FRONT_MAX_WORDS


@dataclass
class _Entry:
    href: str          # zip path of the document
    frag: str          # anchor within it, "" for the whole file
    title: str
    depth: int = 0


@dataclass
class _Piece:
    """A chapter-sized slice of one document."""
    body: object
    title: str = ""            # from the table of contents; "" if none
    frag: str = ""
    from_toc: bool = False
    heading: str = ""
    words: int = 0
    link_fraction: float = 0.0


def _target(base: str, ref: str) -> tuple[str | None, str]:
    path, _, frag = (ref or "").partition("#")
    return _resolve(base, path), unquote(frag)


def _read_nav(zf, nav_path: str) -> tuple[list[_Entry], set[str]]:
    soup = htmlutil.parse(_decode(_read(zf, nav_path, MAX_TEXT_BYTES)))
    if _depth(soup) > MAX_NEST:
        raise EpubError("Refusing this file: its navigation is nested "
                        "implausibly deep")
    toc: list[_Entry] = []
    front: set[str] = set()
    navs = soup.find_all("nav")
    toc_nav = next((n for n in navs
                    if "toc" in (n.get("epub:type") or "").split()), None)
    if toc_nav is None:
        # An untyped <nav> is the contents; a page-list or landmarks is not.
        toc_nav = next((n for n in navs if not n.get("epub:type")), None)
    if toc_nav is not None:
        for a in toc_nav.find_all("a"):
            href, frag = _target(nav_path, a.get("href") or "")
            text = " ".join(a.get_text(" ", strip=True).split())
            if href and text:
                depth = sum(1 for p in a.parents
                            if p is not toc_nav and p.name in ("ol", "ul"))
                toc.append(_Entry(href, frag, text, max(0, depth - 1)))
    for n in navs:
        if "landmarks" in (n.get("epub:type") or ""):
            for a in n.find_all("a"):
                if set((a.get("epub:type") or "").split()) & _FRONT_TYPES:
                    href, _frag = _target(nav_path, a.get("href") or "")
                    if href:
                        front.add(href)
    return toc, front


def _read_ncx(zf, ncx_path: str) -> list[_Entry]:
    root = _xml(_read(zf, ncx_path, MAX_TEXT_BYTES))
    out: list[_Entry] = []
    # Document order with depth, without recursion.
    stack = [(root, -1)]
    while stack:
        el, depth = stack.pop()
        is_point = _local(el) == "navPoint"
        if is_point:
            label = next((c for c in el if _local(c) == "navLabel"), None)
            content = next((c for c in el if _local(c) == "content"), None)
            text = " ".join(("".join(label.itertext()) if label is not None
                             else "").split())
            href, frag = (_target(ncx_path, content.get("src") or "")
                          if content is not None else (None, ""))
            if href and text:
                out.append(_Entry(href, frag, text, depth))
        for child in reversed(list(el)):
            if isinstance(child.tag, str):
                stack.append((child, depth + 1 if is_point else depth))
    return out


# -- chapter HTML ---------------------------------------------------------

def _decode(raw: bytes) -> str:
    return UnicodeDammit(raw, is_html=True).unicode_markup or ""


def _depth(root) -> int:
    """Deepest element nesting under `root`, without recursion."""
    deepest, stack = 0, [(root, 0)]
    while stack:
        node, d = stack.pop()
        deepest = max(deepest, d)
        for child in getattr(node, "children", ()):
            if isinstance(child, Tag):
                stack.append((child, d + 1))
    return deepest


def _text_len(node) -> int:
    return len(node.get_text(" ", strip=True))


def _covering_types(body, soup) -> set[str]:
    """epub:type values that describe (nearly) the whole file.

    A `titlepage` section at the top of a one-file book, or a small `toc` div
    opening a chapter, labels a part of the file, not the file.
    """
    types = set((body.get("epub:type") or "").split()) if body is not soup else set()
    total = max(1, _text_len(body))
    for sec in body.find_all(["section", "div"], recursive=False):
        mine = set((sec.get("epub:type") or "").split())
        if mine and _text_len(sec) >= 0.8 * total:
            types |= mine
    return types


def _prepare(raw: bytes, doc: str, names: set[str], entries=()):
    """Parse one document, cut it into chapter pieces, neutralise each.

    `entries` are the table-of-contents entries to cut at (those pointing
    into this file). Returns (pieces, <title>, covering epub:type values).
    """
    soup = htmlutil.parse(_decode(raw))
    body = soup.body or soup
    if _depth(body) > MAX_NEST:
        raise EpubError("Refusing this file: a chapter is nested "
                        "implausibly deep")
    types = _covering_types(body, soup)
    doc_title = ""
    if soup.title:
        doc_title = " ".join(soup.title.get_text(" ", strip=True).split())
    for junk in body.find_all(string=lambda s: isinstance(
            s, (Comment, Declaration, Doctype, ProcessingInstruction))):
        junk.extract()

    # Cut before anything strips the ids the table of contents points at.
    start_title, cuts = None, []              # cuts: (position, tag, entry)
    if entries:
        anchors = {}
        for tag in body.find_all(True):
            for key in (tag.get("id"), tag.get("name") if tag.name == "a" else None):
                if key:
                    anchors.setdefault(key, tag)
        order = {id(t): i for i, t in enumerate(body.find_all(True))}
        seen = set()
        for e in entries:
            tag = anchors.get(e.frag) if e.frag else None
            if tag is None or tag is body:
                if start_title is None:
                    start_title = e.title
            elif id(tag) not in seen:
                seen.add(id(tag))
                cuts.append((order[id(tag)], tag, e))
        cuts.sort(key=lambda c: c[0])
    if cuts:
        roots = _split(soup, body, [c[1] for c in cuts])
    else:
        roots = [body]
    pieces = []
    for i, root in enumerate(roots):
        if i == 0:
            piece = _Piece(root, title=start_title or "",
                           from_toc=start_title is not None)
        else:
            e = cuts[i - 1][2]
            piece = _Piece(root, title=e.title, frag=e.frag, from_toc=True)
        h = root.find(["h1", "h2", "h3"])
        piece.heading = " ".join(h.get_text(" ", strip=True).split()) if h else ""
        total = _text_len(root)
        linked = sum(_text_len(a) for a in root.find_all("a")
                     if (a.get("href") or "").strip())
        piece.words = len(root.get_text(" ", strip=True).split())
        piece.link_fraction = min(1.0, linked / total) if total else 0.0
        if i == 0 and not (piece.words or root.find("img")) and cuts:
            continue                          # nothing before the first cut
        _neutralise(root, doc, names)
        pieces.append(piece)
    return pieces, doc_title, types


def _split(soup, body, cut_tags) -> list:
    """Cut `body` into len(cut_tags)+1 containers at the given elements.

    Each container is a fresh <div> holding clones of the ancestors it needs
    around the content that falls in its range, so wrapping markup (a
    `<div class=chapter>` around a heading) survives in every piece. One pass
    over the tree; ancestors are cloned lazily, so cost is linear in size.
    """
    cutset = {id(t) for t in cut_tags}
    roots = [soup.new_tag("div")]
    maps = [{id(body): roots[0]}]

    def clone_in(i, node):
        got = maps[i].get(id(node))
        if got is None:
            parent = clone_in(i, node.parent)
            got = soup.new_tag(node.name, attrs=dict(node.attrs))
            parent.append(got)
            maps[i][id(node)] = got
        return got

    cur = 0
    stack = list(reversed(body.contents))
    while stack:
        node = stack.pop()
        if id(node) in cutset:
            cur += 1
            roots.append(soup.new_tag("div"))
            maps.append({id(body): roots[cur]})
        if isinstance(node, Tag) and node.contents:
            stack.extend(reversed(node.contents))
            continue
        leaf = (soup.new_tag(node.name, attrs=dict(node.attrs))
                if isinstance(node, Tag) else NavigableString(str(node)))
        clone_in(cur, node.parent).append(leaf)
    return roots


def _neutralise(body, doc: str, names: set[str]) -> None:
    """Make one piece safe and offline for the sanitiser."""
    # Math first, while the MathML elements are still intact.
    for m in body.find_all(lambda t: (t.name or "").split(":")[-1] == "math"):
        tag = "div" if m.get("display") == "block" else "span"
        frag = htmlutil.parse(f"<{tag}>{mathml.to_html(m)}</{tag}>")
        m.replace_with(frag.find(tag))

    for img in list(body.find_all("img")):
        target = _resolve(doc, img.get("src") or "")
        if target is None or target not in names:
            img.decompose()           # remote, data: or escaping: not offline
            continue
        for attr in list(img.attrs):
            if attr not in ("alt", "title", "width", "height"):
                del img[attr]
        img["src"] = ASSET_PREFIX + target
    # Nothing may make the reader fetch from the network: responsive image
    # candidates (<picture>/<source>/srcset) are not followed, only the one
    # local <img> stays.
    for src in body.find_all("source"):
        src.decompose()
    for pic in body.find_all("picture"):
        pic.unwrap()
    for tag in body.find_all(True):
        for attr in ("srcset", "sizes", "poster", "background", "data",
                     "ping", "formaction"):
            if attr in tag.attrs:
                del tag[attr]
    for a in list(body.find_all("a")):
        href = (a.get("href") or "").strip()
        if href.lower().startswith(("http://", "https://", "mailto:")):
            continue
        # Cross-references (and in-page anchors, which the reader does not
        # navigate) degrade to plain text.
        a.unwrap()
    for tag in body.find_all(["aside", "header", "footer"]):
        tag.name = "div"
    for tag in body.find_all(True):
        for attr in ("class", "id", "style"):
            if attr in tag.attrs:
                del tag[attr]


def _render(body, title: str) -> str:
    wanted = re.sub(r"\W+", " ", title or "").strip().lower()
    h = body.find(["h1", "h2", "h3"])
    if h is not None and wanted and re.sub(
            r"\W+", " ", h.get_text(" ", strip=True)).strip().lower() == wanted:
        h.decompose()                  # the reader shows the title itself
    html = htmlutil.sanitise(body, ASSET_PREFIX)
    if not htmlutil.word_count(html) and not htmlutil.count_images(html):
        return ""
    return html
