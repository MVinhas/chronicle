"""Tests for the generic discovery engine, run against synthetic sites.

Each test builds a small fake site exercising one architecture — feed-only,
sitemap index, archive pagination, overlapping routes, redirects — and proves
a property of the engine (completeness, deduplication, request economy)
rather than the behaviour of one real website.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

from chronicle import db, net, sync
from chronicle.sources import detect, discovery
from chronicle.sources.generic import GenericSource
from chronicle.sources.base import Context

from fakesite import (PARA, FakeNet, listing_html, post_html, rss_xml,
                      sitemap_index_xml, sitemap_xml)

BASE = "https://blog.example"
WPBASE = "https://wp.example"

_REAL_CONNECT = db.connect


def run_sync(fn: FakeNet, dbfile: str, spec: dict | None = None,
             url: str = BASE, newest_only: bool = False) -> sync.Progress:
    """detect() + add + sync against the fake net, on a shared temp DB."""
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
            return sync.Syncer().sync_all(cache_images=False,
                                          newest_only=newest_only)


RESYNC = {"name": "B", "plugin": "generic", "homepage": BASE, "config": {}}


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

    def test_dead_site_is_refused(self):
        from chronicle.sources import DetectError
        fn = FakeNet()   # nothing served, and the origin never answers
        fn.dead = True
        with fn.patched():
            with self.assertRaises(DetectError):
                detect(BASE)

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


def _wp_api(fn: FakeNet, posts: list[dict]) -> None:
    """Serve a WordPress-like REST API for `posts` ({link,title,date,html})."""
    import json as _json
    fn.add(f"{BASE}/wp-json/wp/v2/posts?per_page=1", '[{"id": 1}]',
           headers={"x-wp-total": str(len(posts))})
    body = _json.dumps([
        {"id": i, "link": p["link"], "status": "publish",
         "date_gmt": p["date"], "title": {"rendered": p["title"]},
         "content": {"rendered": p.get("html", f"<p>{p['title']} body text "
                                               "that is long enough to keep. "
                                               * 5 + "</p>")}}
        for i, p in enumerate(posts)])
    from chronicle.sources.wordpress import FIELDS, PAGE_SIZE
    fn.add(f"{BASE}/wp-json/wp/v2/posts?per_page={PAGE_SIZE}&page=1"
           f"&orderby=date&order=asc&_fields={FIELDS}", body,
           headers={"x-wp-total": str(len(posts))})


class TestRouteHealing(SyncHarness):
    def test_legacy_wayback_source_is_redetected(self):
        """A source stuck on the retired Internet Archive strategy gets
        re-detected on sync — fs.blog was live with a working WP API."""
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>Healed</title></html>")
        _wp_api(fn, [{"link": f"{BASE}/first/", "title": "First",
                      "date": "2019-03-03T08:00:00"},
                     {"link": f"{BASE}/second/", "title": "Second",
                      "date": "2020-04-04T08:00:00"}])

        spec = {"name": "H", "plugin": "generic", "homepage": BASE,
                "config": {"strategy": "wayback"}}
        run_sync(fn, self.dbfile, spec=spec)

        c = self.conn()
        src = c.execute("SELECT plugin, config FROM sources").fetchone()
        c.close()
        self.assertEqual(src["plugin"], "wordpress")
        self.assertNotIn("wayback", src["config"])
        rows = self.urls()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["content_status"] == "ok" for r in rows))

    def test_redetect_of_dead_site_fails_cleanly(self):
        fn = FakeNet()
        fn.dead = True
        spec = {"name": "D", "plugin": "generic", "homepage": BASE,
                "config": {"strategy": "wayback"}}
        prog = run_sync(fn, self.dbfile, spec=spec)
        self.assertEqual(prog.error, "")   # the sync run itself survives
        c = self.conn()
        src = c.execute("SELECT last_sync_status FROM sources").fetchone()
        c.close()
        self.assertEqual(src["last_sync_status"], "error")


class TestListingDates(SyncHarness):
    """Dates printed beside links in an archive index (danluu.com's <d> tags)."""

    def _danluu_site(self, fn: FakeNet):
        items = []
        for i, my in enumerate(("08/26", "07/26", "03/25", "11/24", "02/24")):
            u = f"{BASE}/essay-{i}/"
            fn.add(u, post_html(f"Essay {i}", None, og_type=None, paragraphs=4))
            items.append(f"<li><d>{my}</d><a href={u}>Essay {i}</a></li>")
        fn.add(BASE + "/", "<html><title>Minimal</title><body><ul>"
               "<li><d>xx/xx</d><a href=#pt>Patreon posts</a></li>"
               + "".join(items) + "</ul></body></html>")

    def test_mm_yy_listing_dates_apply(self):
        fn = FakeNet()
        self._danluu_site(fn)
        run_sync(fn, self.dbfile, spec={"name": "M", "plugin": "generic",
                                        "homepage": BASE, "config": {}})
        rows = self.urls()
        self.assertEqual(len(rows), 5)
        dated = {r["url"]: r["published_at"] for r in rows}
        self.assertEqual(dated[f"{BASE}/essay-0/"], "2026-08-01T00:00:00")
        self.assertEqual(dated[f"{BASE}/essay-4/"], "2024-02-01T00:00:00")
        c = self.conn()
        conf = c.execute("SELECT DISTINCT date_confidence, date_precision, "
                         "date_source FROM articles").fetchone()
        c.close()
        self.assertEqual(tuple(conf), ("medium", "month", "archive:index"))

    def test_listing_date_heals_wrong_stored_date(self):
        """An already-archived article with a wrong date gets corrected from
        the listing without a single page fetch."""
        fn = FakeNet()
        self._danluu_site(fn)
        spec = {"name": "M", "plugin": "generic", "homepage": BASE, "config": {}}
        conn = _REAL_CONNECT(self.dbfile)
        db.init(conn)
        sid = db.add_source(conn, "t", "M", "generic", BASE)
        guid = net.canonical_url(f"{BASE}/essay-0/")
        aid, _ = db.upsert_article(
            conn, sid, guid, url=f"{BASE}/essay-0/", title="Essay 0",
            published_at="1991-01-01T00:00:00", date_precision="year",
            date_confidence="medium", date_source="text:heading")
        conn.execute("UPDATE articles SET content_status='ok', "
                     "content_html='<p>x</p>' WHERE id=?", (aid,))
        conn.close()

        run_sync(fn, self.dbfile, spec=spec)
        c = self.conn()
        row = c.execute("SELECT published_at, date_source FROM articles "
                        "WHERE guid=?", (guid,)).fetchone()
        c.close()
        self.assertEqual(row["published_at"], "2026-08-01T00:00:00")
        self.assertEqual(row["date_source"], "archive:index")
        self.assertEqual(fn.count("/essay-0/"), 0)   # zero-request healing

    def test_ambiguous_numbers_without_order_stay_unknown(self):
        entries = [(f"u{i}", h) for i, h in
                   enumerate(("3/4", "9/2", "1/8", "5/5"))]
        resolved = discovery.resolve_listing_dates(entries)
        self.assertTrue(all(not d.known for _, d in resolved))

    def test_named_month_and_iso_hints(self):
        entries = [("a", "May 5, 2020 · 4 min"), ("b", "2019-03-07"),
                   ("c", "12 comments"), ("d", "(2018)")]
        resolved = dict(discovery.resolve_listing_dates(entries))
        self.assertEqual(resolved["a"].iso, "2020-05-05T00:00:00")
        self.assertEqual(resolved["b"].iso, "2019-03-07T00:00:00")
        self.assertFalse(resolved["c"].known)
        self.assertEqual(resolved["d"].precision, "year")


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
        ctx = Context(known={"a": (True, "ok", 4), "b": (True, "missing", 2),
                             "c": (False, "ok", 0), "d": (True, "gone", 3)})
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
        self.assertEqual(ctx.known_rank("a"), 4)
        self.assertEqual(ctx.known_rank("c"), -1)   # undated
        self.assertEqual(ctx.known_rank("missing"), -1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestFetchNewPosts(SyncHarness):
    """"Fetch new posts" vs "Full archive scan".

    The contract is about *cost*, not about correctness: the cheap mode may
    look at less of a site, but anything it does find must be recorded exactly
    as the full scan would record it, and it must never lose what is stored.
    """

    def _site(self, n=5):
        fn = FakeNet()
        posts = _add_posts(fn, n)
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml(posts))
        fn.add(BASE + "/", "<html><title>B</title></html>")
        return fn, posts

    def test_skips_the_sitemap_once_there_is_an_archive(self):
        """The completeness route is the expensive one, and cannot stop early."""
        fn, _ = self._site()
        run_sync(fn, self.dbfile)
        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=RESYNC, newest_only=True)
        self.assertEqual(fn.count("sitemap"), 0)

    def test_full_scan_still_reads_the_sitemap(self):
        fn, _ = self._site()
        run_sync(fn, self.dbfile)
        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=RESYNC, newest_only=False)
        self.assertGreater(fn.count("sitemap"), 0)

    def test_an_empty_archive_gets_a_full_scan_regardless(self):
        """There is no "new since" without a "since"."""
        fn, _ = self._site()
        prog = run_sync(fn, self.dbfile, newest_only=True)
        self.assertEqual(len(self.urls()), 5)
        self.assertEqual(prog.new, 5)

    def test_finds_a_new_post_through_the_feed(self):
        """The feed is the route that carries what is new, so it still runs."""
        fn = FakeNet()
        posts = _add_posts(fn, 4)
        fn.add(f"{BASE}/sitemap.xml", sitemap_xml(posts))
        fn.add(BASE + "/", "<html><title>B</title></html>")
        # A feed that exists from the outset: a site proven feedless on one
        # sync is deliberately not re-probed on the next.
        def feed_with(items):
            return rss_xml(BASE, [
                {"title": t, "link": u, "date": d} for t, u, d in items])
        fn.add(f"{BASE}/feed", feed_with(
            [("Post 0", posts[0], "Sat, 05 Jan 2019 00:00:00 +0000")]))
        run_sync(fn, self.dbfile)

        new = f"{BASE}/2021/03/fresh/"
        fn.add(new, post_html("Fresh", "2021-03-02T00:00:00+00:00"))
        fn.add(f"{BASE}/feed", feed_with(
            [("Fresh", new, "Tue, 02 Mar 2021 00:00:00 +0000"),
             ("Post 0", posts[0], "Sat, 05 Jan 2019 00:00:00 +0000")]))
        prog = run_sync(fn, self.dbfile, spec=RESYNC, newest_only=True)
        self.assertEqual(prog.new, 1)
        self.assertIn(new, [r["url"] for r in self.urls()])

    def test_costs_less_than_a_full_scan(self):
        fn, _ = self._site(6)
        run_sync(fn, self.dbfile)

        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=RESYNC, newest_only=True)
        cheap = len(fn.requests)

        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=RESYNC, newest_only=False)
        full = len(fn.requests)

        self.assertLess(cheap, full)

    def test_never_drops_what_is_already_archived(self):
        fn, _ = self._site(6)
        run_sync(fn, self.dbfile)
        before = {r["url"] for r in self.urls()}
        run_sync(fn, self.dbfile, spec=RESYNC, newest_only=True)
        self.assertEqual({r["url"] for r in self.urls()}, before)

    def test_the_mode_is_recorded_on_the_source(self):
        fn, _ = self._site()
        run_sync(fn, self.dbfile)
        run_sync(fn, self.dbfile, spec=RESYNC, newest_only=True)
        c = self.conn()
        msg = c.execute("SELECT last_sync_message m FROM sources").fetchone()["m"]
        c.close()
        self.assertTrue(msg.startswith("new posts:"), msg)


