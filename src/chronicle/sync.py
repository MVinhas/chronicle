"""Archive building: discovery, content fetching, image caching.

Runs off the UI thread. Every step reports progress and honours cancellation,
because a first full build of five sites is ~2,000 articles and takes a while.
Re-running is cheap and idempotent: articles are keyed on a canonical URL, so
re-encountering one updates it rather than duplicating it.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from . import db, htmlutil, images, net, sources
from .sources.base import Cancelled, Context

log = logging.getLogger("chronicle.sync")


@dataclass
class Progress:
    source: str = ""
    message: str = ""
    fraction: float | None = None
    discovered: int = 0
    new: int = 0
    fetched: int = 0
    failed: int = 0
    done: bool = False
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)

    @property
    def eta_seconds(self) -> float | None:
        """Time remaining, estimated from progress made so far.

        Only meaningful once some real fraction of work is behind us --
        the early part of a sync (listing everything before any of it is
        probed) reports no fraction at all, and the first sliver of one that
        does is too noisy an extrapolation to show.
        """
        if self.done or self.fraction is None or self.fraction < 0.02:
            return None
        elapsed = time.monotonic() - self.started_at
        return elapsed * (1 - self.fraction) / self.fraction


class Syncer:
    """Builds and updates the archive. One instance per app; one run at a time."""

    def __init__(self, on_progress=None, browser_fetch=None):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self.on_progress = on_progress or (lambda p: None)
        self.browser_fetch = browser_fetch

    # -- control -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        self._stop.set()
        net.cancel_all()

    def _reset(self) -> None:
        self._stop.clear()
        net.reset_cancel()

    def should_stop(self) -> bool:
        return self._stop.is_set()

    # -- entry points ------------------------------------------------------

    def sync_all(self, source_ids: list[int] | None = None,
                 fetch_content: bool = True, cache_images: bool = True) -> Progress:
        with self._lock:
            if self._running:
                return Progress(message="A sync is already running", done=True)
            self._running = True
        self._reset()
        prog = Progress()
        conn = db.connect()
        db.init(conn)
        try:
            rows = db.list_sources(conn, enabled_only=True)
            if source_ids:
                rows = [r for r in rows if r["id"] in source_ids]
            for row in rows:
                if self.should_stop():
                    break
                self._sync_source(conn, row, prog, fetch_content, cache_images)
            prog.done = True
            prog.message = ("Stopped" if self.should_stop()
                            else f"Archive up to date — {prog.new} new, "
                                 f"{prog.fetched} articles retrieved")
        except Exception as exc:                      # noqa: BLE001 - reported to UI
            prog.error = str(exc)
            prog.done = True
            log.error("sync failed: %s", traceback.format_exc())
        finally:
            self._running = False
            conn.close()
            self._emit(prog)
        return prog

    # -- per source --------------------------------------------------------

    def _sync_source(self, conn, row, prog: Progress, fetch_content: bool,
                     cache_images: bool) -> None:
        prog.source = row["name"]
        self._say(prog, f"Starting {row['name']}…")
        try:
            config = json.loads(row["config"] or "{}")
        except json.JSONDecodeError:
            config = {}
        source = sources.build(row, config)

        ctx = Context(
            progress=lambda msg, frac=None: self._say(prog, msg, frac),
            should_stop=self.should_stop,
            browser_fetch=self.browser_fetch,
        )

        discovered = 0
        pending: list[tuple[int, str, str | None, str | None]] = []
        try:
            for stub in source.discover(ctx):
                if self.should_stop():
                    break
                # Enforced here as well as in the adapter, so a source scoped
                # to one section can never quietly pull in the whole site.
                if not source.in_scope(stub.url):
                    continue
                discovered += 1
                prog.discovered += 1
                article_id, created = self._record(conn, row["id"], stub)
                if created:
                    prog.new += 1
                if fetch_content:
                    need = conn.execute(
                        "SELECT content_status FROM articles WHERE id=?",
                        (article_id,)).fetchone()
                    if need and need["content_status"] in ("pending", "error"):
                        pending.append((article_id, stub.url, stub.raw_html,
                                        stub.base_url, stub.extra))
                if discovered % 25 == 0:
                    self._emit(prog)
        except Cancelled:
            db.mark_sync(conn, row["id"], "stopped", "Cancelled")
            return
        except net.FetchError as exc:
            db.mark_sync(conn, row["id"], "error", str(exc))
            self._say(prog, f"{row['name']}: {exc}")
            return
        except Exception as exc:                      # noqa: BLE001
            db.mark_sync(conn, row["id"], "error", str(exc))
            log.error("discover failed for %s: %s", row["slug"], traceback.format_exc())
            self._say(prog, f"{row['name']} failed: {exc}")
            return

        if fetch_content and not self.should_stop():
            self._fetch_bodies(conn, source, ctx, row, pending, prog, cache_images)

        status = "stopped" if self.should_stop() else "ok"
        db.mark_sync(conn, row["id"], status,
                     f"{discovered} articles, {prog.new} new")

    def _record(self, conn, source_id: int, stub) -> tuple[int, bool]:
        fields = dict(url=stub.url, title=stub.title, author=stub.author,
                      source_order=stub.source_order, **stub.date.as_fields())
        return db.upsert_article(conn, source_id, stub.guid, **fields)

    # -- bodies ------------------------------------------------------------

    def _fetch_bodies(self, conn, source, ctx, row, pending, prog: Progress,
                      cache_images: bool) -> None:
        if not pending:
            # Pick up anything left unfetched by an earlier interrupted run.
            pending = [(r["id"], r["url"], None, None, {})
                       for r in db.pending_content(conn, row["id"])]
        total = len(pending)
        if not total:
            return
        workers = max(1, int(source.fetch_concurrency))
        self._say(prog, f"{row['name']}: retrieving {total} articles…")

        def fetch(item):
            article_id, url, raw_html, base_url, extra = item
            if self.should_stop():
                return article_id, url, None
            try:
                return article_id, url, source.fetch_content(
                    ctx, url, raw_html, base_url, extra=extra)
            except Cancelled:
                return article_id, url, None
            except Exception as exc:                  # noqa: BLE001
                return article_id, url, exc

        done = 0
        # Fetching is network-bound and some archives answer slowly, so it runs
        # in parallel. Everything that touches the database stays on this
        # thread: the connection is not shared across threads.
        #
        # Only a bounded window of work is submitted ahead of what has been
        # processed, rather than the whole `pending` list at once. A plain
        # ThreadPoolExecutor(...).submit() for every item pre-queues the lot,
        # and shutting the pool down (the `with` block's __exit__) then blocks
        # until every already-started worker finishes -- futures not yet
        # started can be cancelled, but ones mid-flight cannot, so stopping a
        # sync against something slow (the Internet Archive answers in
        # 10-15s/page) could take minutes even though should_stop() was set
        # straight away.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            items = iter(pending)
            futures: dict = {}

            def fill():
                for item in items:
                    if self.should_stop():
                        break
                    futures[pool.submit(fetch, item)] = item
                    if len(futures) >= workers * 2:
                        break

            fill()
            try:
                while futures:
                    done_set, _ = wait(futures, return_when=FIRST_COMPLETED)
                    future = done_set.pop()
                    del futures[future]
                    article_id, url, result = future.result()
                    done += 1
                    if done % 5 == 0 or done == total:
                        self._say(prog, f"{row['name']}: article {done} of {total}",
                                  done / max(1, total))
                    if result is not None:
                        if isinstance(result, Exception):
                            db.mark_content_error(conn, article_id, str(result))
                            prog.failed += 1
                        else:
                            self._store(conn, source, article_id, url, result, prog,
                                        cache_images)
                    if self.should_stop():
                        break
                    fill()
            finally:
                for future in futures:
                    future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)

    def _store(self, conn, source, article_id: int, url: str, content,
               prog: Progress, cache_images: bool) -> None:
        html = content.html or ""
        status = content.status
        if status == "ok" and cache_images and not self.should_stop():
            try:
                html, _ = images.cache_images_for(
                    conn, article_id, html, should_stop=self.should_stop)
            except Exception as exc:                  # noqa: BLE001
                log.debug("image caching failed for %s: %s", url, exc)

        row_now = conn.execute("SELECT title FROM articles WHERE id=?",
                               (article_id,)).fetchone()
        if status in ("ok", "partial", "paywalled"):
            db.update_content(
                conn, article_id, html, status=status, source=content.source,
                word_count=htmlutil.word_count(html),
                image_count=htmlutil.count_images(html),
                excerpt=htmlutil.make_excerpt(html),
                content_hash=htmlutil.content_hash(html))
            prog.fetched += 1
            self._maybe_title(conn, article_id, row_now, url)
        else:
            db.mark_content_error(conn, article_id, f"{status}:{content.source}")
            prog.failed += 1

    @staticmethod
    def _maybe_title(conn, article_id: int, row_now, url: str) -> None:
        """Replace a slug-derived placeholder title once we have the real page."""
        title = (row_now["title"] if row_now else "") or ""
        slug = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        if title.strip().lower() != slug.strip().lower():
            return
        art = db.get_article(conn, article_id)
        if not art or not art["content_html"]:
            return
        heading = htmlutil.parse(art["content_html"]).find(["h2", "h3"])
        if heading:
            text = heading.get_text(" ", strip=True)
            if 3 < len(text) < 160:
                conn.execute("UPDATE articles SET title=? WHERE id=?", (text, article_id))

    # -- progress ----------------------------------------------------------

    def _say(self, prog: Progress, message: str, fraction: float | None = None) -> None:
        prog.message = message
        prog.fraction = fraction
        self._emit(prog)

    def _emit(self, prog: Progress) -> None:
        try:
            self.on_progress(prog)
        except Exception:                             # noqa: BLE001
            log.debug("progress callback failed", exc_info=True)
