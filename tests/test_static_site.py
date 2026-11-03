"""A hand-built static site modelled on aresluna.org: an Atom feed of recent
posts whose bodies are one-line summaries, a homepage listing many more posts
with root-relative links, old pages whose only date is a text timestamp, a
robots.txt with a Crawl-delay and no sitemap."""
from __future__ import annotations

import unittest
from unittest import mock

from chronicle import net
from chronicle.sources import discovery

from fakesite import FakeNet, PARA
from test_discovery import SyncHarness, run_sync

SITE = "https://ares.example"
RECENT = 5
OLDER = 7


def _page(title: str, stamp: str) -> str:
    return (f"<html><head><title>{title}</title></head><body><h1>{title}</h1>"
            f"<p>{stamp}</p>" + "".join(f"<p>{PARA}</p>" for _ in range(4))
            + "</body></html>")


def build_site() -> FakeNet:
    fn = FakeNet()
    links = []
    entries = []
    for i in range(RECENT):
        u = f"{SITE}/walk-{i}/"
        fn.add(u, _page(f"Walk {i}", f"{10 + i} Sep 2026 / 00:54"))
        links.append(f'<a href="/walk-{i}/">Walk {i}</a>')
        entries.append(
            f'<entry><title>Walk {i}</title>'
            f'<link rel="alternate" href="{u}"/>'
            f"<updated>2026-09-{10 + i:02d}T00:54:00Z</updated>"
            f'<content type="html">(6,100 words, 600 photos.)</content></entry>')
    for i in range(OLDER):
        u = f"{SITE}/old-{i}/"
        fn.add(u, _page(f"Old {i}", f"{17 + i} Sep 2019 / 00:54"))
        links.append(f'<a href="/old-{i}/">Old {i}</a>')
    # Entries that are not articles: the homepage, another host, a podcast,
    # and the feed itself.
    for junk in (f"{SITE}/", "https://unsung.ares.example/",
                 "https://elsewhere.example/x/",
                 f"{SITE}/audio/ep1.mp3", f"{SITE}/main.rss"):
        entries.append(f'<entry><title>junk</title><link rel="alternate" '
                       f'href="{junk}"/><updated>2026-09-01T00:00:00Z</updated>'
                       f'<content type="html">(x)</content></entry>')
    fn.add(f"{SITE}/main.rss",
           '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
           "<title>Ares</title>" + "".join(entries) + "</feed>")
    fn.add(SITE + "/", '<html><head><title>Ares</title><link rel="alternate" '
           'type="application/atom+xml" href="/main.rss"></head><body>'
           + "".join(links) + '<a href="/main.rss">feed</a>'
           '<a href="/audio/ep1.mp3">listen</a></body></html>')
    fn.add("https://elsewhere.example/x/", _page("Elsewhere", "1 Jan 2020 / 10:00"))
    fn.add(SITE + "/robots.txt",
           "User-agent: *\nCrawl-delay: 10\nDisallow: /private/\n")
    return fn


SPEC = {"name": "Ares", "plugin": "generic", "homepage": SITE, "config": {}}


class TestStaticSite(SyncHarness):
    def setUp(self):
        super().setUp()
        net.reset_crawl_delays()

    def tearDown(self):
        net.reset_crawl_delays()
        super().tearDown()

    def sync(self, fn, **kw):
        # Honouring Crawl-delay would sleep for real; its effect is tested
        # on the limiter itself.
        with mock.patch.object(net._limiter, "wait"):
            return run_sync(fn, self.dbfile, spec=SPEC, **kw)

    def rows(self):
        c = self.conn()
        rows = c.execute("SELECT url, published_at, date_source, "
                         "content_status, word_count FROM articles "
                         "ORDER BY url").fetchall()
        c.close()
        return rows

    def test_every_listed_post_is_stored_with_a_real_body(self):
        fn = build_site()
        self.sync(fn)
        rows = self.rows()
        self.assertEqual(len(rows), RECENT + OLDER)
        self.assertEqual({r["content_status"] for r in rows}, {"ok"})
        # A one-line feed summary is never taken as the article.
        self.assertTrue(all(r["word_count"] > 100 for r in rows))

    def test_non_articles_in_the_feed_are_dropped(self):
        fn = build_site()
        self.sync(fn)
        self.assertEqual(fn.count("unsung.ares.example"), 0)
        self.assertFalse([r for r in self.rows() if "elsewhere" in r["url"]])
        self.assertEqual(fn.count(".mp3"), 0)
        self.assertEqual(fn.count("main.rss/"), 0)

    def test_old_pages_are_dated_from_their_text_timestamp(self):
        fn = build_site()
        self.sync(fn)
        old = [r for r in self.rows() if "/old-" in r["url"]]
        self.assertEqual(len(old), OLDER)
        for r in old:
            self.assertTrue(r["published_at"].startswith("2019-09-"), r["published_at"])
            self.assertEqual(r["date_source"], "text:timestamp")

    def test_a_missing_sitemap_is_remembered(self):
        fn = build_site()
        self.sync(fn)
        first = fn.count("sitemap")
        self.assertGreater(first, 0)
        fn.requests.clear()
        self.sync(fn)
        self.assertEqual(fn.count("sitemap"), 0)

    def test_rows_that_failed_on_a_feed_teaser_heal(self):
        fn = build_site()
        self.sync(fn)
        c = self.conn()
        c.execute("UPDATE articles SET content_status='error', content_html=NULL,"
                  " content_fetched_at=NULL WHERE url LIKE '%/walk-0/'")
        c.commit()
        c.close()
        fn.requests.clear()
        self.sync(fn)
        row = [r for r in self.rows() if r["url"].endswith("/walk-0/")][0]
        self.assertEqual(row["content_status"], "ok")
        self.assertGreater(row["word_count"], 100)

    def test_a_resync_makes_no_per_article_requests(self):
        fn = build_site()
        self.sync(fn)
        fn.requests.clear()
        self.sync(fn)
        self.assertEqual(fn.count("/walk-") + fn.count("/old-"), 0)