# --------------------------------------------------------------------------
# Routine updates
#
# "Fetch new posts" has to cost about nothing on a built archive. Every source
# below used to re-derive its whole history on every run: gwern.net fetched all
# 669 candidate pages to read one meta tag from each, paulgraham.com re-read all
# 229 essays for their datelines, and the WordPress adapter paginated the entire
# archive from page one. These prove each of them now enumerates only what it
# does not already have.
# --------------------------------------------------------------------------

GWERN = {"name": "Gwern", "plugin": "gwern", "homepage": "https://gwern.net",
         "config": {}}
PG = {"name": "Paul Graham", "plugin": "paulgraham",
      "homepage": "https://paulgraham.com/", "config": {}}


def gwern_page(title, issued):
    """A gwern.net essay: the date lives in a Dublin Core meta tag."""
    meta = f'<meta name="dc.date.issued" content="{issued}">' if issued else ""
    return ("<html><head><title>%s</title>%s</head><body>"
            "<div id=\"markdownBody\"><p>%s</p></div></body></html>"
            % (title, meta, PARA * 3))


class TestGwernUpdates(SyncHarness):
    """gwern.net has no feed and its sitemap carries no <lastmod>, so there is
    no date to enumerate by -- only identity."""

    def _site(self):
        fn = FakeNet()
        urls = []
        for i in range(6):
            u = f"https://gwern.net/essay-{i}"
            fn.add(u, gwern_page(f"Essay {i}", f"201{i}-03-04"))
            urls.append(u)
        # Site furniture: fetched once, has no date, must never be fetched again.
        fn.add("https://gwern.net/about", gwern_page("About", None))
        urls.append("https://gwern.net/about")
        fn.add("https://gwern.net/sitemap.xml", sitemap_xml(urls))
        fn.add("https://gwern.net/index", "<html><title>Index</title></html>")
        return fn, urls

    def test_a_built_archive_is_not_re_probed(self):
        fn, _ = self._site()
        run_sync(fn, self.dbfile, spec=GWERN)
        self.assertEqual(len(self.urls()), 6)

        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=GWERN, newest_only=True)
        # The sitemap and index still get read; nothing else does.
        self.assertEqual(fn.count("/essay-"), 0)
        self.assertEqual(fn.count("/about"), 0)

    def test_an_undated_page_is_judged_once(self):
        """Without a recorded verdict, every sync pays a request to rediscover
        that /about is not an article."""
        fn, _ = self._site()
        run_sync(fn, self.dbfile, spec=GWERN)
        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=GWERN)      # a *full* scan, even
        self.assertEqual(fn.count("/about"), 0)

    def test_a_new_essay_is_still_found(self):
        fn, urls = self._site()
        run_sync(fn, self.dbfile, spec=GWERN)

        fn.add("https://gwern.net/brand-new", gwern_page("Brand New", "2026-05-05"))
        fn.add("https://gwern.net/sitemap.xml",
               sitemap_xml(urls + ["https://gwern.net/brand-new"]))
        fn.requests.clear()
        prog = run_sync(fn, self.dbfile, spec=GWERN, newest_only=True)

        self.assertEqual(prog.new, 1)
        self.assertEqual(len(self.urls()), 7)
        # Exactly one essay was read: the new one.
        self.assertEqual(fn.count("/brand-new"), 1)
        self.assertEqual(fn.count("/essay-"), 0)


