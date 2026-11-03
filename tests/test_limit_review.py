"""Regressions found in review of the last-N-posts limit and path detection."""
from __future__ import annotations

import json
import logging
from unittest import mock

from chronicle import db, sources, sync
from chronicle.sources import detect, discovery

from fakesite import FakeNet, listing_html, post_html, rss_xml
from test_discovery import BASE, SyncHarness, run_sync
from test_limit import (LimitHarness, _day, _feed_site, _generic_spec, _rfc)
from test_detect_paths import TestWorksInProgress, WIP


class TestIncrementalKeepsCap(LimitHarness):
    def test_fetch_new_posts_does_not_pull_in_the_old_feed_tail(self):
        fn = _feed_site(247, per_page=247)
        run_sync(fn, self.dbfile, spec=_generic_spec(50))
        self.assertEqual(len(self.urls()), 50)
        fn.requests.clear()
        prog = run_sync(fn, self.dbfile, spec=_generic_spec(50), newest_only=True)
        self.assertEqual(prog.new, 0)
        self.assertEqual(len(self.urls()), 50)
        self.assertEqual(fn.count("/post-"), 0)

    def test_a_paged_feed_is_not_walked_back_through_its_history(self):
        fn = _feed_site(247)
        run_sync(fn, self.dbfile, spec=_generic_spec(50))
        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=_generic_spec(50), newest_only=True)
        self.assertLessEqual(fn.count("/feed/"), 2)
        self.assertEqual(len(self.urls()), 50)

    def test_genuinely_new_posts_are_still_added(self):
        fn = _feed_site(100, per_page=200)
        run_sync(fn, self.dbfile, spec=_generic_spec(10))
        fn2 = _feed_site(130, per_page=200)
        prog = run_sync(fn2, self.dbfile, spec=_generic_spec(10),
                        newest_only=True)
        self.assertEqual(prog.new, 30)
        self.assertEqual(len(self.urls()), 40)
        self.assertEqual(fn2.count("/post-"), 30)


class TestScopeAwareFeedCount(LimitHarness):
    def test_out_of_scope_feed_items_do_not_satisfy_the_limit(self):
        fn = FakeNet()
        items = []
        for i in reversed(range(30)):
            sect = "essays" if i < 6 else "other"
            u = f"{BASE}/{sect}/p-{i}/"
            fn.add(u, post_html(f"P {i}", _day(i) + "+00:00"))
            items.append({"link": u, "title": f"P {i}", "date": _rfc(i),
                          "content": False})
        fn.add(f"{BASE}/feed/", rss_xml(BASE, items))
        fn.add(BASE + "/", "<html><head><title>B</title>"
               '<link rel="alternate" type="application/rss+xml" href="/feed/">'
               "</head></html>")
        spec = {"name": "B", "plugin": "generic", "homepage": BASE,
                "config": {"limit": 5, "path_prefix": "/essays",
                           "index": "/essays/"}}
        run_sync(fn, self.dbfile, spec=spec)
        self.assertEqual(len(self.urls()), 5)
        self.assertTrue(all("/essays/" in r["url"] for r in self.urls()))


class TestLimitBounds(LimitHarness):
    def test_absurd_limits_are_refused_or_ignored(self):
        with self.assertRaises(ValueError):
            sources.parse_limit(str(10 ** 20))
        self.assertIsNone(sources.config_limit({"limit": 10 ** 20}))
        self.assertEqual(sources.parse_limit(str(sources.MAX_LIMIT)),
                         sources.MAX_LIMIT)

    def test_a_huge_stored_limit_does_not_abort_the_sync(self):
        fn = _feed_site(3)
        conn = self.conn()
        db.init(conn)
        db.add_source(conn, "t", "B", "generic", BASE, {"limit": 10 ** 20})
        conn.close()
        prog = run_sync(fn, self.dbfile, spec=_generic_spec(None))
        self.assertEqual(prog.error, "")
        self.assertEqual(len(self.urls()), 3)


