"""SQLite storage layer.

Single-writer / multi-reader. The UI thread owns one connection; sync workers
open their own. WAL keeps readers unblocked during a long archive build.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from . import paths

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id                INTEGER PRIMARY KEY,
    slug              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    homepage          TEXT NOT NULL DEFAULT '',
    plugin            TEXT NOT NULL,
    config            TEXT NOT NULL DEFAULT '{}',
    enabled           INTEGER NOT NULL DEFAULT 1,
    added_at          TEXT NOT NULL,
    last_sync_at      TEXT,
    last_sync_status  TEXT,
    last_sync_message TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    id                INTEGER PRIMARY KEY,
    source_id         INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    guid              TEXT NOT NULL,
    url               TEXT NOT NULL,
    title             TEXT NOT NULL,
    author            TEXT,

    -- Publication date. published_at is the START of the precision window,
    -- stored as naive-UTC ISO8601 so it sorts lexicographically.
    published_at      TEXT,
    date_precision    TEXT NOT NULL DEFAULT 'unknown',  -- day|month|year|unknown
    date_confidence   TEXT NOT NULL DEFAULT 'unknown',  -- exact|high|medium|inferred|unknown
    date_source       TEXT NOT NULL DEFAULT '',
    modified_at       TEXT,

    content_html      TEXT,
    excerpt           TEXT NOT NULL DEFAULT '',
    word_count        INTEGER NOT NULL DEFAULT 0,
    image_count       INTEGER NOT NULL DEFAULT 0,
    content_status    TEXT NOT NULL DEFAULT 'pending',  -- pending|ok|partial|paywalled|empty|error
    content_source    TEXT NOT NULL DEFAULT '',
    content_fetched_at TEXT,
    content_hash      TEXT,

    source_order      INTEGER NOT NULL DEFAULT 0,
    discovered_at     TEXT NOT NULL,
    UNIQUE (source_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_articles_chrono
    ON articles (published_at IS NULL, published_at, source_order, id);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles (source_id);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles (content_status);
CREATE INDEX IF NOT EXISTS idx_articles_url    ON articles (url);

CREATE TABLE IF NOT EXISTS reading_state (
    article_id     INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    read_at        TEXT,
    favourite_at   TEXT,
    scroll_pos     REAL NOT NULL DEFAULT 0,
    last_opened_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reading_fav  ON reading_state (favourite_at);
CREATE INDEX IF NOT EXISTS idx_reading_read ON reading_state (read_at);

CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY,
    digest     TEXT NOT NULL UNIQUE,
    orig_url   TEXT NOT NULL,
    mime       TEXT NOT NULL DEFAULT '',
    bytes      INTEGER NOT NULL DEFAULT 0,
    relpath    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_url ON images (orig_url);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, excerpt, content='articles', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS articles_fts_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, excerpt) VALUES (new.id, new.title, new.excerpt);
END;
CREATE TRIGGER IF NOT EXISTS articles_fts_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, excerpt)
        VALUES ('delete', old.id, old.title, old.excerpt);
END;
CREATE TRIGGER IF NOT EXISTS articles_fts_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, excerpt)
        VALUES ('delete', old.id, old.title, old.excerpt);
    INSERT INTO articles_fts(rowid, title, excerpt) VALUES (new.id, new.title, new.excerpt);
END;
"""

_local = threading.local()
_lock_handle = None


class LibraryBusy(Exception):
    """Another Chronicle process already has the library open."""


def acquire_library_lock(blocking: bool = False) -> bool:
    """Take an exclusive, process-wide lock on the library.

    SQLite's WAL mode coordinates readers and writers through a shared-memory
    index (the -shm file). Across Flatpak sandboxes that memory is not actually
    shared, so a reader in one sandbox and a writer in another do not merely see
    stale data — they see *inconsistent* data, with WAL frames misattributed to
    the wrong rows. Rather than give up WAL (which is what keeps the UI
    responsive during a long archive build), Chronicle allows only one process
    to hold the library at a time.
    """
    global _lock_handle
    if _lock_handle is not None:
        return True
    paths.ensure_dirs()
    handle = open(paths.DATA_DIR / "library.lock", "w")
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except OSError:
        handle.close()
        return False
    handle.write(str(os.getpid()))
    handle.flush()
    _lock_handle = handle
    return True