class TestPaulGrahamUpdates(SyncHarness):
    def _site(self, n=5):
        fn = FakeNet()
        slugs = [f"essay{i}.html" for i in range(n)]
        links = "".join(f'<a href="{s}">E</a>' for s in slugs)
        fn.add("https://paulgraham.com/articles.html",
               f"<html><body>{links}</body></html>")
        for i, s in enumerate(slugs):
            fn.add(f"https://paulgraham.com/{s}",
                   f"<html><head><title>Essay {i}</title></head><body>"
                   f"<font face=\"verdana\">March 200{i}<br><br>{PARA * 3}"
                   f"</font></body></html>")
        return fn, slugs

    def test_essays_are_read_once(self):
        fn, _ = self._site()
        run_sync(fn, self.dbfile, spec=PG)
        self.assertEqual(len(self.urls()), 5)

        fn.requests.clear()
        run_sync(fn, self.dbfile, spec=PG, newest_only=True)
        # The index is still read; not one essay behind it is.
        self.assertEqual(fn.count("articles.html"), 1)
        self.assertEqual(fn.count("essay"), 0)

    def test_a_new_essay_is_still_read(self):
        fn, slugs = self._site()
        run_sync(fn, self.dbfile, spec=PG)

        links = "".join(f'<a href="{s}">E</a>' for s in slugs + ["fresh.html"])
        fn.add("https://paulgraham.com/articles.html",
               f"<html><body>{links}</body></html>")
        fn.add("https://paulgraham.com/fresh.html",
               "<html><head><title>Fresh</title></head><body>"
               f"<font face=\"verdana\">June 2026<br><br>{PARA * 3}</font>"
               "</body></html>")
        fn.requests.clear()
        prog = run_sync(fn, self.dbfile, spec=PG, newest_only=True)
        self.assertEqual(prog.new, 1)
        self.assertEqual(fn.count("fresh.html"), 1)
        self.assertEqual(fn.count("essay0.html"), 0)


