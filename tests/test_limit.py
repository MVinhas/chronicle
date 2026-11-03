"""The "last N posts" limit: how far back fetching reaches.

The limit bounds what is *fetched*, never what is kept: stored posts are not
deleted when it is lowered. "Newest N" is measured over the source's whole
known set (stored plus discovered), newest first, undated after dated.
"""
from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timedelta
from unittest import mock

from chronicle import db, sources, sync
from chronicle.sources.base import Context

from fakesite import FakeNet, post_html, rss_xml, sitemap_xml
from test_discovery import (BASE, WP, FakeWordPress, SyncHarness, run_sync)


def _day(i: int) -> str:
    return (datetime(2015, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%dT10:00:00")


def _rfc(i: int) -> str:
    return (datetime(2015, 1, 1) + timedelta(days=i)).strftime(
        "%a, %d %b %Y 08:00:00 +0000")


def _wp_spec(limit):
    spec = dict(WP, config=dict(WP["config"]))
    if limit is not None:
        spec["config"]["limit"] = limit
    return spec


def _generic_spec(limit):
    return {"name": "B", "plugin": "generic", "homepage": BASE,
            "config": {"limit": limit} if limit else {}}


def _feed_site(n: int, per_page: int = 50, start: int = 0) -> FakeNet:
    """A feed of n dated, body-less items, newest first, paged WordPress-style;
    every post page exists. Post i is dated day i."""
    fn = FakeNet()
    items = []
    for i in reversed(range(start, n)):
        u = f"{BASE}/post-{i}/"
        fn.add(u, post_html(f"Post {i}", _day(i) + "+00:00"))
        items.append({"link": u, "title": f"Post {i}", "date": _rfc(i),
                      "content": False})
    pages = [items[k:k + per_page] for k in range(0, n, per_page)]
    fn.add(f"{BASE}/feed/", rss_xml(BASE, pages[0]))
    for k, page in enumerate(pages[1:], start=2):
        fn.add(f"{BASE}/feed/?paged={k}", rss_xml(BASE, page))
    fn.add(BASE + "/", "<html><head><title>B</title>"
           '<link rel="alternate" type="application/rss+xml" href="/feed/">'
           "</head></html>")
    return fn


class LimitHarness(SyncHarness):
    def set_limit(self, limit):
        conn = self.conn()
        sid = conn.execute("SELECT id FROM sources").fetchone()["id"]
        db.set_source_limit(conn, sid, limit)
        conn.close()

    def slugs(self):
        return sorted(r["url"].rstrip("/").rsplit("/", 1)[-1] for r in self.urls())


class TestWordPressLimit(LimitHarness):
    def _posts(self, n=300):
        return [(_day(i), f"post-{i}", f"Post {i}") for i in range(n)]

    def test_exactly_the_newest_n_in_bounded_requests(self):
        fn = FakeWordPress(self._posts())
        prog = run_sync(fn, self.dbfile, spec=_wp_spec(50))
        want = sorted(f"post-{i}" for i in range(250, 300))
        self.assertEqual(self.slugs(), want)
        self.assertEqual(prog.fetched, 50)
        api = [u for u in fn.requests if "/wp-json/" in u]
        self.assertEqual(len(api), 1)
        self.assertIn("order=desc", api[0])
        self.assertIn("per_page=50", api[0])

    def test_a_limit_above_one_page_pages_only_as_far_as_needed(self):
        fn = FakeWordPress(self._posts())
        run_sync(fn, self.dbfile, spec=_wp_spec(150))
        self.assertEqual(len(self.urls()), 150)
        self.assertEqual(len([u for u in fn.requests if "/wp-json/" in u]), 2)

    def test_all_reaches_the_whole_history(self):
        fn = FakeWordPress(self._posts(120))
        run_sync(fn, self.dbfile, spec=_wp_spec(None))
        self.assertEqual(len(self.urls()), 120)

    def test_a_routine_update_adds_every_new_post_even_beyond_n(self):
        posts = self._posts(100)
        fn = FakeWordPress(posts)
        run_sync(fn, self.dbfile, spec=_wp_spec(5))
        fn.posts = posts + [(_day(200 + i), f"fresh-{i}", f"Fresh {i}")
                            for i in range(8)]
        fn.requests.clear()
        prog = run_sync(fn, self.dbfile, spec=_wp_spec(5), newest_only=True)
        self.assertEqual(prog.new, 8)
        self.assertEqual(len(self.urls()), 13)
        self.assertEqual(len([u for u in fn.requests if "/wp-json/" in u]), 1)

    def test_lowering_the_limit_deletes_nothing(self):
        fn = FakeWordPress(self._posts(120))
        run_sync(fn, self.dbfile, spec=_wp_spec(None))
        self.set_limit(10)
        run_sync(fn, self.dbfile, spec=_wp_spec(10))
        self.assertEqual(len(self.urls()), 120)

    def test_raising_the_limit_then_scanning_reaches_the_newer_missing_posts(self):
        fn = FakeWordPress(self._posts(300))
        run_sync(fn, self.dbfile, spec=_wp_spec(50))
        self.set_limit(200)
        prog = run_sync(fn, self.dbfile, spec=_wp_spec(200))
        self.assertEqual(self.slugs(),
                         sorted(f"post-{i}" for i in range(100, 300)))
        self.assertEqual(prog.fetched, 150)

    def test_all_after_a_limit_reaches_everything(self):
        fn = FakeWordPress(self._posts(130))
        run_sync(fn, self.dbfile, spec=_wp_spec(20))
        self.set_limit(None)
        run_sync(fn, self.dbfile, spec=_wp_spec(None))
        self.assertEqual(len(self.urls()), 130)


class TestGenericLimit(LimitHarness):
    def test_a_long_feed_probes_only_the_newest_n(self):
        fn = _feed_site(247)
        run_sync(fn, self.dbfile, spec=_generic_spec(50))
        self.assertEqual(self.slugs(),
                         sorted(f"post-{i}" for i in range(197, 247)))
        self.assertEqual(fn.count("/post-"), 50)
        # The feed was not paged further than N items needed.
        self.assertEqual(fn.count("/feed/"), 1)

    def test_a_sitemap_is_not_read_once_the_feed_covers_n(self):
        fn = _feed_site(120)
        fn.add(f"{BASE}/robots.txt", f"Sitemap: {BASE}/sitemap.xml\n")
        fn.add(f"{BASE}/sitemap.xml", "<urlset></urlset>")
        run_sync(fn, self.dbfile, spec=_generic_spec(50))
        self.assertEqual(fn.count("sitemap"), 0)

    def test_a_full_scan_keeps_the_cap(self):
        fn = _feed_site(247)
        run_sync(fn, self.dbfile, spec=_generic_spec(50))
        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=_generic_spec(50))
        self.assertEqual(fn.count("/post-"), 0)
        self.assertEqual(len(self.urls()), 50)

    def test_the_pending_drain_stays_capped(self):
        fn = _feed_site(30)
        conn = self.conn()
        db.init(conn)
        sid = db.add_source(conn, "t", "B", "generic", BASE, {"limit": 10})
        # 100 older posts left pending by some earlier, unlimited run.
        for i in range(100):
            u = f"{BASE}/old-{i}/"
            fn.add(u, post_html(f"Old {i}", "2001-01-01T00:00:00+00:00"))
            db.upsert_article(conn, sid, u, url=u, title=f"Old {i}",
                              published_at=f"2001-01-{1 + i % 28:02d}T00:00:00",
                              date_precision="day", date_confidence="exact",
                              date_source="t")
        conn.close()
        run_sync(fn, self.dbfile, spec=_generic_spec(10))
        # The newest 10 of the known set are the feed's; none of the old
        # pending bodies is worth fetching.
        self.assertEqual(fn.count("/old-"), 0)
        self.assertEqual(fn.count("/post-"), 10)
        self.assertEqual(len(self.urls()), 110)    # 10 fetched, nothing removed

    def test_raising_the_limit_then_scanning_fetches_the_gap(self):
        fn = _feed_site(120)
        run_sync(fn, self.dbfile, spec=_generic_spec(20))
        self.set_limit(60)
        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=_generic_spec(60))
        self.assertEqual(fn.count("/post-"), 40)    # only the 40 not held
        self.assertEqual(len(self.urls()), 60)

    def test_lowering_the_limit_deletes_nothing(self):
        fn = _feed_site(60)
        run_sync(fn, self.dbfile, spec=_generic_spec(60))
        self.set_limit(5)
        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=_generic_spec(5))
        self.assertEqual(len(self.urls()), 60)
        self.assertEqual(fn.count("/post-"), 0)

    def test_no_limit_means_the_whole_feed(self):
        fn = _feed_site(120)
        run_sync(fn, self.dbfile, spec=_generic_spec(None))
        self.assertEqual(len(self.urls()), 120)

    def test_a_site_without_a_feed_still_works(self):
        fn = FakeNet()
        urls = []
        for i in range(12):
            u = f"{BASE}/p-{i}/"
            fn.add(u, post_html(f"P {i}", _day(i) + "+00:00"))
            urls.append(u)
        fn.add(f"{BASE}/robots.txt", f"Sitemap: {BASE}/sitemap.xml\n")
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml(urls))
        fn.add(BASE + "/", "<html><head><title>B</title></head></html>")
        run_sync(fn, self.dbfile, spec=_generic_spec(5))
        # Nothing orders an undated pool, so which five is arbitrary, but
        # the cap holds.
        self.assertEqual(len(self.urls()), 5)

    def test_the_cap_is_not_applied_to_a_routine_update(self):
        fn = _feed_site(40)
        run_sync(fn, self.dbfile, spec=_generic_spec(5))
        fn2 = _feed_site(70, start=35)     # 30 new posts appear
        prog = run_sync(fn2, self.dbfile, spec=_generic_spec(5),
                        newest_only=True)
        self.assertEqual(prog.new, 30)
        self.assertEqual(len(self.urls()), 35)


