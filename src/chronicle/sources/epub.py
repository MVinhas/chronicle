"""A book you own, read from a local .epub file.

Nothing is downloaded and no website is involved: the file is the source.

Adding a book copies it into the library (books/<sha256>.epub) and reads the
copy from then on, so the book survives the original being moved or deleted
and, under Flatpak, a document-portal path that only lives for one session.
The source keeps the original absolute path (`homepage`, `origin`) for
display and as the book's identity: adding the same path again is the same
book. On a sync the original is looked at again, if it can still be reached:
when its content hash differs from the stored copy the new version is copied
in and the chapters are re-imported in place (same guids, so read state,
notes and highlights stay). When the original has gone the stored copy is
used and the sync says so; only if the copy is gone as well does it fail.

Chapters have no publication date, so they are undated and ordered by
`source_order` (reading order). The shared sequence in db.py keeps undated
articles grouped by source and ascending in both reading directions, so a
book always reads chapter 1..N.

A chapter's guid is `epub:<href>` (or `epub:<href>#<anchor>` for a chapter
cut out of a larger file). After a complete re-import, guids the new file no
longer has are deleted, except chapters you annotated (note, highlight or
favourite), which stay and sort after the book.
"""
from __future__ import annotations

import os
import re
import threading

from .. import db, epubfile, paths
from .base import Content, Context, Source, Stub

_ADD_LOCK = threading.Lock()
_ORPHAN_ORDER = 1_000_000          # kept-but-removed chapters sort last


class EpubSource(Source):
    plugin_id = "epub"
    display_name = "Book (EPUB)"
    fetch_concurrency = 1                 # local data; nothing to overlap
    ASSET_PREFIX = epubfile.ASSET_PREFIX

    def __init__(self, row, config: dict | None = None):
        super().__init__(row, config)
        self._book: epubfile.Book | None = None
        self._use: str | None = None        # file actually read this run
        self._sha: str | None = None
        self._yielded: set[str] = set()
        self._known: set[str] = set()
        self._complete = False

    @property
    def path(self) -> str:
        """The library's copy (the original for a source from before copies)."""
        return self._use or self.config.get("path") or self.homepage

    @property
    def origin(self) -> str | None:
        return self.config.get("origin")

    def _locate(self, ctx: Context) -> str:
        """Pick the file to read; returns the hash of what the book now is."""
        stored = self.config.get("path") or self.homepage
        origin = self.origin
        if origin and os.path.isfile(origin):
            try:
                sha = epubfile.file_sha256(origin)
            except epubfile.EpubError:
                sha = None                   # unreadable now: use the copy
            if sha:
                self._use = epubfile.store_copy(origin, sha)
                if self._use != stored or sha != self.config.get("sha256"):
                    ctx.config_updates.update(path=self._use, sha256=sha)
                return sha
        self._use = stored
        sha = epubfile.file_sha256(stored)       # EpubError if the copy is gone
        if origin:
            ctx.result_note = "original file not found; using the stored copy"
        return sha

    def discover(self, ctx: Context):
        sha = self._locate(ctx)
        self._sha = sha
        if ctx.known and self.config.get("synced_sha256") == sha:
            ctx.result_note = ((ctx.result_note + "; " if ctx.result_note else "")
                               + "book file unchanged")
            return
        ctx.say(f"Reading {os.path.basename(self.origin or self.path)}…")
        self._book = book = epubfile.open_book(self.path)
        self._known = set(ctx.known or {})
        for ch in book.chapters:
            ctx.check()
            guid = f"epub:{ch.key}"
            self._yielded.add(guid)
            yield Stub(
                guid=guid, url=f"epub://{self.id}/{ch.key}",
                title=ch.title, author=book.author or None,
                source_order=ch.order, raw_html=ch.html,
                content_source="epub",
                # A changed file means changed chapters: take them again.
                extra={"refresh": True})
        if not ctx.should_stop():
            self._complete = True
            note = f"{len(book.chapters)} chapters"
            ctx.result_note = (ctx.result_note + "; " if ctx.result_note else "") + note

    def finish(self, conn, ctx: Context) -> None:
        """Called once a sync ran to the end with every body stored.

        Only now is the file recorded as taken in (a cancelled refresh is
        redone next time) and only now are chapters the file no longer has
        removed.
        """
        if self._book is not None and self._complete and self._sha:
            row = db.get_source(conn, self.id)
            import json
            cfg = json.loads(row["config"] or "{}")
            cfg["synced_sha256"] = self._sha
            db.set_source_config(conn, self.id, cfg)
            kept = self._retire_vanished(conn)
            if kept:
                ctx.result_note += f"; kept {kept} removed chapters you annotated"
        epubfile.prune_copies({
            _config_path(r) for r in db.list_sources(conn)
            if r["plugin"] == "epub"})

    def _retire_vanished(self, conn) -> int:
        kept = 0
        for guid in sorted(self._known - self._yielded):
            art = conn.execute(
                "SELECT a.id, a.source_order, "
                " EXISTS(SELECT 1 FROM notes n WHERE n.article_id=a.id "
                "        AND n.body != '') AS has_note, "
                " EXISTS(SELECT 1 FROM highlights h WHERE h.article_id=a.id) AS has_hl, "
                " EXISTS(SELECT 1 FROM reading_state r WHERE r.article_id=a.id "
                "        AND r.favourite_at IS NOT NULL) AS fav "
                "FROM articles a WHERE a.source_id=? AND a.guid=?",
                (self.id, guid)).fetchone()
            if art is None:
                continue
            if art["has_note"] or art["has_hl"] or art["fav"]:
                kept += 1
                if art["source_order"] < _ORPHAN_ORDER:
                    conn.execute("UPDATE articles SET source_order=? WHERE id=?",
                                 (_ORPHAN_ORDER + art["source_order"], art["id"]))
            else:
                conn.execute("DELETE FROM articles WHERE id=?", (art["id"],))
        return kept

    def fetch_content(self, ctx: Context, url: str, stub_html: str | None = None,
                      base_url: str | None = None, extra: dict | None = None
                      ) -> Content:
        if stub_html:
            return Content(stub_html, status="ok", source="epub")
        # A chapter left unfetched by an earlier run: read it from the file.
        if self._book is None:
            if self._use is None:
                self._locate(ctx)
            self._book = epubfile.open_book(self.path)
        key = url.split(f"epub://{self.id}/", 1)[-1]
        for ch in self._book.chapters:
            if ch.key == key:
                return Content(ch.html, status="ok", source="epub")
        return Content("", status="empty", source="epub")

    # -- images shipped inside the file ---------------------------------

    def load_asset(self, ref: str) -> bytes | None:
        if self._book is None:
            self._book = epubfile.Book(path=self.path)
        return self._book.read_asset(ref)

    def asset_key_prefix(self) -> str:
        return f"epub:{self.id}/"