WP = {"name": "WP Blog", "plugin": "wordpress", "homepage": WPBASE,
      "config": {"api_root": WPBASE + "/wp-json/wp/v2"}}


class FakeWordPress(FakeNet):
    """A REST origin that honours per_page/page/after.

    Answering the query rather than a canned URL is the point: it proves the
    adapter narrowed what it asked for, not merely that some string appeared
    in a URL.
    """

    def __init__(self, posts):
        super().__init__()
        self.posts = posts                    # [(iso_date, slug, title)]

    def fetch(self, url, **kw):
        if "/wp-json/" not in url:
            return super().fetch(url, **kw)
        self.requests.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        after = (q.get("after") or [None])[0]
        per_page = int((q.get("per_page") or ["10"])[0])
        page = int((q.get("page") or ["1"])[0])

        chosen = sorted(p for p in self.posts if after is None or p[0] > after)
        window = chosen[(page - 1) * per_page: page * per_page]
        body = json.dumps([
            {"id": i, "date_gmt": d, "modified_gmt": d, "link": f"{WPBASE}/{slug}/",
             "title": {"rendered": title},
             "content": {"rendered": "<p>" + PARA * 3 + "</p>"},
             "excerpt": {"rendered": "<p>short</p>"}, "slug": slug,
             "status": "publish", "type": "post"}
            for i, (d, slug, title) in enumerate(window)])
        return net.Response(url=url, status=200,
                            headers={"x-wp-total": str(len(chosen)),
                                     "content-type": "application/json"},
                            body=body.encode())


