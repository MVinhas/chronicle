"""Reading a book: EPUB import, safety limits, math, ordering.

EPUBs are generated here with zipfile; nothing touches the network.
Run: tools/run-tests.sh
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from chronicle import db, epubfile, htmlutil, images, mathml, paths, sync

_REAL_CONNECT = db.connect
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4      # over 900 bytes
TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40           # below MIN_BYTES

CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>')


def xhtml(title: str, body: str, attrs: str = "") -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><head>'
            f'<title>{title}</title></head><body {attrs}>{body}</body></html>')


def chapter_body(title: str, words: int = 60) -> str:
    return f"<h1>{title}</h1><p>{' '.join(['word'] * words)}</p>"


def make_epub(path, chapters, toc="nav", images_=None, extra=None,
              guide=None, opf_extra="", title="The Test Book", repeat=None,
              toc_entries=None, nav_extra="", spine_toc="ncx"):
    """chapters: list of (id, filename, toc_title|None, body, body_attrs).

    repeat: {idref: n} lists that itemref n times in the spine.
    toc_entries: [(href[#frag], title, depth)] replacing the entries derived
    from the chapters' titles. nav_extra: more <nav> elements in the nav file.
    """
    items, spine = [], []
    for cid, fn, _t, _b, *_ in chapters:
        items.append(f'<item id="{cid}" href="{fn}" '
                     'media-type="application/xhtml+xml"/>')
        spine.extend([f'<itemref idref="{cid}"/>'] * (repeat or {}).get(cid, 1))
    for name in (images_ or {}):
        items.append(f'<item id="img-{name}" href="{name}" media-type="image/png"/>')
    if toc == "nav":
        items.append('<item id="nav" href="nav.xhtml" properties="nav" '
                     'media-type="application/xhtml+xml"/>')
    elif toc == "ncx":
        items.append('<item id="ncx" href="toc.ncx" '
                     'media-type="application/x-dtbncx+xml"/>')
    spine_attr = f' toc="{spine_toc}"' if toc == "ncx" else ""
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
           'version="3.0" unique-identifier="id"><metadata '
           'xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>{title}</dc:title><dc:creator>A. Author</dc:creator>'
           f'</metadata><manifest>{"".join(items)}</manifest>'
           f'<spine{spine_attr}>{"".join(spine)}</spine>{guide or ""}'
           f'{opf_extra}</package>')
    titled = (toc_entries if toc_entries is not None
              else [(fn, t, 0) for _i, fn, t, *_ in chapters if t])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        for cid, fn, _t, body, *rest in chapters:
            z.writestr(f"OEBPS/{fn}", xhtml(_t or cid, body, *(rest or [""])))
        if toc == "nav":
            z.writestr("OEBPS/nav.xhtml", xhtml(
                "Nav", f'<nav epub:type="toc">{_nav_list(titled)}</nav>'
                       + nav_extra))
        elif toc == "ncx":
            pts = _ncx_points(titled)
            z.writestr("OEBPS/toc.ncx",
                       '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/'
                       f'z3986/2005/ncx/" version="2005-1"><navMap>{pts}</navMap></ncx>')
        for name, data in (images_ or {}).items():
            z.writestr(f"OEBPS/{name}", data)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def _nav_list(entries):
    out, depth = "<ol>", 0
    for i, (href, t, d) in enumerate(entries):
        while depth < d:
            out += "<ol>"
            depth += 1
        while depth > d:
            out += "</ol></li>"
            depth -= 1
        out += f'<li><a href="{href}">{t}</a>'
        nxt = entries[i + 1][2] if i + 1 < len(entries) else 0
        if nxt <= d:
            out += "</li>"
    while depth > 0:
        out += "</ol></li>"
        depth -= 1
    return out + "</ol>"


def _ncx_points(entries):
    out, stack = "", []
    for i, (href, t, d) in enumerate(entries, 1):
        while len(stack) > d:
            stack.pop()
            out += "</navPoint>"
        out += (f'<navPoint id="p{i}" playOrder="{i}"><navLabel><text>{t}'
                f'</text></navLabel><content src="{href}"/>')
        stack.append(i)
    return out + "</navPoint>" * len(stack)


def standard_chapters():
    return [
        ("cover", "cover.xhtml", "Cover", '<img src="cover.png" alt="c"/>'),
        ("title", "titlepage.xhtml", "Title Page", "<h1>The Test Book</h1>"),
        ("copy", "copyright.xhtml", "Copyright", "<p>All rights reserved.</p>"),
        ("toc", "contents.xhtml", "Contents", "<p>1. One 2. Two</p>"),
        ("c1", "ch1.xhtml", "Chapter One", chapter_body("Chapter One")),
        ("c2", "ch2.xhtml", "Chapter Two", chapter_body("Chapter Two")),
        ("c3", "ch3.xhtml", "Chapter Three", chapter_body("Chapter Three")),
    ]


class TmpMixin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.epub = os.path.join(self.dir.name, "book.epub")
        self.dbfile = os.path.join(self.dir.name, "lib.db")
        patches = [
            mock.patch.object(paths, "IMAGE_DIR", Path(self.dir.name) / "img"),
            mock.patch.object(paths, "BOOK_DIR", Path(self.dir.name) / "books"),
            mock.patch.object(db, "connect",
                              lambda path=None: _REAL_CONNECT(self.dbfile)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        (Path(self.dir.name) / "img").mkdir()
        self.conn = _REAL_CONNECT(self.dbfile)
        db.init(self.conn)
        self.addCleanup(self.conn.close)

    def add_book(self, name=None):
        from chronicle.sources import epub as epub_source
        return epub_source.add_book_source(self.conn, self.epub, name)

    def sync(self, newest_only=False):
        return sync.Syncer().sync_all(newest_only=newest_only)

    def titles(self, sid=None):
        q = "SELECT title FROM articles"
        args = ()
        if sid:
            q += " WHERE source_id=?"
            args = (sid,)
        return [r["title"] for r in self.conn.execute(
            q + " ORDER BY source_order", args)]


class TestChapters(TmpMixin):
    def test_epub3_nav_titles_order_and_front_matter(self):
        make_epub(self.epub, standard_chapters(), images_={"cover.png": PNG})
        sid = self.add_book()["id"]
        self.sync()
        self.assertEqual(self.titles(sid),
                         ["Chapter One", "Chapter Two", "Chapter Three"])
        orders = [r["source_order"] for r in self.conn.execute(
            "SELECT source_order FROM articles ORDER BY source_order")]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(set(orders)), 3)
        row = self.conn.execute("SELECT * FROM articles LIMIT 1").fetchone()
        self.assertIsNone(row["published_at"])
        self.assertEqual(row["content_status"], "ok")

    def test_epub2_ncx(self):
        make_epub(self.epub, standard_chapters(), toc="ncx",
                  images_={"cover.png": PNG})
        sid = self.add_book()["id"]
        self.sync()
        self.assertEqual(self.titles(sid),
                         ["Chapter One", "Chapter Two", "Chapter Three"])

    def test_spine_only_uses_headings(self):
        chs = [(c, f, None, b) for c, f, _t, b in standard_chapters()[4:]]
        make_epub(self.epub, chs, toc=None)
        sid = self.add_book()["id"]
        self.sync()
        self.assertEqual(self.titles(sid),
                         ["Chapter One", "Chapter Two", "Chapter Three"])

    def test_epub_type_guide_and_landmarks_mark_front_matter(self):
        tiny = "<p>" + "w " * 20 + "</p>"
        chs = [("a", "a.xhtml", "Preamble", tiny, 'epub:type="frontmatter copyright-page"'),
               ("g", "g.xhtml", "Intro", tiny),
               ("l", "l.xhtml", "Welcome", tiny),
               ("b", "b.xhtml", "Real", chapter_body("Real"))]
        guide = ('<guide><reference type="title-page" title="T" href="g.xhtml"/>'
                 '</guide>')
        landmarks = ('<nav epub:type="landmarks"><ol><li><a epub:type="cover" '
                     'href="l.xhtml">Cover</a></li></ol></nav>')
        make_epub(self.epub, chs, guide=guide, nav_extra=landmarks)
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters], ["Real"])

    def test_epub_type_on_a_small_part_of_a_file_does_not_drop_it(self):
        long_ = "<p>" + "w " * 400 + "</p>"
        body = ('<section epub:type="titlepage"><h1>My Book</h1></section>'
                '<h2>Chapter One</h2>' + long_)
        mini_toc = ('<div epub:type="toc"><p>In this chapter: a, b</p></div>'
                    '<h2>Two</h2>' + long_)
        chs = [("a", "a.xhtml", "Chapter One", body),
               ("b", "b.xhtml", "Two", mini_toc)]
        make_epub(self.epub, chs)
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters], ["Chapter One", "Two"])

    def test_explicit_toc_title_beats_front_matter_file_names(self):
        chs = [("a", "contents2.xhtml", "Chapter Four", chapter_body("Chapter Four")),
               ("b", "nav1.xhtml", "Navigation at sea", chapter_body("Navigation")),
               ("c", "toc-1.xhtml", "The Toll", chapter_body("The Toll"))]
        make_epub(self.epub, chs)
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters],
                         ["Chapter Four", "Navigation at sea", "The Toll"])

    def test_substantial_pages_titled_like_front_matter_are_kept(self):
        long_ = "<p>" + "real prose here " * 100 + "</p>"
        chs = [("a", "a.xhtml", "Contents", long_),
               ("b", "b.xhtml", "Preface", chapter_body("Preface")),
               ("c", "c.xhtml", "Contents of Volume One", chapter_body("V1")),
               ("d", "d.xhtml", "Copyright", "<p>All rights reserved.</p>")]
        make_epub(self.epub, chs)
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters],
                         ["Contents", "Preface", "Contents of Volume One"])

    def test_guide_title_page_with_substantial_text_is_kept(self):
        chs = [("t", "t.xhtml", "Opening", "<p>" + "story " * 400 + "</p>"),
               ("b", "b.xhtml", "Next", chapter_body("Next"))]
        guide = '<guide><reference type="title-page" href="t.xhtml"/></guide>'
        make_epub(self.epub, chs, guide=guide)
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters], ["Opening", "Next"])

    def test_a_contents_page_made_of_links_is_front_matter_at_any_length(self):
        links = "".join(f'<p><a href="c{i}.xhtml">Chapter number {i}</a></p>'
                        for i in range(80))
        chs = [("t", "toc.xhtml", None, links),
               ("a", "c1.xhtml", "One", chapter_body("One"))]
        make_epub(self.epub, chs)
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters], ["One"])

    def test_unlisted_spine_file_continues_previous_chapter(self):
        chs = [("c1", "ch1.xhtml", "One", chapter_body("One")),
               ("c1b", "ch1b.xhtml", None, "<p>second half marker</p>"),
               ("c2", "ch2.xhtml", "Two", chapter_body("Two"))]
        make_epub(self.epub, chs)
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters], ["One", "Two"])
        self.assertIn("second half marker", book.chapters[0].html)

    def test_continuation_document_keeps_its_own_heading(self):
        chs = [("c1", "ch1.xhtml", "One", chapter_body("One")),
               ("c1b", "ch1b.xhtml", None,
                "<h2>Second Movement</h2><p>second half marker</p>")]
        make_epub(self.epub, chs)
        html = epubfile.open_book(self.epub).chapters[0].html
        self.assertIn("Second Movement", html)

    def test_scripts_and_handlers_never_survive(self):
        body = ('<h1>T</h1><p onclick="x()">hello ' + "w " * 30 +
                '</p><script>alert(1)</script><iframe src="http://e"></iframe>')
        make_epub(self.epub, [("c", "c.xhtml", "T", body)])
        self.add_book()
        self.sync()
        html = self.conn.execute("SELECT content_html h FROM articles").fetchone()["h"]
        self.assertNotIn("script", html.lower())
        self.assertNotIn("onclick", html)
        self.assertNotIn("iframe", html)

    def test_internal_links_degrade_to_text_external_stay(self):
        body = ('<h1>T</h1><p>See <a href="ch2.xhtml#x">chapter two</a> and '
                '<a href="https://example.org/">site</a> ' + "w " * 30 + "</p>")
        make_epub(self.epub, [("c", "c.xhtml", "T", body),
                              ("d", "ch2.xhtml", "D", chapter_body("D"))])
        book = epubfile.open_book(self.epub)
        html = book.chapters[0].html
        self.assertIn("chapter two", html)
        self.assertNotIn("ch2.xhtml", html)
        self.assertIn("https://example.org/", html)


class TestSafety(TmpMixin):
    def test_zip_slip_member_rejected(self):
        make_epub(self.epub, standard_chapters()[4:],
                  extra={"../../evil.txt": "x"})
        with self.assertRaises(epubfile.EpubError):
            epubfile.open_book(self.epub)

    def test_absolute_member_rejected(self):
        make_epub(self.epub, standard_chapters()[4:], extra={"/etc/x": "x"})
        with self.assertRaises(epubfile.EpubError):
            epubfile.open_book(self.epub)

    def test_oversized_entry_rejected(self):
        make_epub(self.epub, standard_chapters()[4:],
                  extra={"OEBPS/big.bin": b"\x00" * 2_000_000})
        with mock.patch.object(epubfile, "MAX_ENTRY_BYTES", 100_000):
            with self.assertRaises(epubfile.EpubError):
                epubfile.open_book(self.epub)

    def test_too_many_entries_rejected(self):
        make_epub(self.epub, standard_chapters()[4:],
                  extra={f"OEBPS/f{i}.txt": "x" for i in range(50)})
        with mock.patch.object(epubfile, "MAX_ENTRIES", 20):
            with self.assertRaises(epubfile.EpubError):
                epubfile.open_book(self.epub)

    def test_compression_bomb_ratio_rejected(self):
        make_epub(self.epub, standard_chapters()[4:],
                  extra={"OEBPS/bomb.txt": b"\x00" * 5_000_000})
        with mock.patch.object(epubfile, "MAX_RATIO", 50), \
                mock.patch.object(epubfile, "RATIO_FLOOR", 100_000):
            with self.assertRaises(epubfile.EpubError):
                epubfile.open_book(self.epub)

    def test_total_size_cap(self):
        make_epub(self.epub, standard_chapters()[4:],
                  extra={"OEBPS/a.bin": b"a" * 60_000, "OEBPS/b.bin": b"b" * 60_000})
        with mock.patch.object(epubfile, "MAX_TOTAL_BYTES", 100_000):
            with self.assertRaises(epubfile.EpubError):
                epubfile.open_book(self.epub)

    def test_entity_declaration_in_package_rejected(self):
        evil = ('<?xml version="1.0"?><!DOCTYPE p [<!ENTITY xxe SYSTEM '
                '"file:///etc/passwd">]><package xmlns="http://www.idpf.org/2007/opf">'
                '<metadata/><manifest/><spine/></package>')
        make_epub(self.epub, standard_chapters()[4:],
                  extra={"OEBPS/content.opf": evil})
        # writestr of a duplicate name keeps the later one first in the index;
        # build the archive directly instead for a deterministic override.
        with zipfile.ZipFile(self.epub, "w") as z:
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr("OEBPS/content.opf", evil)
        with self.assertRaises(epubfile.EpubError):
            epubfile.open_book(self.epub)

    def test_external_entity_in_chapter_is_not_resolved(self):
        secret = Path(self.dir.name) / "secret.txt"
        secret.write_text("TOP-SECRET-MARKER")
        body = (f'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file://{secret}">]>'
                '<h1>T</h1><p>before &xxe; after ' + "w " * 30 + '</p>')
        make_epub(self.epub, [("c", "c.xhtml", "T", body)])
        book = epubfile.open_book(self.epub)
        self.assertNotIn("TOP-SECRET-MARKER", book.chapters[0].html)
        self.assertIn("before", book.chapters[0].html)

    def test_drm_encrypted_book_rejected(self):
        enc = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
               '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
               '<EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
               '<CipherData><CipherReference URI="OEBPS/c.xhtml"/></CipherData>'
               '</EncryptedData></encryption>')
        make_epub(self.epub, standard_chapters()[4:],
                  extra={"META-INF/encryption.xml": enc})
        with self.assertRaisesRegex(epubfile.EpubError, "DRM"):
            epubfile.open_book(self.epub)

    def test_font_obfuscation_is_not_drm(self):
        enc = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
               '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
               '<EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
               '<CipherData><CipherReference URI="OEBPS/f.otf"/></CipherData>'
               '</EncryptedData></encryption>')
        make_epub(self.epub, standard_chapters()[4:],
                  extra={"META-INF/encryption.xml": enc})
        self.assertEqual(len(epubfile.open_book(self.epub).chapters), 3)

    def test_not_a_zip(self):
        Path(self.epub).write_bytes(b"not a zip at all")
        with self.assertRaises(epubfile.EpubError):
            epubfile.open_book(self.epub)


class TestResourceLimits(TmpMixin):
    def test_duplicate_spine_itemrefs_are_read_once(self):
        make_epub(self.epub, [("c", "c.xhtml", "One", chapter_body("One"))],
                  repeat={"c": 300})
        reads = []
        real = epubfile._read

        def counting(zf, name, limit):
            reads.append(name)
            return real(zf, name, limit)
        with mock.patch.object(epubfile, "_read", counting):
            book = epubfile.open_book(self.epub)
        self.assertEqual(len(book.chapters), 1)
        self.assertEqual(reads.count("OEBPS/c.xhtml"), 1)

    def test_read_budget_bounds_total_decompressed_bytes(self):
        chs = [(f"c{i}", f"c{i}.xhtml", f"T{i}", chapter_body(f"T{i}", 300))
               for i in range(10)]
        make_epub(self.epub, chs)
        with mock.patch.object(epubfile, "MAX_READ_BYTES", 5_000):
            with self.assertRaises(epubfile.EpubError):
                epubfile.open_book(self.epub)

    def test_many_small_members_cannot_declare_a_bomb(self):
        extra = {f"OEBPS/z{i}.bin": b"\x00" * 100_000 for i in range(40)}
        make_epub(self.epub, standard_chapters()[4:], extra=extra)
        with self.assertRaisesRegex(epubfile.EpubError, "bomb|large"):
            epubfile.open_book(self.epub)

    def test_utf16_entity_declaration_is_refused(self):
        evil = ('<?xml version="1.0" encoding="utf-16"?><!DOCTYPE p [<!ENTITY a '
                '"aaaa">]><package xmlns="http://www.idpf.org/2007/opf">'
                '<metadata/><manifest/><spine/></package>').encode("utf-16")
        with zipfile.ZipFile(self.epub, "w") as z:
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr("OEBPS/content.opf", evil)
        with self.assertRaisesRegex(epubfile.EpubError, "entit"):
            epubfile.open_book(self.epub)

    def test_plain_doctype_without_entities_is_accepted(self):
        make_epub(self.epub, standard_chapters()[4:], toc="ncx")
        with zipfile.ZipFile(self.epub) as z:
            members = {n: z.read(n) for n in z.namelist()}
        members["OEBPS/toc.ncx"] = (
            b'<?xml version="1.0"?><!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" '
            b'"http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">'
            + members["OEBPS/toc.ncx"].split(b"?>", 1)[1])
        with zipfile.ZipFile(self.epub, "w") as z:
            for n, d in members.items():
                z.writestr(n, d)
        self.assertEqual(len(epubfile.open_book(self.epub).chapters), 3)

    def test_deeply_nested_math_degrades_to_text(self):
        depth = 5000
        src = ("<math>" + "<mrow>" * depth + "<mi>x</mi>" + "</mrow>" * depth
               + "</math>")
        out = mathml.to_html(htmlutil.parse(src).find("math"))
        self.assertIn("x", out)

    def test_deeply_nested_markup_is_refused(self):
        body = "<div>" * 3000 + "<p>deep</p>" + "</div>" * 3000
        make_epub(self.epub, [("c", "c.xhtml", "T", body)])
        with self.assertRaisesRegex(epubfile.EpubError, "nest"):
            epubfile.open_book(self.epub)

    def test_image_bytes_are_loaded_once_per_reference(self):
        calls = []

        def load(ref):
            calls.append(ref)
            return PNG
        html = '<p>' + '<img src="epub-asset:/a.png"/>' * 5 + '</p>'
        out = images.store_assets(html, load, self.conn, "epub-asset:/", "k/")
        self.assertEqual(calls, ["a.png"])
        self.assertEqual(out.count(images.SCHEME + "://"), 5)

    def test_asset_read_rechecks_the_archive(self):
        make_epub(self.epub, [("c", "c.xhtml", "T", chapter_body("T"))],
                  images_={"a.png": PNG})
        book = epubfile.open_book(self.epub)
        self.assertEqual(book.read_asset("OEBPS/a.png"), PNG)
        with zipfile.ZipFile(self.epub, "a") as z:
            z.writestr("../evil", "x")
        self.assertIsNone(book.read_asset("OEBPS/a.png"))


class TestRemoteReferences(TmpMixin):
    def test_picture_sources_and_srcset_are_dropped(self):
        body = (chapter_body("T") + '<picture><source srcset="http://evil/x.png">'
                '<img src="a.png" srcset="http://evil/y.png 2x" alt=""/></picture>'
                '<video poster="http://evil/p.png" src="http://evil/v.mp4"></video>'
                '<p style="background:url(http://evil/b.png)">x</p>'
                '<svg><use href="http://evil/s.svg#a"/></svg>'
                '<link rel="stylesheet" href="http://evil/c.css"/>')
        make_epub(self.epub, [("c", "c.xhtml", "T", body)],
                  images_={"a.png": PNG})
        html = epubfile.open_book(self.epub).chapters[0].html
        self.assertNotIn("evil", html)
        self.assertNotIn("srcset", html)
        self.assertNotIn("<source", html)
        self.assertIn("<img", html)


class TestFragmentChapters(TmpMixin):
    def gutenberg(self, entries, extra_before=""):
        parts = [extra_before]
        for i, name in enumerate(("One", "Two", "Three"), 1):
            parts.append(f'<div class="chapter"><h2><a id="c{i}"></a>Chapter {name}'
                         f'</h2><p>marker{i} ' + "word " * 40 + "</p></div>")
        make_epub(self.epub, [("big", "big.xhtml", None, "".join(parts)),
                              ("end", "end.xhtml", "Epilogue", chapter_body("Epilogue"))],
                  toc_entries=entries)
        return epubfile.open_book(self.epub)

    entries = [("big.xhtml#c1", "I. First", 0), ("big.xhtml#c2", "II. Second", 0),
               ("big.xhtml#c3", "III. Third", 0), ("end.xhtml", "Epilogue", 0)]

    def test_one_file_with_many_toc_anchors_becomes_many_chapters(self):
        book = self.gutenberg(self.entries)
        self.assertEqual([c.title for c in book.chapters],
                         ["I. First", "II. Second", "III. Third", "Epilogue"])
        for i, ch in enumerate(book.chapters[:3], 1):
            self.assertIn(f"marker{i}", ch.html)
            for j in (1, 2, 3):
                if j != i:
                    self.assertNotIn(f"marker{j}", ch.html)
        self.assertEqual([c.order for c in book.chapters], [0, 1, 2, 3])
        self.assertEqual(len({c.key for c in book.chapters}), 4)

    def test_leading_material_before_the_first_anchor_is_kept(self):
        book = self.gutenberg(self.entries,
                              extra_before="<p>lead-in " + "text " * 40 + "</p>")
        self.assertIn("lead-in", book.chapters[0].html)
        self.assertEqual(len(book.chapters), 5)
        self.assertNotIn("lead-in", book.chapters[1].html)

    def test_entry_at_the_start_of_a_file_needs_no_split(self):
        make_epub(self.epub, [("a", "a.xhtml", None,
                               chapter_body("A") + '<h2 id="x">Sub</h2><p>more</p>')],
                  toc_entries=[("a.xhtml", "Alpha", 0)])
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters], ["Alpha"])
        self.assertIn("more", book.chapters[0].html)

    def test_nested_entries_split_only_at_top_level_anchors(self):
        body = ('<h2 id="p1">Part One</h2><p>' + "w " * 30 + 'alpha</p>'
                '<h3 id="s1">Section</h3><p>' + "w " * 30 + 'beta</p>'
                '<h2 id="p2">Part Two</h2><p>' + "w " * 30 + 'gamma</p>')
        make_epub(self.epub, [("a", "a.xhtml", None, body),
                              ("b", "b.xhtml", "Own file child", chapter_body("B"))],
                  toc_entries=[("a.xhtml#p1", "Part One", 0), ("a.xhtml#s1", "Section", 1),
                               ("a.xhtml#p2", "Part Two", 0),
                               ("b.xhtml", "Own file child", 1)])
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters],
                         ["Part One", "Part Two", "Own file child"])
        self.assertIn("beta", book.chapters[0].html)

    def test_split_chapters_sync_with_distinct_guids_and_no_ids(self):
        self.gutenberg(self.entries)
        sid = self.add_book()["id"]
        self.sync()
        guids = [r["guid"] for r in self.conn.execute(
            "SELECT guid FROM articles WHERE source_id=? ORDER BY source_order", (sid,))]
        self.assertEqual(len(set(guids)), 4)
        html = self.conn.execute("SELECT content_html h FROM articles "
                                 "ORDER BY source_order").fetchone()["h"]
        self.assertNotIn(" id=", html)

    def test_many_anchors_in_a_big_file_are_fast(self):
        import time
        n = 400
        body = "".join(f'<h2 id="s{i}">Section {i}</h2><p>' + "word " * 200 + "</p>"
                       for i in range(n))
        make_epub(self.epub, [("a", "a.xhtml", None, body)],
                  toc_entries=[(f"a.xhtml#s{i}", f"Section {i}", 0) for i in range(n)])
        t = time.time()
        book = epubfile.open_book(self.epub)
        self.assertEqual(len(book.chapters), n)
        self.assertLess(time.time() - t, 10)


class TestStructureEdges(TmpMixin):
    def test_missing_spine_toc_attribute_target_falls_back_to_the_ncx(self):
        make_epub(self.epub, standard_chapters()[4:], toc="ncx",
                  spine_toc="nonexistent")
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters],
                         ["Chapter One", "Chapter Two", "Chapter Three"])

    def test_page_list_nav_is_not_taken_for_the_toc(self):
        make_epub(self.epub, standard_chapters()[4:], toc=None)
        with zipfile.ZipFile(self.epub, "a") as z:
            z.writestr("OEBPS/nav.xhtml", xhtml("N", (
                '<nav epub:type="page-list"><ol><li><a href="ch1.xhtml">99</a>'
                '</li></ol></nav>')))
        # Declare it as the nav document.
        with zipfile.ZipFile(self.epub) as z:
            members = {n: z.read(n) for n in z.namelist()}
        members["OEBPS/content.opf"] = members["OEBPS/content.opf"].replace(
            b"</manifest>", b'<item id="nav" href="nav.xhtml" properties="nav" '
            b'media-type="application/xhtml+xml"/></manifest>')
        with zipfile.ZipFile(self.epub, "w") as z:
            for n, d in members.items():
                z.writestr(n, d)
        book = epubfile.open_book(self.epub)
        self.assertEqual([c.title for c in book.chapters],
                         ["Chapter One", "Chapter Two", "Chapter Three"])

    def test_empty_spine_is_an_error(self):
        make_epub(self.epub, [])
        with self.assertRaises(epubfile.EpubError):
            epubfile.open_book(self.epub)

    def test_same_file_through_a_symlink_is_the_same_book(self):
        make_epub(self.epub, standard_chapters()[4:])
        link = os.path.join(self.dir.name, "link.epub")
        os.symlink(self.epub, link)
        from chronicle.sources import epub as epub_source
        a = epub_source.add_book_source(self.conn, self.epub)
        b = epub_source.add_book_source(self.conn, link)
        self.assertEqual(a["id"], b["id"])

    def test_concurrent_adds_make_one_source(self):
        import threading
        make_epub(self.epub, standard_chapters()[4:])
        from chronicle.sources import epub as epub_source
        ids, errors = [], []

        def add():
            conn = _REAL_CONNECT(self.dbfile)
            try:
                ids.append(epub_source.add_book_source(conn, self.epub)["id"])
            except Exception as exc:        # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()
        ts = [threading.Thread(target=add) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertFalse(errors, errors)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM sources").fetchone()["c"], 1)


class TestImages(TmpMixin):
    def test_images_are_extracted_and_cached_locally(self):
        body = (chapter_body("One") + '<p><img src="fig/a.png" alt="fig"/></p>'
                '<p><img src="fig/tiny.png" alt="eq"/></p>'
                '<p><img src="../../escape.png" alt="x"/></p>'
                '<p><img src="http://remote.example/x.png" alt="r"/></p>')
        make_epub(self.epub, [("c", "c.xhtml", "One", body)],
                  images_={"fig/a.png": PNG, "fig/tiny.png": TINY_PNG})
        self.add_book()
        self.sync()
        html = self.conn.execute("SELECT content_html h FROM articles").fetchone()["h"]
        srcs = htmlutil.image_urls(html)
        local = [s for s in srcs if s.startswith(images.SCHEME + "://")]
        self.assertEqual(len(local), 2, srcs)       # math-sized image kept
        for s in local:
            self.assertIsNotNone(images.resolve(s.split("://", 1)[1]))
        self.assertFalse([s for s in srcs if "escape" in s])

    def test_images_are_cached_even_when_remote_caching_is_off(self):
        body = chapter_body("One") + '<img src="a.png" alt=""/>'
        make_epub(self.epub, [("c", "c.xhtml", "One", body)],
                  images_={"a.png": PNG})
        self.add_book()
        sync.Syncer().sync_all(cache_images=False)
        html = self.conn.execute("SELECT content_html h FROM articles").fetchone()["h"]
        self.assertIn(images.SCHEME + "://", html)


class TestMath(unittest.TestCase):
    def conv(self, mathml_src: str) -> str:
        soup = htmlutil.parse(mathml_src)
        return mathml.to_html(soup.find("math"))

    def test_superscript_and_subscript(self):
        self.assertEqual(self.conv(
            "<math><msup><mi>x</mi><mn>2</mn></msup></math>"),
            "<i>x</i><sup>2</sup>")
        self.assertEqual(self.conv(
            "<math><msub><mi>a</mi><mn>0</mn></msub></math>"),
            "<i>a</i><sub>0</sub>")

    def test_fraction_sqrt_and_operators(self):
        out = self.conv("<math><mfrac><mn>1</mn><mn>2</mn></mfrac></math>")
        self.assertEqual(out, "1/2")
        out = self.conv("<math><msqrt><mi>x</mi></msqrt></math>")
        self.assertIn("√", out)
        out = self.conv("<math><mrow><mi>E</mi><mo>=</mo><mi>m</mi>"
                        "<msup><mi>c</mi><mn>2</mn></msup></mrow></math>")
        self.assertEqual(out, "<i>E</i> = <i>m</i><i>c</i><sup>2</sup>")

    def test_compound_fraction_is_parenthesised(self):
        out = self.conv("<math><mfrac><mrow><mi>a</mi><mo>+</mo><mi>b</mi></mrow>"
                        "<mn>2</mn></mfrac></math>")
        self.assertEqual(out, "(<i>a</i> + <i>b</i>)/2")

    def test_vector_accent_and_text_are_escaped(self):
        out = self.conv("<math><mover><mi>F</mi><mo>→</mo></mover></math>")
        self.assertTrue(out.endswith("\u20d7"), out)
        out = self.conv("<math><mtext>a &lt; b</mtext></math>")
        self.assertEqual(out, "a &lt; b")

    def test_math_inside_a_chapter_becomes_text(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "m.epub")
        body = ('<h1>M</h1><p>Energy ' + "w " * 30 + '<math xmlns="http://www.w3.org/'
                '1998/Math/MathML"><msup><mi>x</mi><mn>2</mn></msup></math> ok.</p>'
                '<p><img src="eq.png" alt="eq"/></p>')
        make_epub(path, [("c", "c.xhtml", "M", body)], images_={"eq.png": PNG})
        html = epubfile.open_book(path).chapters[0].html
        self.assertIn("<i>x</i><sup>2</sup>", html)
        self.assertNotIn("<math", html)
        self.assertIn("<img", html)


class TestResync(TmpMixin):
    def test_unchanged_file_is_not_reread(self):
        make_epub(self.epub, standard_chapters()[4:])
        self.add_book()
        self.sync()
        with mock.patch.object(epubfile, "open_book",
                               side_effect=AssertionError("re-read")):
            self.sync()
            self.sync(newest_only=True)
        msg = self.conn.execute("SELECT last_sync_message m FROM sources"
                                ).fetchone()["m"]
        self.assertIn("unchanged", msg)
        self.assertEqual(len(self.titles()), 3)

    def annotate(self, article_id):
        db.set_read(self.conn, article_id)
        db.toggle_favourite(self.conn, article_id)
        db.set_note(self.conn, article_id, "my note")
        db.add_highlight(self.conn, article_id, "word word", start_offset=3)

    def revised(self):
        chs = standard_chapters()[4:]
        chs[0] = ("c1", "ch1.xhtml", "Chapter One",
                  "<h1>Chapter One</h1><p>" + "revised " * 40 + "</p>")
        return chs

    def first_html(self):
        return self.conn.execute("SELECT content_html h FROM articles "
                                 "ORDER BY source_order").fetchone()["h"]

    def test_changed_file_is_reimported_in_place_keeping_reader_state(self):
        make_epub(self.epub, standard_chapters()[4:])
        self.add_book()
        self.sync()
        first = [r["id"] for r in self.conn.execute(
            "SELECT id FROM articles ORDER BY source_order")]
        self.annotate(first[0])
        make_epub(self.epub, self.revised())
        self.sync()
        rows = self.conn.execute(
            "SELECT id, content_html h FROM articles ORDER BY source_order").fetchall()
        self.assertEqual([r["id"] for r in rows], first)      # state preserved
        self.assertIn("revised", rows[0]["h"])
        st = self.conn.execute("SELECT * FROM reading_state WHERE article_id=?",
                               (first[0],)).fetchone()
        self.assertIsNotNone(st["read_at"])
        self.assertIsNotNone(st["favourite_at"])
        self.assertEqual(db.get_note(self.conn, first[0]), "my note")
        self.assertEqual(len(db.list_highlights(self.conn, first[0])), 1)

    def test_cancelled_refresh_is_picked_up_by_the_next_sync(self):
        make_epub(self.epub, standard_chapters()[4:])
        self.add_book()
        self.sync()
        make_epub(self.epub, self.revised())

        def cancel_instead(self_, *a, **kw):
            self_.cancel()
        with mock.patch.object(sync.Syncer, "_fetch_bodies", cancel_instead):
            self.sync()
        self.assertNotIn("revised", self.first_html())     # nothing stored yet
        self.sync()
        self.assertIn("revised", self.first_html())

    def test_removed_chapters_are_deleted_unless_annotated(self):
        make_epub(self.epub, standard_chapters()[4:])
        sid = self.add_book()["id"]
        self.sync()
        ids = {r["title"]: r["id"] for r in self.conn.execute(
            "SELECT id, title FROM articles")}
        db.set_note(self.conn, ids["Chapter Two"], "keep me")
        db.set_read(self.conn, ids["Chapter Three"])      # read alone: not kept
        make_epub(self.epub, standard_chapters()[4:5])    # only chapter one left
        self.sync()
        left = {r["title"] for r in self.conn.execute(
            "SELECT title FROM articles WHERE source_id=?", (sid,))}
        self.assertEqual(left, {"Chapter One", "Chapter Two"})
        order = [r["title"] for r in db.queue(self.conn)]
        self.assertEqual(order[0], "Chapter One")

    def test_book_is_copied_into_the_library(self):
        import json
        make_epub(self.epub, standard_chapters()[4:])
        row = self.add_book()
        cfg = json.loads(row["config"])
        self.assertNotEqual(cfg["path"], self.epub)
        self.assertTrue(cfg["path"].startswith(str(paths.BOOK_DIR)))
        self.assertTrue(os.path.exists(cfg["path"]))
        self.assertEqual(cfg["origin"], os.path.abspath(self.epub))
        self.assertEqual(row["homepage"], os.path.abspath(self.epub))

    def test_book_survives_its_original_being_moved_away(self):
        make_epub(self.epub, standard_chapters()[4:])
        self.add_book()
        os.unlink(self.epub)
        self.sync()
        src = self.conn.execute("SELECT * FROM sources").fetchone()
        self.assertEqual(src["last_sync_status"], "ok")
        self.assertIn("stored copy", src["last_sync_message"])
        self.assertEqual(len(self.titles()), 3)

    def test_missing_original_and_copy_reports_clearly_and_keeps_library(self):
        make_epub(self.epub, standard_chapters()[4:])
        self.add_book()
        self.sync()
        os.unlink(self.epub)
        for f in paths.BOOK_DIR.iterdir():
            f.unlink()
        self.sync()
        src = self.conn.execute("SELECT * FROM sources").fetchone()
        self.assertEqual(src["last_sync_status"], "error")
        self.assertIn("not found", src["last_sync_message"])
        self.assertEqual(len(self.titles()), 3)

    def test_adding_the_same_file_twice_returns_the_same_source(self):
        make_epub(self.epub, standard_chapters()[4:])
        a = self.add_book()
        b = self.add_book()
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["name"], "The Test Book")
        self.assertEqual(self.add_book("Other")["id"], a["id"])


class TestOrdering(TmpMixin):
    def test_book_is_contiguous_beside_blog_and_second_undated_source(self):
        make_epub(self.epub, standard_chapters()[4:], title="Book A")
        a = self.add_book()
        other = os.path.join(self.dir.name, "b.epub")
        make_epub(other, [("x1", "x1.xhtml", "Other 1", chapter_body("Other 1")),
                          ("x2", "x2.xhtml", "Other 2", chapter_body("Other 2"))],
                  title="Book B")
        from chronicle.sources import epub as epub_source
        b = epub_source.add_book_source(self.conn, other, None)
        blog = db.add_source(self.conn, "blog", "Blog", "generic", "https://b")
        aid, _ = db.upsert_article(
            self.conn, blog, "g", url="https://b/1", title="Dated",
            published_at="2020-01-01T00:00:00", date_precision="day",
            date_confidence="exact", date_source="t", source_order=0)
        self.conn.execute("UPDATE articles SET content_status='ok', "
                          "content_html='<p>x</p>' WHERE id=?", (aid,))
        sync.Syncer().sync_all(source_ids=[a["id"], b["id"]])   # never the blog
        want = ["Dated", "Chapter One", "Chapter Two", "Chapter Three",
                "Other 1", "Other 2"]
        for newest in (False, True):
            rows = db.queue(self.conn, newest_first=newest)
            self.assertEqual([r["title"] for r in rows], want, newest)
        self.assertLess(a["id"], b["id"])


class TestCli(TmpMixin):
    def test_add_book_command(self):
        from chronicle import cli
        make_epub(self.epub, standard_chapters()[4:])
        out = io.StringIO()
        with mock.patch.object(db, "get_conn", return_value=self.conn), \
                contextlib.redirect_stdout(out):
            rc = cli.main(["add-book", self.epub, "--name", "My Book"])
        self.assertEqual(rc or 0, 0)
        row = self.conn.execute("SELECT * FROM sources").fetchone()
        self.assertEqual(row["name"], "My Book")
        self.assertEqual(row["plugin"], "epub")
        self.assertEqual(row["homepage"], os.path.abspath(self.epub))

    def test_add_book_rejects_a_bad_file(self):
        from chronicle import cli
        Path(self.epub).write_bytes(b"junk")
        err = io.StringIO()
        with mock.patch.object(db, "get_conn", return_value=self.conn), \
                contextlib.redirect_stderr(err):
            rc = cli.main(["add-book", self.epub])
        self.assertEqual(rc, 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM sources").fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
