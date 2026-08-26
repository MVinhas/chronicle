"""Tests for the generic discovery engine, run against synthetic sites.

Each test builds a small fake site exercising one architecture — feed-only,
sitemap index, archive pagination, overlapping routes, redirects — and proves
a property of the engine (completeness, deduplication, request economy)
rather than the behaviour of one real website.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from chronicle import db, net, sync
from chronicle.sources import detect, discovery
from chronicle.sources.generic import GenericSource
from chronicle.sources.base import Context

from fakesite import (FakeNet, listing_html, post_html, rss_xml,
                      sitemap_index_xml, sitemap_xml)

BASE = "https://blog.example"

_REAL_CONNECT = db.connect


def run_sync(fn: FakeNet, dbfile: str, spec: dict | None = None,
             url: str = BASE) -> sync.Progress:
    """detect() + add + full sync against the fake net, on a shared temp DB."""
    with fn.patched():
        if spec is None:
            spec = detect(url)
        conn = _REAL_CONNECT(dbfile)
        db.init(conn)
        db.add_source(conn, "t", spec["name"], spec["plugin"],
                      spec["homepage"], spec.get("config") or {})
        conn.close()
        with mock.patch.object(db, "connect",
                               lambda path=None: _REAL_CONNECT(dbfile)):
            return sync.Syncer().sync_all(cache_images=False)


class SyncHarness(unittest.TestCase):
    """Shared plumbing: temp library + patched connections + zero rate limit."""

    def setUp(self):
        net.set_rate(0.0)
        fd, self.dbfile = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.dbfile)

    def conn(self):
        return _REAL_CONNECT(self.dbfile)

    def urls(self):
        c = self.conn()
        rows = c.execute("SELECT url, published_at, content_status, title "
                         "FROM articles ORDER BY url").fetchall()
        c.close()
        return rows


def _add_posts(fn: FakeNet, n: int = 8, **kw) -> list[str]:
    urls = []
    for i in range(n):
        u = f"{BASE}/2019/{1 + i % 12:02d}/post-{i}/"
        fn.add(u, post_html(f"Post {i}", f"2019-{1 + i % 12:02d}-05T00:00:00+00:00",
                            **kw))
        urls.append(u)
    return urls


class TestSitemapOnly(SyncHarness):
    def test_posts_found_noise_rejected(self):
        fn = FakeNet()
        posts = _add_posts(fn, 8)
        fn.add(f"{BASE}/about-me/", post_html("About", None, og_type="website"))
        fn.add(f"{BASE}/sitemap.xml",
               sitemap_xml(posts + [f"{BASE}/about-me/", f"{BASE}/tag/x/",
                                    f"{BASE}/style.css", BASE + "/"]))
        fn.add(BASE + "/", "<html><title>B</title><body>hi</body></html>")

        prog = run_sync(fn, self.dbfile)
        rows = self.urls()
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r["published_at"] for r in rows))
        self.assertTrue(all(r["content_status"] == "ok" for r in rows))
        # Assets and taxonomy URLs were never even requested.
        self.assertEqual(fn.count("style.css"), 0)
        self.assertEqual(fn.count("/tag/"), 0)
        self.assertEqual(prog.failed, 0)

    def test_incremental_sync_is_nearly_free(self):
        fn = FakeNet()
        posts = _add_posts(fn, 8)
        fn.add(f"{BASE}/about-me/", post_html("About", None, og_type="website"))
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml(posts + [f"{BASE}/about-me/"]))
        fn.add(BASE + "/", "<html><title>B</title><body>hi</body></html>")

        run_sync(fn, self.dbfile)
        fn.requests.clear()
        run_sync(fn, self.dbfile, spec={"name": "B", "plugin": "generic",
                                        "homepage": BASE, "config": {}})
        # No per-article fetches: only the cheap enumerators run again —
        # and the rejected about-me page is not re-examined either.
        self.assertEqual(fn.count("/post-"), 0)
        self.assertEqual(fn.count("about-me"), 0)
        self.assertLess(len(fn.requests), 12)

    def test_new_post_found_incrementally(self):
        fn = FakeNet()
        posts = _add_posts(fn, 5)
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml(posts))
        fn.add(BASE + "/", "<html><title>B</title></html>")
        run_sync(fn, self.dbfile)

        new = f"{BASE}/2020/01/brand-new/"
        fn.add(new, post_html("Brand New", "2020-01-15T00:00:00+00:00"))
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml(posts + [new]))
        prog = run_sync(fn, self.dbfile, spec={"name": "B", "plugin": "generic",
                                               "homepage": BASE, "config": {}})
        self.assertEqual(prog.new, 1)
        self.assertEqual(len(self.urls()), 6)


class TestFeedOnly(SyncHarness):
    def test_feed_items_need_no_page_fetches(self):
        fn = FakeNet()
        items = []
        for i in range(5):
            u = f"{BASE}/post-{i}/"
            items.append({"link": u, "title": f"Post {i}",
                          "date": f"Mon, 0{i + 1} Jan 2018 08:00:00 +0000"})
        fn.add(f"{BASE}/feed/", rss_xml(BASE, items))
        fn.add(BASE + "/", "<html><head><title>B</title>"
               '<link rel="alternate" type="application/rss+xml" href="/feed/">'
               "</head><body>hi</body></html>")

        prog = run_sync(fn, self.dbfile)
        rows = self.urls()
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["published_at"] for r in rows))
        # The bodies came from the feed, so no article page was ever fetched.
        self.assertEqual(fn.count("/post-"), 0)
        self.assertEqual(prog.fetched, 5)

    def test_undated_feed_item_is_kept(self):
        """The old engine silently dropped posts with no date signal."""
        fn = FakeNet()
        fn.add(f"{BASE}/feed/", rss_xml(BASE, [
            {"link": f"{BASE}/mystery/", "title": "Mystery", "date": None}]))
        fn.add(f"{BASE}/mystery/", post_html("Mystery", None, og_type=None))
        fn.add(BASE + "/", "<html><head><title>B</title>"
               '<link rel="alternate" type="application/rss+xml" href="/feed/">'
               "</head></html>")

        run_sync(fn, self.dbfile)
        rows = self.urls()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["published_at"])
        self.assertEqual(rows[0]["content_status"], "ok")

    def test_feed_pagination_wordpress_style(self):
        fn = FakeNet()
        page1, page2 = [], []
        for i in range(10):
            u = f"{BASE}/new-{i}/"
            fn.add(u, post_html(f"New {i}", "2020-05-05T00:00:00+00:00"))
            page1.append({"link": u, "title": f"New {i}",
                          "date": "Tue, 05 May 2020 08:00:00 +0000"})
        for i in range(4):
            u = f"{BASE}/old-{i}/"
            fn.add(u, post_html(f"Old {i}", "2015-03-03T00:00:00+00:00"))
            page2.append({"link": u, "title": f"Old {i}",
                          "date": "Tue, 03 Mar 2015 08:00:00 +0000"})
        fn.add(f"{BASE}/feed/", rss_xml(BASE, page1))
        fn.add(f"{BASE}/feed/?paged=2", rss_xml(BASE, page2))
        fn.add(f"{BASE}/feed/?paged=3", rss_xml(BASE, page2))  # repeats: stop
        fn.add(BASE + "/", "<html><head><title>B</title>"
               '<link rel="alternate" type="application/rss+xml" href="/feed/">'
               "</head></html>")

        run_sync(fn, self.dbfile)
        self.assertEqual(len(self.urls()), 14)


class TestSitemapIndex(SyncHarness):
    def test_numbered_children_are_followed(self):
        """sitemap-1.xml / sitemap-2.xml children used to be skipped."""
        fn = FakeNet()
        posts = _add_posts(fn, 6)
        fn.add(f"{BASE}/sitemap.xml", sitemap_index_xml(
            [f"{BASE}/sitemap-1.xml", f"{BASE}/sitemap-2.xml",
             f"{BASE}/sitemap-3.xml", f"{BASE}/sitemap-4.xml"]))
        fn.add(f"{BASE}/sitemap-1.xml", sitemap_xml(posts[:3]))
        fn.add(f"{BASE}/sitemap-2.xml", sitemap_xml(posts[3:]))
        fn.add(f"{BASE}/sitemap-3.xml", sitemap_xml([]))
        fn.add(f"{BASE}/sitemap-4.xml", sitemap_xml([]))
        fn.add(BASE + "/", "<html><title>B</title></html>")

        run_sync(fn, self.dbfile)
        self.assertEqual(len(self.urls()), 6)

    def test_taxonomy_children_are_skipped(self):
        fn = FakeNet()
        posts = _add_posts(fn, 4)
        fn.add(f"{BASE}/sitemap.xml", sitemap_index_xml(
            [f"{BASE}/post-sitemap.xml", f"{BASE}/category-sitemap.xml",
             f"{BASE}/tag-sitemap.xml", f"{BASE}/author-sitemap.xml"]))
        fn.add(f"{BASE}/post-sitemap.xml", sitemap_xml(posts))
        fn.add(BASE + "/", "<html><title>B</title></html>")

        run_sync(fn, self.dbfile)
        self.assertEqual(len(self.urls()), 4)
        self.assertEqual(fn.count("category-sitemap"), 0)
        self.assertEqual(fn.count("tag-sitemap"), 0)


class TestArchivePages(SyncHarness):
    def test_numbered_pagination(self):
        fn = FakeNet()
        posts = _add_posts(fn, 6)
        fn.add(f"{BASE}/blog", listing_html("Blog", posts[:3]))
        fn.add(f"{BASE}/blog/page/2/", listing_html("Blog p2", posts[3:]))
        fn.add(BASE + "/", "<html><title>B</title></html>")

        run_sync(fn, self.dbfile)
        self.assertEqual(len(self.urls()), 6)

    def test_rel_next_pagination(self):
        fn = FakeNet()
        posts = _add_posts(fn, 6)
        fn.add(f"{BASE}/archive", listing_html("A", posts[:3]))
        # No /page/2/, only a rel=next chain via ?paged= fallback:
        fn.add(f"{BASE}/archive?paged=2", listing_html("A2", posts[3:]))
        fn.add(BASE + "/", "<html><title>B</title></html>")

        run_sync(fn, self.dbfile)
        self.assertEqual(len(self.urls()), 6)

    def test_undated_essay_reachable_only_from_archive(self):
        """An archive-listed page with real prose but no date metadata is
        kept as an undated article instead of being dropped."""
        fn = FakeNet()
        fn.add(f"{BASE}/essays", listing_html("Essays", [
            f"{BASE}/one-essay/", f"{BASE}/two-essay/", f"{BASE}/three-essay/"]))
        for slug in ("one-essay", "two-essay", "three-essay"):
            fn.add(f"{BASE}/{slug}/",
                   post_html(slug.title(), None, og_type=None, paragraphs=4))
        fn.add(BASE + "/", "<html><title>B</title></html>")

        run_sync(fn, self.dbfile)
        rows = self.urls()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["published_at"] is None for r in rows))
        self.assertTrue(all(r["content_status"] == "ok" for r in rows))


class TestMergedRoutes(SyncHarness):
    def test_overlapping_routes_merge_to_one_article_each(self):
        fn = FakeNet()
        posts = _add_posts(fn, 10)
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml(posts))
        feed = [{"link": posts[-1 - i], "title": f"Post {9 - i}",
                 "date": "Mon, 05 Aug 2019 08:00:00 +0000"} for i in range(3)]
        fn.add(f"{BASE}/feed/", rss_xml(BASE, feed))
        fn.add(BASE + "/", "<html><head><title>B</title>"
               '<link rel="alternate" type="application/rss+xml" href="/feed/">'
               "</head></html>")

        run_sync(fn, self.dbfile)
        self.assertEqual(len(self.urls()), 10)
        # The three feed-covered posts needed no page fetch at all.
        probed = [u for u in fn.requests if "/post-" in u]
        self.assertEqual(len(probed), 7)

    def test_redirect_and_query_variant_become_one_article(self):
        fn = FakeNet()
        pretty = f"{BASE}/2020/06/hello-world/"
        fn.add(pretty, post_html("Hello World", "2020-06-01T00:00:00+00:00"))
        fn.redirect(f"{BASE}/hello?p=42", pretty)
        fn.add(f"{BASE}/sitemap.xml",
               sitemap_xml([f"{BASE}/hello?p=42", pretty]))
        fn.add(BASE + "/", "<html><title>B</title></html>")

        run_sync(fn, self.dbfile)
        self.assertEqual(len(self.urls()), 1)

    def test_post_vanished_from_site_but_in_feed_is_kept(self):
        fn = FakeNet()
        gone = f"{BASE}/2018/01/deleted/"    # 404s on the site
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml([gone]))
        fn.add(f"{BASE}/feed/", rss_xml(BASE, [
            {"link": gone, "title": "Deleted",
             "date": "Mon, 01 Jan 2018 08:00:00 +0000", "content": True}]))
        fn.add(BASE + "/", "<html><head><title>B</title>"
               '<link rel="alternate" type="application/rss+xml" href="/feed/">'
               "</head></html>")

        run_sync(fn, self.dbfile)
        rows = self.urls()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content_status"], "ok")

    def test_conflicting_dates_resolve_deterministically(self):
        """Feed and page disagree at equal confidence: the feed's reading,
        merged first, wins — and keeps winning on re-sync."""
        fn = FakeNet()
        u = f"{BASE}/disputed/"
        fn.add(u, post_html("Disputed", None, meta_date=False,
                            jsonld_date="2021-09-09T00:00:00+00:00"))
        fn.add(f"{BASE}/feed/", rss_xml(BASE, [
            {"link": u, "title": "Disputed",
             "date": "Wed, 05 May 2021 08:00:00 +0000", "content": False}]))
        fn.add(BASE + "/", "<html><head><title>B</title>"
               '<link rel="alternate" type="application/rss+xml" href="/feed/">'
               "</head></html>")

        for _ in range(2):
            run_sync(fn, self.dbfile, spec={"name": "B", "plugin": "generic",
                                            "homepage": BASE, "config": {}})
            rows = self.urls()
            self.assertEqual(rows[0]["published_at"], "2021-05-05T08:00:00")


class TestFailureHandling(SyncHarness):
    def test_permanently_gone_page_not_retried(self):
        """A 404 at the origin is recorded as 'gone' and stops costing one
        request per sync — a transient failure would stay retryable."""
        fn = FakeNet()
        lost = f"{BASE}/2019/09/lost/"
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml([lost]))
        fn.add(BASE + "/", "<html><title>B</title></html>")

        spec = {"name": "B", "plugin": "generic", "homepage": BASE, "config": {}}
        # The article is already in the library (from an earlier import whose
        # HTML was never stored), and the page has since been deleted.
        conn = _REAL_CONNECT(self.dbfile)
        db.init(conn)
        sid = db.add_source(conn, "t", "B", "generic", BASE)
        db.upsert_article(conn, sid, net.canonical_url(lost), url=lost,
                          title="Lost", published_at="2019-09-01T00:00:00",
                          date_precision="day", date_confidence="high",
                          date_source="url:permalink")
        conn.close()

        run_sync(fn, self.dbfile, spec=spec)
        c = self.conn()
        row = c.execute("SELECT content_status FROM articles WHERE url=?",
                        (lost,)).fetchone()
        c.close()
        self.assertEqual(row["content_status"], "gone")

        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=spec)
        self.assertEqual(fn.count("/lost"), 0)

    def test_empty_blog_yields_clean_result(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>Empty</title><body>nothing</body></html>")
        prog = run_sync(fn, self.dbfile,
                        spec={"name": "E", "plugin": "generic",
                              "homepage": BASE, "config": {}})
        self.assertEqual(prog.error, "")
        self.assertEqual(len(self.urls()), 0)

    def test_single_post_blog(self):
        fn = FakeNet()
        u = f"{BASE}/2022/02/only/"
        fn.add(u, post_html("Only", "2022-02-02T00:00:00+00:00"))
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml([u]))
        fn.add(BASE + "/", "<html><title>B</title></html>")
        run_sync(fn, self.dbfile)
        self.assertEqual(len(self.urls()), 1)


class TestDetect(unittest.TestCase):
    def setUp(self):
        net.set_rate(0.0)

    def test_wordpress_api(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>WP</title></html>")
        fn.add(f"{BASE}/wp-json/wp/v2/posts?per_page=1", '[{"id": 1}]',
               headers={"x-wp-total": "321"})
        with fn.patched():
            spec = detect(BASE)
        self.assertEqual(spec["plugin"], "wordpress")
        self.assertIn("321", spec["detected"])

    def test_ghost(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>G</title>"
               '<script>ghost apiKey: "0123456789abcdef0123456789"'
               "/ghost/api/</script></html>")
        with fn.patched():
            spec = detect(BASE)
        self.assertEqual(spec["plugin"], "ghost")
        self.assertEqual(spec["config"]["content_key"],
                         "0123456789abcdef0123456789")

    def test_sitemap_via_robots(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>S</title></html>")
        fn.add(f"{BASE}/robots.txt",
               f"User-agent: *\nSitemap: {BASE}/deep/custom-map.xml\n")
        fn.add(f"{BASE}/deep/custom-map.xml", sitemap_xml([f"{BASE}/a/"]))
        with fn.patched():
            spec = detect(BASE)
        self.assertEqual(spec["plugin"], "generic")
        self.assertEqual(spec["config"]["sitemap"], f"{BASE}/deep/custom-map.xml")

    def test_feed_hint_recorded(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><head><title>F</title>"
               '<link rel="alternate" type="application/rss+xml" href="/rss/">'
               "</head></html>")
        with fn.patched():
            spec = detect(BASE)
        self.assertEqual(spec["config"].get("feed"), f"{BASE}/rss/")
        self.assertTrue(spec["partial"])

    def test_dead_site_goes_to_wayback(self):
        fn = FakeNet()   # nothing served, and the origin never answers
        fn.dead = True
        with fn.patched():
            spec = detect(BASE)
        self.assertEqual(spec["config"].get("strategy"), "wayback")

    def test_section_url_scopes_to_section(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>S</title></html>")
        fn.add(f"{BASE}/essays/", "<html><title>Essays</title></html>")
        with fn.patched():
            spec = detect(f"{BASE}/essays/x")
        self.assertEqual(spec["config"].get("path_prefix"), "/essays/x")


class TestClassify(unittest.TestCase):
    def test_assets_and_listings_skipped(self):
        for u in (f"{BASE}/photo.jpg", f"{BASE}/tag/python/",
                  f"{BASE}/category/life/", f"{BASE}/author/jane/",
                  f"{BASE}/2020/", f"{BASE}/2020/05/", f"{BASE}/page/3/",
                  f"{BASE}/wp-content/uploads/x", f"{BASE}/"):
            self.assertEqual(discovery.classify_url(u), "skip", u)

    def test_posts_pass(self):
        for u in (f"{BASE}/2020/05/my-post/", f"{BASE}/my-post/",
                  f"{BASE}/articles/12345", f"{BASE}/archive/2020/05/06/slug"):
            self.assertEqual(discovery.classify_url(u), "maybe", u)

    def test_dated_listing_under_taxonomy_still_skipped(self):
        self.assertEqual(discovery.classify_url(f"{BASE}/tag/2020/05/"), "skip")


class TestWaybackStrategy(SyncHarness):
    def test_snapshots_fetched_once_not_twice(self):
        fn = FakeNet()
        cdx = ("http://web.archive.org/cdx/search/cdx?url=blog.example"
               "&matchType=domain&fl=original,timestamp&collapse=urlkey"
               "&filter=statuscode:200&filter=mimetype:text/html")
        fn.add(cdx, f"{BASE}/2016/04/old-one/ 20200101000000\n"
                    f"{BASE}/2016/05/old-two/ 20200101000000\n")
        for slug, ts in (("2016/04/old-one", "20200101000000"),
                         ("2016/05/old-two", "20200101000000")):
            fn.add(f"https://web.archive.org/web/{ts}id_/{BASE}/{slug}/",
                   post_html(slug, f"{slug[:4]}-{slug[5:7]}-01T00:00:00+00:00"))

        spec = {"name": "W", "plugin": "generic", "homepage": BASE,
                "config": {"strategy": "wayback"}}
        run_sync(fn, self.dbfile, spec=spec)
        rows = self.urls()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["content_status"] == "ok" for r in rows))
        # One request per snapshot: the probe HTML is reused for content.
        self.assertEqual(fn.count("web.archive.org/web/"), 2)


class TestExtractDate(unittest.TestCase):
    """The <time> element rules: theme class names must not matter."""

    def _date(self, body: str, url: str = f"{BASE}/post/"):
        from chronicle import htmlutil
        return discovery.extract_date(
            htmlutil.parse(f"<html><body>{body}</body></html>"), url)

    def test_unhinted_time_with_theme_class_is_used(self):
        d = self._date('<time class="ui-gray right" datetime="2022-12-22">x</time>')
        self.assertEqual(d.iso, "2022-12-22T00:00:00")
        self.assertEqual(d.confidence, "high")

    def test_agreeing_times_are_unambiguous(self):
        d = self._date('<time datetime="2022-12-22">a</time>'
                       '<time class="x" datetime="2022-12-22">b</time>')
        self.assertEqual(d.iso, "2022-12-22T00:00:00")
        self.assertEqual(d.confidence, "high")

    def test_disagreeing_times_take_first_at_medium(self):
        d = self._date('<time datetime="2020-01-01">a</time>'
                       '<time datetime="2023-05-05">b</time>')
        self.assertEqual(d.iso, "2020-01-01T00:00:00")
        self.assertEqual(d.confidence, "medium")

    def test_hinted_time_beats_others(self):
        d = self._date('<time datetime="2023-05-05">b</time>'
                       '<time class="entry-date" datetime="2020-01-01">a</time>')
        self.assertEqual(d.iso, "2020-01-01T00:00:00")
        self.assertEqual(d.confidence, "high")

    def test_times_in_nav_and_updated_are_ignored(self):
        d = self._date('<nav><time datetime="2011-01-01">n</time></nav>'
                       '<time class="updated" datetime="2024-04-04">u</time>'
                       '<time datetime="2019-03-03">real</time>')
        self.assertEqual(d.iso, "2019-03-03T00:00:00")
        self.assertEqual(d.confidence, "high")

    def test_permalink_beats_ambiguous_times(self):
        d = self._date('<time datetime="2020-01-01">a</time>'
                       '<time datetime="2023-05-05">b</time>',
                       url=f"{BASE}/2021/07/post/")
        self.assertEqual(d.iso, "2021-07-01T00:00:00")
        self.assertEqual(d.source, "url:permalink")


class TestContextHelpers(unittest.TestCase):
    def test_settled(self):
        ctx = Context(known={"a": (True, "ok"), "b": (True, "missing"),
                             "c": (False, "ok"), "d": (True, "gone")})
        self.assertTrue(ctx.settled("a"))
        self.assertFalse(ctx.settled("b"))
        self.assertFalse(ctx.settled("c"))
        self.assertFalse(ctx.settled("d"))
        self.assertFalse(ctx.settled("missing"))
        self.assertFalse(Context().settled("a"))
        # 'gone' still blocks a direct refetch, but is not settled.
        self.assertTrue(ctx.no_direct("a"))
        self.assertTrue(ctx.no_direct("d"))
        self.assertFalse(ctx.no_direct("b"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