class TestWordPressUpdates(SyncHarness):
    """The REST API can filter by date, so a routine update is one request."""

    def _posts(self, n=8):
        # Real past dates: dates.parse_iso refuses a future one, and a fixture
        # of undated posts would prove nothing about a date filter.
        return [(f"20{10 + i:02d}-03-04T10:00:00", f"post-{i}", f"Post {i}")
                for i in range(n)]

    def test_the_whole_archive_is_imported_first(self):
        fn = FakeWordPress(self._posts())
        prog = run_sync(fn, self.dbfile, spec=WP)
        self.assertEqual(len(self.urls()), 8)
        self.assertEqual(prog.new, 8)
        # No date filter on a full scan: it must see everything.
        self.assertEqual(fn.count("after="), 0)

    def test_a_routine_update_asks_only_for_what_is_new(self):
        posts = self._posts()
        fn = FakeWordPress(posts)
        run_sync(fn, self.dbfile, spec=WP)

        fn.posts = posts + [("2026-01-01T00:00:00", "fresh", "Fresh")]
        fn.requests.clear()
        prog = run_sync(fn, self.dbfile, spec=WP, newest_only=True)

        self.assertEqual(prog.new, 1)
        self.assertEqual(len(self.urls()), 9)
        # One API request, carrying the cutoff -- not a walk through 8 pages.
        api = [u for u in fn.requests if "/wp-json/" in u]
        self.assertEqual(len(api), 1)
        self.assertIn("after=", api[0])

    def test_the_cutoff_reaches_back_far_enough_to_be_safe(self):
        """WordPress compares `after` against the site's local publication
        time, while the cutoff is UTC. A filter set exactly at the newest
        article held would lose a post to the site's own UTC offset."""
        posts = self._posts()
        fn = FakeWordPress(posts)
        run_sync(fn, self.dbfile, spec=WP)

        newest = max(p[0] for p in posts)
        run_sync(fn, self.dbfile, spec=WP, newest_only=True)
        api = [u for u in fn.requests if "after=" in u][-1]
        cutoff = urllib.parse.parse_qs(urllib.parse.urlparse(api).query)["after"][0]
        self.assertLess(cutoff, newest)
        self.assertGreater(cutoff, "2017-02")  # near the end, not the start