class TestSafetyNet(LimitHarness):
    """A source that ignores ctx.limit is still held to it by the Syncer."""

    def test_stubs_beyond_n_are_dropped(self):
        from chronicle import dates
        from chronicle.sources.base import Stub

        def stub(i, dated=True):
            d = (dates.parse_iso(_day(i), confidence="exact", source="t")
                 if dated else dates.UNKNOWN)
            return Stub(guid=f"g{i}", url=f"{BASE}/g{i}/", title=f"T{i}",
                        date=d, source_order=i)

        stubs = [stub(i) for i in range(10)] + [stub(50, dated=False)]
        kept = sync.newest_stubs(stubs, 4, known={})
        self.assertEqual({s.guid for s in kept}, {"g9", "g8", "g7", "g6"})
        # Known stubs are never dropped, but do count toward N.
        kept = sync.newest_stubs(stubs, 4, known={"g0": (True, "ok", 4)})
        self.assertEqual({s.guid for s in kept},
                         {"g9", "g8", "g7", "g6", "g0"})
        # Undated stubs rank after dated ones, and fill what is left.
        kept = sync.newest_stubs(stubs[:2] + [stub(50, dated=False)], 5, known={})
        self.assertEqual(len(kept), 3)
        kept = sync.newest_stubs(stubs[:3] + [stub(50, dated=False)], 3, known={})
        self.assertNotIn("g50", {s.guid for s in kept})


