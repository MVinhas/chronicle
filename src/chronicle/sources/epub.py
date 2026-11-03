"""A book you own, read from a local .epub file.

Nothing is downloaded and no website is involved: the file is the source.
Its location (absolute path) and content hash live in the source config. A
sync re-reads the file only when the hash has changed; if the file has gone
missing the sync fails with a clear message and the library keeps every
chapter it already has.

Chapters have no publication date, so they are undated and ordered by
`source_order` (reading order). The shared sequence in db.py keeps undated
articles grouped by source and ascending in both reading directions, so a
book always reads chapter 1..N.
"""
from __future__ import annotations

import os
import re

from .. import db, epubfile
from .base import Content, Context, Source, Stub


class EpubSource(Source):
    plugin_id = "epub"
    display_name = "Book (EPUB)"
    fetch_concurrency = 1                 # local data; nothing to overlap
    ASSET_PREFIX = epubfile.ASSET_PREFIX

    def __init__(self, row, config: dict | None = None):
        super().__init__(row, config)
        self._book: epubfile.Book | None = None

    @property
    def path(self) -> str:
        return self.config.get("path") or self.homepage

    def discover(self, ctx: Context):
        sha = epubfile.file_sha256(self.path)       # EpubError if missing
        if ctx.known and self.config.get("synced_sha256") == sha:
            ctx.result_note = "book file unchanged"
            return
        ctx.say(f"Reading {os.path.basename(self.path)}…")
        self._book = book = epubfile.open_book(self.path)
        for ch in book.chapters:
            ctx.check()
            yield Stub(
                guid=f"epub:{ch.href}", url=f"epub://{self.id}/{ch.href}",
                title=ch.title, author=book.author or None,
                source_order=ch.order, raw_html=ch.html,
                content_source="epub",
                # A changed file means changed chapters: take them again.
                extra={"refresh": True})
        if not ctx.should_stop():
            ctx.config_updates.update(sha256=sha, synced_sha256=sha)
            ctx.result_note = f"{len(book.chapters)} chapters"

    def fetch_content(self, ctx: Context, url: str, stub_html: str | None = None,
                      base_url: str | None = None, extra: dict | None = None
                      ) -> Content:
        if stub_html:
            return Content(stub_html, status="ok", source="epub")
        # A chapter left unfetched by an earlier run: read it from the file.
        if self._book is None:
            self._book = epubfile.open_book(self.path)
        href = url.split(f"epub://{self.id}/", 1)[-1]
        for ch in self._book.chapters:
            if ch.href == href:
                return Content(ch.html, status="ok", source="epub")
        return Content("", status="empty", source="epub")

    # -- images shipped inside the file ---------------------------------

    def load_asset(self, ref: str) -> bytes | None:
        if self._book is None:
            self._book = epubfile.Book(path=self.path)
        return self._book.read_asset(ref)

    def asset_key_prefix(self) -> str:
        return f"epub:{self.id}/"


def add_book_source(conn, path: str, name: str | None = None):
    """Register an EPUB as a source and return its row.

    The file is opened (and so validated) first: a book that cannot be read
    is reported now, not as a failed sync later. Adding the same file twice
    returns the existing source.
    """
    path = os.path.abspath(os.path.expanduser(path))
    for row in db.list_sources(conn):
        if row["plugin"] == "epub" and row["homepage"] == path:
            return row
    book = epubfile.open_book(path)
    book.close()
    base = "book-" + (re.sub(r"[^a-z0-9]+", "-", book.title.lower()).strip("-")[:40]
                      or "untitled")
    slug, n = base, 1
    while conn.execute("SELECT 1 FROM sources WHERE slug=?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    sid = db.add_source(
        conn, slug, (name or "").strip() or book.title, "epub", path,
        {"path": path, "sha256": epubfile.file_sha256(path),
         "detected": f"EPUB file, {len(book.chapters)} chapters"})
    return db.get_source(conn, sid)