class TestConfigRace(LimitHarness):
    def test_a_limit_set_during_a_sync_survives_the_config_write(self):
        conn = self.conn()
        db.init(conn)
        sid = db.add_source(conn, "t", "B", "generic", BASE, {"limit": 5})
        stale = {"limit": 5}
        db.set_source_limit(conn, sid, 80)
        db.merge_source_config(conn, sid, {"feed": "/x"})
        cfg = json.loads(db.get_source(conn, sid)["config"])
        self.assertEqual(cfg, {"limit": 80, "feed": "/x"})
        conn.close()


class TestStreaming(LimitHarness):
    def _stubs(self, n):
        from chronicle import dates
        from chronicle.sources.base import Stub
        for i in range(n):
            d = dates.parse_iso(_day(i), confidence="exact", source="t")
            yield Stub(guid=f"g{i}", url=f"{BASE}/g{i}/", title="t",
                       date=d, source_order=i)

    def test_an_adapter_that_honours_the_limit_is_streamed_not_buffered(self):
        stream = sync.honour_limit(self._stubs(6), 3, {}, honours=True)
        self.assertEqual(next(iter(stream)).guid, "g0")   # lazily, not ranked
        self.assertEqual(len(list(sync.honour_limit(self._stubs(6), 3, {}, True))), 6)

    def test_other_adapters_are_held_to_the_limit(self):
        got = list(sync.honour_limit(self._stubs(6), 3, {}, honours=False))
        self.assertEqual({s.guid for s in got}, {"g5", "g4", "g3"})


class TestWipTwice(TestWorksInProgress):
    def test_second_sync_requests_no_junk(self):
        fn = self.build()
        run_sync(fn, self.dbfile, url=WIP + "/archive/")
        fn.requests.clear()
        run_sync(fn, self.dbfile, url=WIP + "/archive/")
        for j in ("/issue-05/", "/print/", "/our-authors/", "/nop/",
                  "/notes-on-progress/", "/special-issue/"):
            self.assertEqual(fn.count(WIP + j), 0, j)
        self.assertEqual(len(self.urls()), 3)

    def test_sentence_case_slug_titles_survive(self):
        fn = self.build()
        # The feed gives a real sentence-case title that equals its slug.
        fn.add(WIP + "/rss.xml", rss_xml(WIP, [
            {"link": f"{WIP}/issue/alpha/", "title": "Alpha",
             "date": "Mon, 26 Oct 2026 11:23:34 GMT", "content": False}]))
        page = fn.pages[f"{WIP}/issue/alpha/"][1].decode()
        page = page.replace("<h1>alpha</h1>", "<h1>alpha</h1><h2>A subheading here</h2>")
        fn.add(f"{WIP}/issue/alpha/", page)
        run_sync(fn, self.dbfile, url=WIP + "/archive/")
        titles = {r["url"]: r["title"] for r in self.urls()}
        self.assertEqual(titles[f"{WIP}/issue/alpha/"], "Alpha")


class RestRouteNet(FakeNet):
    """WordPress answers ?rest_route= on ANY path, as the real thing does."""

    def fetch(self, url, **kw):
        if "rest_route=/wp/v2/posts" in url:
            self.requests.append(url)
            from chronicle import net as _net
            return _net.Response(url=url, status=200,
                                 headers={"x-wp-total": "9000"},
                                 body=b'[{"id": 1}]')
        return super().fetch(url, **kw)