def _config_path(row) -> str:
    import json
    try:
        cfg = json.loads(row["config"] or "{}")
    except ValueError:
        cfg = {}
    return cfg.get("path") or row["homepage"]


def find_book_source(conn, path: str):
    """The source already holding this file (same original path), or None."""
    want = os.path.realpath(os.path.expanduser(path))
    for row in db.list_sources(conn):
        if row["plugin"] != "epub":
            continue
        import json
        try:
            origin = json.loads(row["config"] or "{}").get("origin") or row["homepage"]
        except ValueError:
            origin = row["homepage"]
        if os.path.realpath(origin) == want:
            return row
    return None


def add_book_source(conn, path: str, name: str | None = None):
    """Register an EPUB as a source and return its row.

    The file is opened (and so validated) first: a book that cannot be read
    is reported now, not as a failed sync later. It is then copied into the
    library. Identity is the original absolute path: adding it again returns
    the existing source (use find_book_source to know that beforehand), and
    the next sync picks up any change to the file.
    """
    import json
    path = os.path.abspath(os.path.expanduser(path))
    with _ADD_LOCK:                       # check-then-insert must be atomic
        existing = find_book_source(conn, path)
        if existing is not None:
            return existing
        book = epubfile.open_book(path)
        book.close()
        sha = epubfile.file_sha256(path)
        stored = epubfile.store_copy(path, sha)
        base = "book-" + (re.sub(r"[^a-z0-9]+", "-", book.title.lower()).strip("-")[:40]
                          or "untitled")
        slug, n = base, 1
        while conn.execute("SELECT 1 FROM sources WHERE slug=?", (slug,)).fetchone():
            n += 1
            slug = f"{base}-{n}"
        sid = db.add_source(
            conn, slug, (name or "").strip() or book.title, "epub", path,
            {"path": stored, "origin": path, "sha256": sha,
             "detected": f"EPUB file, {len(book.chapters)} chapters"})
        return db.get_source(conn, sid)