def release_library_lock() -> None:
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_UN)
        _lock_handle.close()
    except OSError:
        pass
    _lock_handle = None


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the WAL back into the main database file."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def connect(path=None) -> sqlite3.Connection:
    paths.ensure_dirs()
    conn = sqlite3.connect(str(path or paths.DB_PATH), timeout=30.0,
                           detect_types=0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn() -> sqlite3.Connection:
    """Thread-local connection."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        init(conn)
        _local.conn = conn
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                     (str(SCHEMA_VERSION),))


# --------------------------------------------------------------------------
# app_state helpers
# --------------------------------------------------------------------------

def state_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def state_set(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO app_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def add_source(conn, slug: str, name: str, plugin: str, homepage: str = "",
               config: dict | None = None, enabled: bool = True) -> int:
    conn.execute(
        "INSERT INTO sources(slug,name,plugin,homepage,config,enabled,added_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
        (slug, name, plugin, homepage, json.dumps(config or {}),
         1 if enabled else 0, utcnow()))
    row = conn.execute("SELECT id FROM sources WHERE slug=?", (slug,)).fetchone()
    return row["id"]


def list_sources(conn, enabled_only: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM sources"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY name COLLATE NOCASE"
    return conn.execute(q).fetchall()


def get_source(conn, source_id: int):
    return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def delete_source(conn, source_id: int) -> None:
    conn.execute("DELETE FROM sources WHERE id=?", (source_id,))


def set_source_enabled(conn, source_id: int, enabled: bool) -> None:
    conn.execute("UPDATE sources SET enabled=? WHERE id=?",
                 (1 if enabled else 0, source_id))


def rename_source(conn, source_id: int, name: str) -> None:
    name = (name or "").strip()
    if name:
        conn.execute("UPDATE sources SET name=? WHERE id=?", (name, source_id))


def mark_sync(conn, source_id: int, status: str, message: str = "") -> None:
    conn.execute(
        "UPDATE sources SET last_sync_at=?, last_sync_status=?, last_sync_message=? WHERE id=?",
        (utcnow(), status, message[:500], source_id))


# --------------------------------------------------------------------------
# articles
# --------------------------------------------------------------------------

ARTICLE_UPSERT_FIELDS = (
    "url", "title", "author", "published_at", "date_precision", "date_confidence",
    "date_source", "modified_at", "source_order",
)


def upsert_article(conn, source_id: int, guid: str, **fields) -> tuple[int, bool]:
    """Insert or update an article's *metadata*. Returns (article_id, created).

    Never clobbers existing content; content is written by update_content().
    Never overwrites a good date with a worse one.
    """
    row = conn.execute("SELECT * FROM articles WHERE source_id=? AND guid=?",
                       (source_id, guid)).fetchone()
    if row is None:
        cols = ["source_id", "guid", "discovered_at"]
        vals: list[Any] = [source_id, guid, utcnow()]
        for f in ARTICLE_UPSERT_FIELDS:
            if f in fields and fields[f] is not None:
                cols.append(f)
                vals.append(fields[f])
        placeholders = ",".join("?" * len(cols))
        cur = conn.execute(
            f"INSERT INTO articles({','.join(cols)}) VALUES({placeholders})", vals)
        return cur.lastrowid, True

    # Existing: update only fields that improve on what we have.
    sets, vals = [], []
    for f in ARTICLE_UPSERT_FIELDS:
        if f not in fields or fields[f] is None:
            continue
        if f in ("published_at", "date_precision", "date_confidence", "date_source"):
            continue  # handled together below
        if row[f] != fields[f]:
            sets.append(f"{f}=?")
            vals.append(fields[f])

    # An article has exactly one source, so that source's latest reading is
    # authoritative -- including when it is now *less* certain than before,
    # which is what happens after an adapter is corrected. A date that used to
    # be read confidently out of a page's prose should be able to become an
    # honest estimate.
    #
    # The one thing that must never happen is an unknown date wiping a known
    # one: that would let a single failed fetch erase good data.
    if fields.get("published_at"):
        for f in ("published_at", "date_precision", "date_confidence",
                  "date_source"):
            if fields.get(f) is not None:
                sets.append(f"{f}=?")
                vals.append(fields[f])
    if sets:
        vals.append(row["id"])
        conn.execute(f"UPDATE articles SET {','.join(sets)} WHERE id=?", vals)
    return row["id"], False


_CONF_RANK = {"unknown": 0, "inferred": 1, "medium": 2, "high": 3, "exact": 4}


def _date_rank(conf: str | None) -> int:
    return _CONF_RANK.get(conf or "unknown", 0)


def update_content(conn, article_id: int, html: str, *, status: str,
                   source: str, word_count: int, image_count: int,
                   excerpt: str, content_hash: str) -> None:
    conn.execute(
        "UPDATE articles SET content_html=?, content_status=?, content_source=?, "
        "word_count=?, image_count=?, excerpt=?, content_hash=?, content_fetched_at=? "
        "WHERE id=?",
        (html, status, source, word_count, image_count, excerpt, content_hash,
         utcnow(), article_id))


def mark_content_error(conn, article_id: int, message: str) -> None:
    conn.execute(
        "UPDATE articles SET content_status='error', content_source=?, content_fetched_at=? "
        "WHERE id=?", (message[:120], utcnow(), article_id))


def pending_content(conn, source_id: int | None = None, limit: int = 100000):
    q = ("SELECT a.*, s.plugin, s.config, s.slug AS source_slug, s.name AS source_name "
         "FROM articles a JOIN sources s ON s.id=a.source_id "
         "WHERE a.content_status IN ('pending','error')")
    args: list[Any] = []
    if source_id is not None:
        q += " AND a.source_id=?"
        args.append(source_id)
    q += " ORDER BY (a.published_at IS NULL), a.published_at LIMIT ?"
    args.append(limit)
    return conn.execute(q, args).fetchall()


# --------------------------------------------------------------------------
# the reading queue
# --------------------------------------------------------------------------

QUEUE_SELECT = """
SELECT a.id, a.title, a.url, a.published_at, a.date_precision, a.date_confidence,
       a.date_source, a.excerpt, a.word_count, a.image_count, a.content_status,
       a.source_id, s.name AS source_name, s.slug AS source_slug,
       r.read_at, r.favourite_at, r.scroll_pos
FROM articles a
JOIN sources s ON s.id = a.source_id
LEFT JOIN reading_state r ON r.article_id = a.id
"""

ORDER_CHRONO = " ORDER BY (a.published_at IS NULL), a.published_at ASC, a.source_order ASC, a.id ASC"
ORDER_CHRONO_DESC = " ORDER BY (a.published_at IS NULL), a.published_at DESC, a.source_order DESC, a.id DESC"


def _filter_sql(scope: str, include_disabled: bool) -> tuple[str, list]:
    where = ["a.content_status IN ('ok','partial','paywalled')"]
    args: list[Any] = []
    if not include_disabled:
        where.append("s.enabled = 1")
    if scope == "unread":
        where.append("r.read_at IS NULL")
    elif scope == "read":
        where.append("r.read_at IS NOT NULL")
    elif scope == "favourites":
        where.append("r.favourite_at IS NOT NULL")
    return " WHERE " + " AND ".join(where), args


def queue(conn, scope: str = "all", limit: int = 500, offset: int = 0,
          source_id: int | None = None, search: str | None = None,
          newest_first: bool = False) -> list[sqlite3.Row]:
    where, args = _filter_sql(scope, include_disabled=False)
    if source_id:
        where += " AND a.source_id=?"
        args.append(source_id)
    if search:
        where += " AND a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)"
        args.append(_fts_query(search))
    order = ORDER_CHRONO_DESC if newest_first else ORDER_CHRONO
    q = QUEUE_SELECT + where + order + " LIMIT ? OFFSET ?"
    return conn.execute(q, args + [limit, offset]).fetchall()


def _fts_query(text: str) -> str:
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " "
                                for c in text).split() if t]
    return " ".join(f'"{t}"*' for t in terms) or '""'


def queue_counts(conn) -> dict[str, int]:
    base = ("FROM articles a JOIN sources s ON s.id=a.source_id "
            "LEFT JOIN reading_state r ON r.article_id=a.id "
            "WHERE s.enabled=1 AND a.content_status IN ('ok','partial','paywalled')")
    out = {}
    out["all"] = conn.execute(f"SELECT COUNT(*) c {base}").fetchone()["c"]
    out["unread"] = conn.execute(f"SELECT COUNT(*) c {base} AND r.read_at IS NULL").fetchone()["c"]
    out["read"] = out["all"] - out["unread"]
    out["favourites"] = conn.execute(
        f"SELECT COUNT(*) c {base} AND r.favourite_at IS NOT NULL").fetchone()["c"]
    out["undated"] = conn.execute(
        f"SELECT COUNT(*) c {base} AND a.published_at IS NULL").fetchone()["c"]
    return out


def get_article(conn, article_id: int):
    return conn.execute(
        "SELECT a.*, s.name AS source_name, s.slug AS source_slug, s.homepage "
        "FROM articles a JOIN sources s ON s.id=a.source_id WHERE a.id=?",
        (article_id,)).fetchone()


def neighbour(conn, article_id: int, direction: int, scope: str = "all"):
    """Next (+1) or previous (-1) article in the current reading order."""
    cur = get_article(conn, article_id)
    if cur is None:
        return None
    where, args = _filter_sql(scope, include_disabled=False)
    # Build a strict lexicographic comparison on (null-rank, published_at, source_order, id)
    nullrank = 1 if cur["published_at"] is None else 0
    pub = cur["published_at"]
    key = [nullrank, pub, cur["source_order"], cur["id"]]
    if direction > 0:
        cmp = ("((a.published_at IS NULL) > ?) OR ((a.published_at IS NULL) = ? AND ("
               "COALESCE(a.published_at,'') > COALESCE(?,'') OR (COALESCE(a.published_at,'') = COALESCE(?,'') AND ("
               "a.source_order > ? OR (a.source_order = ? AND a.id > ?)))))")
        order = ORDER_CHRONO
    else:
        cmp = ("((a.published_at IS NULL) < ?) OR ((a.published_at IS NULL) = ? AND ("
               "COALESCE(a.published_at,'') < COALESCE(?,'') OR (COALESCE(a.published_at,'') = COALESCE(?,'') AND ("
               "a.source_order < ? OR (a.source_order = ? AND a.id < ?)))))")
        order = ORDER_CHRONO_DESC
    q = QUEUE_SELECT + where + f" AND ({cmp})" + order + " LIMIT 1"
    return conn.execute(q, args + [nullrank, nullrank, pub, pub,
                                   key[2], key[2], key[3]]).fetchone()


def position_in_queue(conn, article_id: int, scope: str = "all") -> tuple[int, int]:
    """1-based position of an article in the queue, and the queue total."""
    cur = get_article(conn, article_id)
    where, args = _filter_sql(scope, include_disabled=False)
    total = conn.execute(
        "SELECT COUNT(*) c FROM articles a JOIN sources s ON s.id=a.source_id "
        "LEFT JOIN reading_state r ON r.article_id=a.id" + where, args).fetchone()["c"]
    if cur is None:
        return 0, total
    nullrank = 1 if cur["published_at"] is None else 0
    before = conn.execute(
        "SELECT COUNT(*) c FROM articles a JOIN sources s ON s.id=a.source_id "
        "LEFT JOIN reading_state r ON r.article_id=a.id" + where +
        " AND (((a.published_at IS NULL) < ?) OR ((a.published_at IS NULL) = ? AND ("
        "COALESCE(a.published_at,'') < COALESCE(?,'') OR (COALESCE(a.published_at,'') = COALESCE(?,'') AND ("
        "a.source_order < ? OR (a.source_order = ? AND a.id < ?))))))",
        args + [nullrank, nullrank, cur["published_at"], cur["published_at"],
                cur["source_order"], cur["source_order"], cur["id"]]).fetchone()["c"]
    return before + 1, total


# --------------------------------------------------------------------------
# reading state
# --------------------------------------------------------------------------

def _ensure_state(conn, article_id: int) -> None:
    conn.execute("INSERT INTO reading_state(article_id) VALUES(?) "
                 "ON CONFLICT(article_id) DO NOTHING", (article_id,))


def set_read(conn, article_id: int, read: bool = True) -> None:
    _ensure_state(conn, article_id)
    conn.execute("UPDATE reading_state SET read_at=? WHERE article_id=?",
                 (utcnow() if read else None, article_id))


def toggle_favourite(conn, article_id: int) -> bool:
    _ensure_state(conn, article_id)
    row = conn.execute("SELECT favourite_at FROM reading_state WHERE article_id=?",
                       (article_id,)).fetchone()
    now = None if row["favourite_at"] else utcnow()
    conn.execute("UPDATE reading_state SET favourite_at=? WHERE article_id=?",
                 (now, article_id))
    return now is not None


def set_scroll(conn, article_id: int, pos: float) -> None:
    _ensure_state(conn, article_id)
    conn.execute("UPDATE reading_state SET scroll_pos=?, last_opened_at=? WHERE article_id=?",
                 (pos, utcnow(), article_id))


def first_unread(conn):
    rows = queue(conn, scope="unread", limit=1)
    return rows[0] if rows else None


def resume_article(conn):
    """The article to open on launch: the remembered one, else first unread."""
    aid = state_get(conn, "current_article_id")
    if aid:
        row = get_article(conn, int(aid))
        if row and row["content_status"] in ("ok", "partial", "paywalled"):
            return row
    r = first_unread(conn)
    return get_article(conn, r["id"]) if r else None


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------

def find_image(conn, orig_url: str):
    return conn.execute("SELECT * FROM images WHERE orig_url=?", (orig_url,)).fetchone()


def record_image(conn, digest: str, orig_url: str, mime: str, size: int,
                 relpath: str) -> None:
    conn.execute(
        "INSERT INTO images(digest,orig_url,mime,bytes,relpath,fetched_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(digest) DO NOTHING",
        (digest, orig_url, mime, size, relpath, utcnow()))


def stats(conn) -> dict:
    a = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
    ok = conn.execute("SELECT COUNT(*) c FROM articles WHERE content_status='ok'").fetchone()["c"]
    im = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(bytes),0) b FROM images").fetchone()
    rng = conn.execute(
        "SELECT MIN(published_at) a, MAX(published_at) b FROM articles "
        "WHERE published_at IS NOT NULL").fetchone()
    return {"articles": a, "with_content": ok, "images": im["c"],
            "image_bytes": im["b"], "oldest": rng["a"], "newest": rng["b"]}