class TestLimitConfig(LimitHarness):
    def test_config_limit(self):
        self.assertIsNone(Context().limit)
        self.assertEqual(sources.config_limit({"limit": 50}), 50)
        self.assertIsNone(sources.config_limit({}))
        self.assertIsNone(sources.config_limit({"limit": None}))
        self.assertIsNone(sources.config_limit({"limit": "junk"}))

    def test_books_ignore_the_limit(self):
        seen = {}
        real = sync.Context

        def spy(**kw):
            ctx = real(**kw)
            seen["limit"] = ctx.limit
            return ctx

        conn = self.conn()
        db.init(conn)
        db.add_source(conn, "book", "Book", "epub", "/nonexistent.epub",
                      {"limit": 3})
        conn.close()
        with mock.patch.object(db, "connect", lambda path=None: self.conn()), \
                mock.patch.object(sync, "Context", spy):
            sync.Syncer().sync_all(cache_images=False)
        self.assertIn("limit", seen)
        self.assertIsNone(seen["limit"])

    def test_a_normal_source_gets_its_limit(self):
        fn = _feed_site(3)
        seen = {}
        real = sync.Context

        def spy(**kw):
            ctx = real(**kw)
            seen["limit"] = ctx.limit
            return ctx

        with mock.patch.object(sync, "Context", spy):
            run_sync(fn, self.dbfile, spec=_generic_spec(2))
        self.assertEqual(seen["limit"], 2)

    def test_redetect_keeps_the_limit_and_the_recipe_config(self):
        conn = self.conn()
        db.init(conn)
        sid = db.add_source(conn, "x", "X", "wayback-gone", BASE,
                            {"limit": 25, "path_prefix": "/blog"})
        row = db.get_source(conn, sid)
        spec = {"plugin": "generic", "homepage": BASE, "detected": "feed",
                "config": {"feed": f"{BASE}/feed/"}}
        with mock.patch.object(sources, "detect", return_value=spec):
            _row, config = sync.Syncer()._redetect(
                conn, row, json.loads(row["config"]), sync.Progress())
        self.assertEqual(config["limit"], 25)
        self.assertEqual(config["path_prefix"], "/blog")
        self.assertEqual(config["feed"], f"{BASE}/feed/")
        stored = json.loads(db.get_source(conn, sid)["config"])
        self.assertEqual(stored["limit"], 25)
        conn.close()

    def test_parse_limit(self):
        self.assertEqual(sources.parse_limit("50"), 50)
        self.assertIsNone(sources.parse_limit("all"))
        self.assertIsNone(sources.parse_limit("All"))
        for bad in ("0", "-3", "many", ""):
            with self.assertRaises(ValueError):
                sources.parse_limit(bad)