class TestCrawlDelay(unittest.TestCase):
    def setUp(self):
        net.reset_crawl_delays()

    def tearDown(self):
        net.reset_crawl_delays()

    def test_parse(self):
        p = net.parse_crawl_delay
        self.assertEqual(p("User-agent: *\nCrawl-delay: 10\n"), 10.0)
        self.assertIsNone(p("User-agent: *\nDisallow: /x\n"))
        self.assertEqual(p("User-agent: Googlebot\nCrawl-delay: 99\n\n"
                           "User-agent: *\nCrawl-delay: 4.5 # polite\n"), 4.5)
        self.assertIsNone(p("User-agent: Googlebot\nCrawl-delay: 5\n"))
        self.assertEqual(p("User-agent: *\nCrawl-delay: 600\n"),
                         net.CRAWL_DELAY_CAP)
        self.assertIsNone(p("User-agent: *\nCrawl-delay: soon\n"))

    def test_the_limiter_spaces_that_host_but_not_others(self):
        lim = net._HostLimiter(0.0)
        lim.min_gap["slow.example"] = 10.0
        with mock.patch("time.sleep") as sleep, \
                mock.patch("time.monotonic", return_value=100.0):
            lim.wait("slow.example")
            lim.wait("slow.example")
            lim.wait("fast.example")
            lim.wait("fast.example")
        self.assertEqual(lim._next["slow.example"], 120.0)
        self.assertEqual(lim._next["fast.example"], 100.0)

    def test_a_first_build_does_not_halve_it(self):
        old = (net._limiter.base, net._limiter.interval)
        try:
            net.set_rate(0.6)
            net._limiter.min_gap["slow.example"] = 10.0
            net.set_rate_scale(0.5)
            self.assertEqual(net._limiter.gap("slow.example"), 10.0)
            self.assertEqual(net._limiter.gap("other.example"), 0.3)
        finally:
            net._limiter.base, net._limiter.interval = old
            net._limiter.min_gap.clear()

    def test_apply_reads_robots_once_per_host(self):
        fn = FakeNet()
        fn.add("https://slow.example/robots.txt",
               "User-agent: *\nCrawl-delay: 7\n")
        with fn.patched():
            net.apply_crawl_delay("https://slow.example/blog")
            net.apply_crawl_delay("https://slow.example/")
        self.assertEqual(fn.count("robots.txt"), 1)
        self.assertEqual(net._limiter.gap("slow.example"), 7.0)


class TestFeedItemHygiene(unittest.TestCase):
    def test_feed_and_media_urls_are_not_articles(self):
        for u in ("https://x.example/main.rss", "https://x.example/a.atom",
                  "https://x.example/ep.mp3", "https://x.example/f.xml"):
            self.assertEqual(discovery.classify_url(u), "skip", u)

    def test_text_timestamp_is_a_weak_date(self):
        from chronicle import htmlutil
        soup = htmlutil.parse(_page("T", "17 Sep 2019 / 00:54"))
        d = discovery.extract_date(soup, "https://x.example/t/")
        self.assertTrue(d.iso.startswith("2019-09-17"))
        self.assertEqual(d.source, "text:timestamp")
        self.assertEqual(d.confidence, "inferred")
        # A bare date in prose is not a timestamp.
        soup = htmlutil.parse("<html><body><p>On 17 Sep 2019 we left.</p></body></html>")
        self.assertFalse(discovery.extract_date(soup, "https://x.example/t/").known)