class TestApiTakeover(SyncHarness):
    def _wp(self, fn):
        fn.add(f"{BASE}/wp-json/wp/v2/posts?per_page=1", '[{"id": 1}]',
               headers={"x-wp-total": "9000"})

    def test_category_page_stays_a_scoped_generic_source(self):
        fn = RestRouteNet()
        fn.add(BASE + "/", "<html><head><title>News Site</title></head></html>")
        fn.add(f"{BASE}/category/essays/",
               "<html><title>Essays | News Site</title></html>")
        self._wp(fn)
        with fn.patched():
            spec = detect(BASE + "/category/essays/")
        self.assertEqual(spec["plugin"], "generic")
        self.assertEqual(spec["config"]["path_prefix"], "/category/essays")

    def test_multisite_subsite_uses_its_own_api(self):
        fn = RestRouteNet()
        fn.add(BASE + "/", "<html><head><title>Big University</title></head></html>")
        fn.add(f"{BASE}/labs/robotics/", "<html><title>Robotics Lab</title></html>")
        self._wp(fn)
        fn.add(f"{BASE}/labs/robotics/wp-json/",
               json.dumps({"name": "Robotics", "home": f"{BASE}/labs/robotics"}))
        fn.add(f"{BASE}/labs/robotics/wp-json/wp/v2/posts?per_page=1",
               '[{"id": 2}]', headers={"x-wp-total": "12"})
        with fn.patched():
            spec = detect(BASE + "/labs/robotics/")
        self.assertEqual(spec["plugin"], "wordpress")
        self.assertEqual(spec["config"]["api_root"],
                         f"{BASE}/labs/robotics/wp-json/wp/v2")

    def test_a_subpath_api_that_describes_the_root_is_not_a_subsite(self):
        fn = RestRouteNet()
        fn.add(BASE + "/", "<html><head><title>Big University</title></head></html>")
        fn.add(f"{BASE}/labs/robotics/", "<html><title>Robotics Lab</title></html>")
        self._wp(fn)
        fn.add(f"{BASE}/labs/robotics/wp-json/", json.dumps({"home": BASE}))
        fn.add(f"{BASE}/labs/robotics/wp-json/wp/v2/posts?per_page=1",
               '[{"id": 2}]')
        with fn.patched():
            spec = detect(BASE + "/labs/robotics/")
        self.assertEqual(spec["plugin"], "generic")
        self.assertEqual(spec["config"]["path_prefix"], "/labs/robotics")

    def test_root_api_without_subsite_api_keeps_generic_section(self):
        fn = RestRouteNet()
        fn.add(BASE + "/", "<html><head><title>Big University</title></head></html>")
        fn.add(f"{BASE}/labs/robotics/", "<html><title>Robotics Lab</title></html>")
        self._wp(fn)
        with fn.patched():
            spec = detect(BASE + "/labs/robotics/")
        self.assertEqual(spec["plugin"], "generic")
        self.assertEqual(spec["config"]["path_prefix"], "/labs/robotics")

    def test_blog_listing_path_still_takes_the_api(self):
        fn = RestRouteNet()
        fn.add(BASE + "/", "<html><head><title>FS</title></head></html>")
        self._wp(fn)
        with fn.patched():
            spec = detect(BASE + "/blog/")
        self.assertEqual(spec["plugin"], "wordpress")

    def test_ghost_section_stays_generic(self):
        fn = FakeNet()
        key = "a" * 26
        fn.add(BASE + "/", f'<html><head><title>G</title></head><body>ghost '
               f'<script>key: "{key}"</script></body></html>')
        fn.add(f"{BASE}/tag/essays/", "<html><title>Essays</title></html>")
        with fn.patched():
            spec = detect(BASE + "/tag/essays/")
        self.assertEqual(spec["plugin"], "generic")

    def test_rest_route_api_root_builds_a_single_query_string(self):
        from chronicle.sources.wordpress import WordPressSource
        src = WordPressSource({"name": "x", "homepage": BASE, "slug": "x",
                               "plugin": "wordpress", "id": 1},
                              {"api_root": f"{BASE}/?rest_route=/wp/v2"})
        url = src.posts_url(per_page=50, page=1)
        self.assertEqual(url.count("?"), 1, url)
        self.assertIn("rest_route=/wp/v2/posts", url)

    def test_a_failing_api_is_reported_not_called_up_to_date(self):
        fn = FakeNet()      # the configured API 404s
        spec = {"name": "W", "plugin": "wordpress", "homepage": BASE,
                "config": {"api_root": f"{BASE}/gone/?rest_route=/wp/v2"}}
        prog = run_sync(fn, self.dbfile, spec=spec)
        self.assertNotIn("up to date", prog.message)
        self.assertTrue(prog.warnings)