class TestLimitCli(LimitHarness):
    SPEC = {"name": "Fake", "plugin": "generic", "homepage": "https://f.example",
            "detected": "a feed", "config": {}}

    def run_cli(self, *argv):
        from chronicle import cli
        out, err = io.StringIO(), io.StringIO()
        conn = self.conn()
        db.init(conn)
        with mock.patch.object(db, "get_conn", return_value=conn), \
                mock.patch.object(sources, "detect",
                                  return_value=dict(self.SPEC)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as exc:          # argparse rejecting a value
                rc = exc.code
        conn.close()
        return rc or 0, out.getvalue() + err.getvalue()

    def limit_of(self, slug="f-example"):
        conn = self.conn()
        row = conn.execute("SELECT config FROM sources WHERE slug=?",
                           (slug,)).fetchone()
        conn.close()
        return json.loads(row["config"]).get("limit", "absent")

    def test_add_defaults_to_fifty(self):
        self.assertEqual(self.run_cli("add", "https://f.example")[0], 0)
        self.assertEqual(self.limit_of(), 50)

    def test_add_with_a_number_and_with_all(self):
        self.run_cli("add", "https://f.example", "--limit", "7")
        self.assertEqual(self.limit_of(), 7)
        self.run_cli("add", "https://g.example", "--slug", "g", "--limit", "all")
        self.assertEqual(self.limit_of("g"), "absent")

    def test_set_limit_round_trips(self):
        self.run_cli("add", "https://f.example")
        self.assertEqual(self.run_cli("set-limit", "f-example", "200")[0], 0)
        self.assertEqual(self.limit_of(), 200)
        self.assertEqual(self.run_cli("set-limit", "f-example", "all")[0], 0)
        self.assertEqual(self.limit_of(), "absent")

    def test_bad_values_and_unknown_slugs_are_refused(self):
        self.run_cli("add", "https://f.example")
        self.assertNotEqual(self.run_cli("set-limit", "f-example", "zero")[0], 0)
        self.assertEqual(self.limit_of(), 50)
        self.assertNotEqual(self.run_cli("set-limit", "nope", "5")[0], 0)
        self.assertNotEqual(self.run_cli("add", "https://f.example", "--slug",
                                         "h", "--limit", "-1")[0], 0)

    def test_books_have_no_limit(self):
        conn = self.conn()
        db.init(conn)
        db.add_source(conn, "bk", "Bk", "epub", "/x.epub", {})
        conn.close()
        self.assertNotEqual(self.run_cli("set-limit", "bk", "5")[0], 0)
        self.assertEqual(self.limit_of("bk"), "absent")
