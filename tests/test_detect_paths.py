"""Detection when the pasted URL has a path, site naming, and the
Works in Progress recipe (a feed-only, REST-less WordPress-alike)."""
from __future__ import annotations

import json
import re
import unittest

from chronicle import net
from chronicle.sources import detect, discovery, recipe_for
from chronicle.sources.base import Context  # noqa: F401

from fakesite import PARA, FakeNet, listing_html, rss_xml
from test_discovery import BASE, SyncHarness, run_sync, _wp_api


class TestDetectWithPath(unittest.TestCase):
    def setUp(self):
        net.set_rate(0.0)

    def test_blog_path_on_wordpress_uses_the_api(self):
        fn = FakeNet()
        fn.add(BASE + "/", '<html><head><meta property="og:site_name" '
               'content="Farnam Street"><title>FS - Mastering the best</title>'
               "</head></html>")
        fn.add(f"{BASE}/blog/", "<html><title>Blog - Farnam Street</title></html>")
        fn.add(f"{BASE}/wp-json/wp/v2/posts?per_page=1", '[{"id": 1}]',
               headers={"x-wp-total": "1383"})
        with fn.patched():
            spec = detect(BASE + "/blog/")
        self.assertEqual(spec["plugin"], "wordpress")
        self.assertEqual(spec["name"], "Farnam Street")
        self.assertNotIn("path_prefix", spec["config"])

    def test_archive_path_on_ghost_uses_the_api(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>G</title>"
               '<script>ghost apiKey: "0123456789abcdef0123456789"'
               "/ghost/api/</script></html>")
        fn.add(f"{BASE}/archive/", "<html><title>Archive</title></html>")
        with fn.patched():
            spec = detect(BASE + "/archive/")
        self.assertEqual(spec["plugin"], "ghost")

    def test_no_api_keeps_the_section_scope(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>S</title></html>")
        fn.add(f"{BASE}/journal/", "<html><title>Journal</title></html>")
        with fn.patched():
            spec = detect(BASE + "/journal/")
        self.assertEqual(spec["plugin"], "generic")
        self.assertEqual(spec["config"]["path_prefix"], "/journal")

    def test_no_api_listing_path_stays_archive(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>S</title></html>")
        with fn.patched():
            spec = detect(BASE + "/archive/")
        self.assertEqual(spec["plugin"], "generic")
        self.assertEqual(spec["config"]["strategy"], "archive")
        self.assertNotIn("path_prefix", spec["config"])


class TestSiteName(unittest.TestCase):
    def test_splits_ascii_hyphen_separator(self):
        f = detect.__globals__["_site_title_from"]
        self.assertEqual(f("<title>Archive - Works in Progress</title>"),
                         "Archive")
        self.assertEqual(f("<title>Works in Progress - Home</title>"),
                         "Works in Progress")

    def test_keeps_unspaced_hyphens(self):
        f = detect.__globals__["_site_title_from"]
        self.assertEqual(f("<title>Ex-Pat Notes | Blog</title>"), "Ex-Pat Notes")
        self.assertEqual(f("<title>Well-Known Blog</title>"), "Well-Known Blog")

    def test_homepage_name_beats_page_title(self):
        fn = FakeNet()
        fn.add(BASE + "/", "<html><title>Quiet Place | Home</title></html>")
        fn.add(f"{BASE}/journal/", "<html><title>Journal - Quiet Place</title></html>")
        with fn.patched():
            spec = detect(BASE + "/journal/")
        self.assertEqual(spec["name"], "Quiet Place")


class TestDiscoveryHelpers(unittest.TestCase):
    def test_plural_authors_sitemap_is_skipped(self):
        for name in ("authors-sitemap.xml", "author-sitemap.xml",
                     "authors_sitemap.xml", "authorssitemap.xml"):
            self.assertTrue(discovery._SITEMAP_CHILD_SKIP.search(
                f"https://x.example/{name}"), name)
        self.assertFalse(discovery._SITEMAP_CHILD_SKIP.search(
            "https://x.example/post-sitemap.xml"))

    def test_dict_valued_graph_is_descended(self):
        data = {"@graph": {"1": {"@type": "WebSite"},
                           "2": {"@type": "Article",
                                 "datePublished": "2020-05-06T07:08:09+00:00"}}}
        types = [d.get("@type") for d in discovery._iter_ld(data)]
        self.assertIn("Article", types)

    def test_dict_graph_date_is_extracted(self):
        ld = json.dumps({"@graph": {"1": {"@type": "WebSite"},
                                    "2": {"@type": "Article",
                                          "datePublished": "2020-05-06T07:08:09+00:00"}}})
        html = ('<html><head><script type="application/ld+json">'
                f"{ld}</script></head><body><article><p>x</p></article></body></html>")
        d = discovery.extract_date(discovery.htmlutil.parse(html), BASE + "/a/")
        self.assertTrue(d.known)
        self.assertTrue(d.iso.startswith("2020-05-06"))


WIP = "https://worksinprogress.co"


def _wip_article(slug: str, iso: str) -> str:
    ld = json.dumps({"@context": "https://schema.org", "@graph": [
        {"@type": ["Article", "WebPage"], "headline": slug,
         "datePublished": iso, "dateModified": "2026-11-01T00:00:00+00:00"},
        {"@type": "WebSite", "name": "Works in Progress Magazine"}]})
    return (f"<html><head><title>{slug} - Works in Progress Magazine</title>"
            f'<script type="application/ld+json">{ld}</script></head><body>'
            '<nav><a href="/archive/">Archive</a></nav>'
            '<article class="single-article__article">'
            f"<h1>{slug}</h1>{''.join(f'<p>{PARA}</p>' for _ in range(3))}"
            "</article></body></html>")


class TestWorksInProgress(SyncHarness):
    SLUGS = ["alpha", "beta", "gamma"]
    DATES = ["2026-10-26T11:23:34+00:00", "2026-10-23T10:15:44+00:00",
             "2022-03-01T09:00:00+00:00"]

    def build(self) -> FakeNet:
        fn = FakeNet()
        fn.add(WIP + "/", "<html><head><title>Works in Progress Magazine"
               "</title></head><body>home</body></html>")
        # gamma is deliberately absent from the feed: only the archive lists
        # it, so its exact date must come from the page's JSON-LD.
        items = [{"link": f"{WIP}/issue/alpha/", "title": "alpha",
                  "date": "Mon, 26 Oct 2026 11:23:34 GMT", "content": False},
                 {"link": f"{WIP}/issue/beta/", "title": "beta",
                  "date": "Fri, 23 Oct 2026 10:15:44 GMT", "content": False}]
        fn.add(WIP + "/rss.xml", rss_xml(WIP, items))
        junk = ["/issue-05/", "/print/", "/our-authors/", "/notes-on-progress/",
                "/nop/", "/special-issue/"]
        fn.add(WIP + "/archive/", listing_html(
            "Archive - Works in Progress Magazine",
            [f"{WIP}/issue/{s}/" for s in self.SLUGS] + [WIP + j for j in junk]))
        fn.redirect(WIP + "/archive", WIP + "/archive/")
        for j in junk:
            fn.add(WIP + j, listing_html("Listing", [f"{WIP}/issue/alpha/"]))
        for s, d in zip(self.SLUGS, self.DATES):
            fn.add(f"{WIP}/issue/{s}/", _wip_article(s, d))
        return fn

    def test_recipe_pins_generic_config(self):
        r = recipe_for("worksinprogress.co")
        self.assertIsNotNone(r)
        with FakeNet().patched():
            spec = detect(WIP + "/archive/")
        self.assertEqual(spec["plugin"], "generic")
        self.assertEqual(spec["name"], "Works in Progress")
        self.assertEqual(spec["config"]["feed"], "/rss.xml")
        self.assertEqual(spec["config"]["path_prefix"], "/issue/")

    def test_end_to_end_only_issue_articles_with_exact_dates(self):
        fn = self.build()
        prog = run_sync(fn, self.dbfile, url=WIP + "/archive/")
        self.assertEqual(prog.error, "")
        rows = self.urls()
        self.assertEqual([r[0] for r in rows],
                         sorted(f"{WIP}/issue/{s}/" for s in self.SLUGS))
        got = {r[0]: r[1] for r in rows}
        for s, d in zip(self.SLUGS, self.DATES):
            self.assertTrue(str(got[f"{WIP}/issue/{s}/"]).startswith(d[:10]),
                            (s, got))
        for j in ("/issue-05/", "/print/", "/nop/"):
            self.assertEqual(fn.count(WIP + j), 0, j)
        self.assertTrue(all(r[2] == "ok" or r[2] for r in rows))


if __name__ == "__main__":
    unittest.main()
