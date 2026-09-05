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
from datetime import datetime, timedelta, timezone

from . import db, htmlutil, images, net, sources
from .sources.base import Cancelled, Context

log = logging.getLogger("chronicle.sync")

# Requests to one host are spaced apart to be polite. Building a blog's whole
# archive is the one job where that spacing dominates -- 549 pages at 0.6s is
# five and a half minutes -- so a first build runs at half the interval.
FIRST_BUILD_RATE_SCALE = 0.5

# How often to look at a blog, as a fraction of how often it actually posts.
# A quarter means a daily blog is checked a few times a day and a monthly one
# every few days: often enough to catch a post within a fraction of its own
# cycle, seldom enough that opening the app does not question every server at
# once for news none of them have.
CHECK_FRACTION = 0.25
MIN_CHECK = timedelta(hours=1)
MAX_CHECK = timedelta(days=3)
CADENCE_SAMPLE = 12        # recent posts used to measure the rhythm


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


def _cadence(conn, source_id: int) -> timedelta | None:
    """How long this blog typically leaves between posts, or None if unclear.

    The median gap rather than the mean: a blog that posted six times in one
    week in 2013 and twice since should not be read as posting every few days.
    """
    stamps = [d for d in (_parse(r["published_at"]) for r in conn.execute(
        "SELECT published_at FROM articles WHERE source_id=? AND published_at "
        "IS NOT NULL ORDER BY published_at DESC LIMIT ?",
        (source_id, CADENCE_SAMPLE))) if d]
    if len(stamps) < 3:
        return None
    gaps = sorted(stamps[i] - stamps[i + 1] for i in range(len(stamps) - 1))
    return gaps[len(gaps) // 2]


# Retrying a page that keeps failing costs a request every sync, for a page
# that has never once worked. mrmoneymustache.com holds twelve of them --
# announcements from 2011 to 2019 that no longer resolve -- and at three
# seconds a page they were most of the cost of every update.
#
# So a failing page is left alone for a while, and the while grows with how
# long it has been failing: something that broke yesterday is worth another
# try tomorrow, something that has been broken for a year is not. No attempt
# counter is needed for that -- the span between when the article was found
# and when it was last tried says the same thing.
RETRY_BACKOFF = 0.5
MIN_RETRY = timedelta(days=1)
MAX_RETRY = timedelta(days=30)


def _cooling(row, now: datetime | None = None) -> bool:
    """Whether a failed article is still inside its back-off window."""
    last = _parse(row["content_fetched_at"])
    if last is None:
        return False                       # never actually tried; try now
    found = _parse(row["discovered_at"]) or last
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    wait = max(MIN_RETRY, min(MAX_RETRY, (last - found) * RETRY_BACKOFF))
    return now - last < wait


def due_sources(conn, now: datetime | None = None) -> list[int]:
    """The blogs worth asking right now, judged by their own publishing rhythm.

    Checking all of them on every launch is what makes an automatic update
    expensive: seven blogs, most of which publish monthly, all questioned
    because one of them publishes daily. Chronicle holds each blog's entire
    history, so it can tell them apart -- a blog is asked again after a
    quarter of its own typical gap between posts, bounded either side.

    A blog that has gone quiet counts as slow rather than as due: the interval
    is measured against the longer of its usual gap and its actual silence, so
    a site that stopped publishing in 2019 is not questioned every hour on the
    grounds that it once posted weekly.

    Nothing goes unasked for more than MAX_CHECK, and pressing the button
    still asks everything -- this only decides what is worth doing unprompted.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    due: list[int] = []
    for row in db.list_sources(conn, enabled_only=True):
        last = _parse(row["last_sync_at"])
        if last is None:
            due.append(row["id"])          # never synced: nothing to go on
            continue
        cadence = _cadence(conn, row["id"])
        if cadence is None:
            interval = MIN_CHECK           # too little history to judge
        else:
            newest = _parse(conn.execute(
                "SELECT MAX(published_at) m FROM articles WHERE source_id=?",
                (row["id"],)).fetchone()["m"])
            silence = (now - newest) if newest else cadence
            interval = max(cadence, silence) * CHECK_FRACTION
            interval = max(MIN_CHECK, min(MAX_CHECK, interval))
        if now - last >= interval:
            due.append(row["id"])
    return due


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
                 fetch_content: bool = True, cache_images: bool = True,
                 newest_only: bool = False) -> Progress:
        """Build or update the archive.

        `newest_only` is the routine update: enumerate only the routes that
        list the newest posts first and stop where they meet what is already
        archived, rather than re-examining a site's whole history. It is a
        cost decision, not a correctness one -- anything it does find is
        recorded exactly as a full scan would record it.
        """
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
                self._sync_source(conn, row, prog, fetch_content, cache_images,
                                  newest_only)
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
                     cache_images: bool, newest_only: bool = False) -> None:
        prog.source = row["name"]
        self._say(prog, f"Starting {row['name']}…")
        try:
            config = json.loads(row["config"] or "{}")
        except json.JSONDecodeError:
            config = {}

        # Heal sources whose stored route no longer exists — a plugin that
        # was removed, or the retired Internet Archive strategy. The site may
        # be perfectly alive (fs.blog was); re-detection finds today's best
        # route instead of failing forever on yesterday's.
        if row["plugin"] not in sources.REGISTRY or config.get("strategy") == "wayback":
            row, config = self._redetect(conn, row, config, prog)
            if row is None:
                return

        source = sources.build(row, config)

        # What this source already has, so discovery can skip what is settled
        # instead of paying one request per already-archived article.
        def _state(r) -> str:
            if r["content_status"] in ("ok", "partial", "paywalled"):
                return "ok"
            if r["content_status"] == "gone":
                return "gone"
            if r["content_status"] == "error" and _cooling(r):
                return "cooling"
            return "missing"

        known = {
            r["guid"]: (r["published_at"] is not None, _state(r),
                        db._date_rank(r["date_confidence"]))
            for r in conn.execute(
                "SELECT guid, published_at, content_status, date_confidence, "
                "content_fetched_at, discovered_at "
                "FROM articles WHERE source_id=?", (row["id"],))
        }

        # The newest date already archived, so a cheap enumeration knows when
        # it has reached settled ground. A source with nothing archived yet has
        # no such floor, and gets a full scan whatever was asked for -- there
        # is no "new since" without a "since".
        newest_known = conn.execute(
            "SELECT MAX(published_at) m FROM articles WHERE source_id=?",
            (row["id"],)).fetchone()["m"]
        incremental = bool(newest_only and newest_known)

        # A blog with nothing archived yet is a first build: several hundred
        # pages of one site, where the polite spacing between requests *is*
        # the cost. Halve it for that one job. Routine updates fetch a handful
        # of pages and can afford to be gentler, so they go back to full.
        net.set_rate_scale(FIRST_BUILD_RATE_SCALE if not known else 1.0)

        new_rejects: list[str] = []
        ctx = Context(
            progress=lambda msg, frac=None: self._say(prog, msg, frac),
            should_stop=self.should_stop,
            browser_fetch=self.browser_fetch,
            known=known,
            rejected=db.rejected_guids(conn, row["id"]),
            reject=new_rejects.append,
            newest_only=incremental,
            newest_known=newest_known,
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
                    status = need["content_status"] if need else None
                    # 'gone' means the origin 404s; retry it only when this
                    # stub brings a new route (a feed-supplied body).
                    retry_gone = status == "gone" and bool(stub.raw_html)
                    if status in ("pending", "error") or retry_gone:
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
        finally:
            # Verdicts already reached are valid however discovery ended.
            if new_rejects:
                db.record_rejects(conn, row["id"], new_rejects)
            # Hints the source learned (where the feed and archive index
            # live) save the next sync the same search.
            if ctx.config_updates:
                db.set_source_config(conn, row["id"],
                                     {**config, **ctx.config_updates})

        if fetch_content and not self.should_stop():
            # Also pick up anything left unfetched by earlier runs — a stub
            # for it may not have been yielded this time at all.
            #
            # Not in "fetch new posts" mode, though: a half-built archive can
            # hold thousands of pending bodies, and draining them is exactly
            # the long job the user asked to skip. They are not lost, only
            # deferred to the next full scan.
            if not incremental:
                have = {item[0] for item in pending}
                for r in db.pending_content(conn, row["id"]):
                    if r["id"] not in have:
                        pending.append((r["id"], r["url"], None, None, {}))
            self._fetch_bodies(conn, source, ctx, row, pending, prog, cache_images)

        net.set_rate_scale(1.0)
        status = "stopped" if self.should_stop() else "ok"
        note = f" — {ctx.result_note}" if ctx.result_note else ""
        kind = "new posts" if incremental else "full scan"
        db.mark_sync(conn, row["id"], status,
                     f"{kind}: {discovered} examined, {prog.new} new{note}")

    def _redetect(self, conn, row, config, prog: Progress):
        """Re-run detection for a source whose stored route is obsolete.

        Returns the refreshed (row, config), or (None, None) when the site
        genuinely cannot be reached — in which case the failure has been
        recorded on the source.
        """
        self._say(prog, f"{row['name']}: re-detecting how to build this archive…")
        try:
            spec = sources.detect(row["homepage"] or config.get("feed") or "")
        except Exception as exc:                      # noqa: BLE001
            db.mark_sync(conn, row["id"], "error", str(exc))
            self._say(prog, f"{row['name']}: {exc}")
            return None, None
        new_config = dict(spec.get("config") or {})
        # A section scope the user asked for survives the route change.
        if config.get("path_prefix") and "path_prefix" not in new_config:
            new_config["path_prefix"] = config["path_prefix"]
        new_config["detected"] = spec["detected"] + " (re-detected)"
        db.update_source_route(conn, row["id"], spec["plugin"], new_config,
                               spec["homepage"])
        self._say(prog, f"{row['name']}: now using {spec['detected']}")
        return db.get_source(conn, row["id"]), new_config

    def _record(self, conn, source_id: int, stub) -> tuple[int, bool]:
        # A metadata-only stub may carry no title; never blank a stored one.
        fields = dict(url=stub.url, title=stub.title or None, author=stub.author,
                      source_order=stub.source_order, **stub.date.as_fields())
        return db.upsert_article(conn, source_id, stub.guid, **fields)

    # -- bodies ------------------------------------------------------------

    def _fetch_bodies(self, conn, source, ctx, row, pending, prog: Progress,
                      cache_images: bool) -> None:
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
        # sync against a slow origin could take minutes even though
        # should_stop() was set straight away.
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
                            # A page the origin says is definitively gone is
                            # not worth one request on every future sync.
                            permanent = (isinstance(result, net.FetchError)
                                         and result.status in (404, 410, 451))
                            db.mark_content_error(conn, article_id, str(result),
                                                  permanent=permanent)
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
