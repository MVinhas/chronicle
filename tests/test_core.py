"""Unit tests for the parts that decide reading order and reading quality.

Run: tools/run-tests.sh
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest

from chronicle import dates, htmlutil, net
from chronicle.sources.base import assess


class TestDates(unittest.TestCase):
    def test_iso_with_timezone_and_fraction(self):
        d = dates.parse_iso("2026-08-25T19:22:59.000+08:00", source="ghost")
        self.assertEqual(d.iso, "2026-08-25T11:22:59")
        self.assertEqual(d.precision, "day")
        self.assertEqual(d.confidence, "exact")

    def test_rfc822(self):
        d = dates.parse_rfc822("Thu, 16 Apr 2026 17:25:38 +0000", source="feed")
        self.assertEqual(d.iso, "2026-04-16T17:25:38")

    def test_month_year_gives_month_precision(self):
        d = dates.parse_freeform("April 2001, rev. April 2003", source="pg")
        self.assertEqual(d.iso, "2001-04-01T00:00:00")
        self.assertEqual(d.precision, "month")

    def test_permalink(self):
        d = dates.parse_from_url(
            "https://www.mrmoneymustache.com/2011/04/06/meet-mr-money-mustache/")
        self.assertEqual(d.iso, "2011-04-06T00:00:00")
        self.assertEqual(d.confidence, "high")

    def test_rejects_future_and_prehistoric(self):
        self.assertFalse(dates.parse_iso("2099-01-01").known)
        self.assertFalse(dates.parse_iso("1901-01-01").known)

    def test_unknown_when_absent(self):
        self.assertFalse(dates.parse_freeform("no date at all here").known)

    def test_month_names_are_locale_independent(self):
        """The Flatpak inherits the user's locale; dates must not follow it."""
        self.assertEqual(dates.MONTH_NAMES[5], "May")
        self.assertEqual(
            dates.format_display("2018-05-16T00:00:00", "day"), "16 May 2018")

    def test_display_respects_precision(self):
        self.assertEqual(
            dates.format_display("2002-02-01T00:00:00", "month"), "February 2002")
        self.assertEqual(
            dates.format_display("2002-02-01T00:00:00", "year"), "2002")

    def test_inferred_is_marked_circa(self):
        self.assertTrue(
            dates.format_display("2002-03-01T00:00:00", "month", "inferred")
            .startswith("circa"))

    def test_window_end(self):
        d = dates.PubDate("2002-02-01T00:00:00", "month", "medium", "t")
        self.assertEqual(d.window_end().day, 28)


class TestUrls(unittest.TestCase):
    def test_canonical_strips_tracking_and_www(self):
        self.assertEqual(
            net.canonical_url("http://www.Example.com/a/?utm_source=x#frag"),
            "https://example.com/a")

    def test_canonical_is_stable(self):
        a = net.canonical_url("https://example.com/post/")
        b = net.canonical_url("http://www.example.com/post")
        self.assertEqual(a, b)


class TestSanitiser(unittest.TestCase):
    def clean(self, html, base="https://example.com/post"):
        soup = htmlutil.parse(html)
        return htmlutil.sanitise(htmlutil.extract_main(soup, []), base)

    def test_drops_scripts_and_navigation(self):
        out = self.clean(
            "<article><p>Real body text that is long enough to score well here "
            "and keep the extractor happy.</p><script>evil()</script>"
            "<nav>menu</nav></article>")
        self.assertNotIn("evil", out)
        self.assertNotIn("<nav", out)
        self.assertIn("Real body text", out)

    def test_keeps_structure(self):
        out = self.clean(
            "<article><h2>Head</h2><p>Body text long enough to be scored as "
            "content by the extractor.</p><ul><li>one</li><li>two</li></ul>"
            "<blockquote>quoted</blockquote><pre><code>x = 1</code></pre>"
            "</article>")
        for tag in ("<h2", "<ul", "<li", "<blockquote", "<pre"):
            self.assertIn(tag, out)

    def test_absolutises_links_and_images(self):
        out = self.clean(
            "<article><p>Body text that is definitely long enough to be kept "
            "by the content extractor here.</p>"
            '<p><a href="/next">n</a><img src="/i.png" alt="x"></p></article>')
        self.assertIn("https://example.com/next", out)
        self.assertIn("https://example.com/i.png", out)

    def test_unwraps_wayback_links(self):
        out = self.clean(
            "<article><p>Body text that is definitely long enough to be kept by "
            "the extractor for this test.</p>"
            '<p><a href="https://web.archive.org/web/2013id_/http://x.com/a">a</a>'
            "</p></article>")
        self.assertIn("http://x.com/a", out)
        self.assertNotIn("web.archive.org", out)

    def test_lazy_image_is_resolved(self):
        out = self.clean(
            "<article><p>Long enough body text for the extractor to keep this "
            "block of content around.</p>"
            '<p><img src="data:image/gif;base64,R0lGOD" '
            'data-src="https://cdn.example.com/real.jpg"></p></article>')
        self.assertIn("cdn.example.com/real.jpg", out)

    def test_survives_decomposition_during_iteration(self):
        """Nested junk containers used to invalidate the iterator."""
        html = ("<article><div class='share'><div class='social'>"
                "<span class='share'>x</span></div></div>"
                "<p>Body text long enough to be treated as the article.</p>"
                "</article>")
        self.assertIn("Body text", self.clean(html))

    def test_excerpt_and_counts(self):
        html = "<p>" + ("word " * 50) + "</p><img src='https://x/y.png'>"
        self.assertEqual(htmlutil.word_count(html), 50)
        self.assertEqual(htmlutil.count_images(html), 1)
        self.assertTrue(htmlutil.make_excerpt(html, 40).endswith("…"))


class TestTitles(unittest.TestCase):
    """Page titles conventionally append the site name; the queue must not."""

    def test_strips_site_name_after_pipe(self):
        self.assertEqual(
            htmlutil.clean_title("Five Years of Rust | Rust Blog"),
            "Five Years of Rust")

    def test_strips_known_suffix(self):
        self.assertEqual(
            htmlutil.clean_title("Spaced Repetition \u00b7 Gwern.net", "Gwern.net"),
            "Spaced Repetition")

    def test_keeps_dashes_that_belong_to_the_title(self):
        for title in ("A Tale of Two Cities - Chapter One",
                      "Why We Sleep \u2014 A Review"):
            self.assertEqual(htmlutil.clean_title(title), title)

    def test_keeps_a_long_trailing_segment(self):
        title = "On Mathematics | An Unusually Long Trailing Clause That Is Not A Site"
        self.assertEqual(htmlutil.clean_title(title), title)

    def test_untouched_when_there_is_no_suffix(self):
        self.assertEqual(
            htmlutil.clean_title("How out of date are Android devices?"),
            "How out of date are Android devices?")


class TestAssess(unittest.TestCase):
    def test_prose_is_ok(self):
        self.assertEqual(assess("<p>" + ("word " * 40) + "</p>"), "ok")

    def test_image_only_post_is_not_empty(self):
        """Several Wait But Why posts are a single drawing and no prose."""
        self.assertEqual(
            assess('<p><img src="https://x/y.png" alt="a"></p>'), "ok")

    def test_truly_empty(self):
        self.assertEqual(assess(""), "empty")
        self.assertEqual(assess("<p>  </p>"), "empty")


class TestScope(unittest.TestCase):
    """A URL with a path means one section of a site, not the whole thing."""

    def _source(self, prefix):
        from chronicle.sources.generic import GenericSource
        row = {"id": 1, "name": "T", "homepage": "https://example.com"}
        return GenericSource(row, {"path_prefix": prefix} if prefix else {})

    def test_no_prefix_accepts_everything(self):
        src = self._source(None)
        self.assertTrue(src.in_scope("https://example.com/anything"))

    def test_prefix_limits_to_the_section(self):
        src = self._source("/c/cases")
        self.assertTrue(src.in_scope("https://example.com/c/cases/a-case"))
        self.assertTrue(src.in_scope("https://example.com/c/cases/"))
        self.assertFalse(src.in_scope("https://example.com/c/concepts/x"))
        self.assertFalse(src.in_scope("https://example.com/some-post"))

    def test_prefix_does_not_match_a_longer_sibling(self):
        src = self._source("/blog")
        self.assertFalse(src.in_scope("https://example.com/blogroll/x"))
        self.assertTrue(src.in_scope("https://example.com/blog/x"))


class TestSectionMembershipScoping(unittest.TestCase):
    """A section index whose articles live elsewhere on the site.

    WordPress category and tag pages are the common case: the index sits at
    /category/<name>/ but every post it lists is at the site root. Scoping
    such a source by path prefix rejected every article the index found, so
    the source archived nothing at all -- silently.
    """

    BASE = "https://example.com"
    SLUGS = ["prostate-cancer-psa", "metformin-and-cancer", "gum-recession",
             "cataracts-and-dementia"]
    MORE = ["protein-and-renal-function", "multifactorial-trials"]

    def _listing(self, slugs, next_page=None):
        links = "".join(
            f'<li><a href="{self.BASE}/{s}/">{s}</a> '
            f'<span>January {i + 1}, 2024</span></li>'
            for i, s in enumerate(slugs))
        head = f'<link rel="next" href="{next_page}"/>' if next_page else ""
        return f"<html><head>{head}</head><body><ul>{links}</ul></body></html>"

    def _site(self):
        from fakesite import FakeNet
        fn = FakeNet()
        index = f"{self.BASE}/category/weekly-newsletter/"
        fn.add(index, self._listing(self.SLUGS, index + "page/2/"))
        fn.add(index + "page/2/", self._listing(self.MORE))
        for slug in self.SLUGS + self.MORE + ["some-podcast-episode"]:
            fn.add(f"{self.BASE}/{slug}/",
                   "<html><body><article><h1>" + slug + "</h1>" +
                   "<p>Real prose about the subject at hand. " * 40 +
                   "</p></article></body></html>")
        return fn

    def _source(self):
        from chronicle.sources.generic import GenericSource
        return GenericSource(
            {"id": 1, "name": "Example", "homepage": self.BASE},
            {"strategy": "archive", "index": "/category/weekly-newsletter/",
             "path_prefix": "/category/weekly-newsletter"})

    def _discover(self, src):
        from chronicle.sources.base import Context
        with self._site().patched():
            return list(src.discover(Context()))

    def test_a_category_index_finds_its_articles(self):
        src = self._source()
        found = {s.url for s in self._discover(src)}
        self.assertEqual(
            found, {f"{self.BASE}/{s}/" for s in self.SLUGS + self.MORE})

    def test_later_pages_of_the_index_still_count(self):
        """Page 2 must widen the scope, not replace what page 1 vouched for."""
        src = self._source()
        found = {s.url for s in self._discover(src)}
        for slug in self.MORE:
            self.assertIn(f"{self.BASE}/{slug}/", found)
        for slug in self.SLUGS:
            self.assertIn(f"{self.BASE}/{slug}/", found)

    def test_the_rest_of_the_site_stays_out(self):
        """Widening the scope must not turn a section into the whole site."""
        src = self._source()
        self._discover(src)
        self.assertFalse(src.in_scope(f"{self.BASE}/some-podcast-episode/"))
        self.assertTrue(src.in_scope(f"{self.BASE}/prostate-cancer-psa/"))

    def test_a_genuine_path_section_is_still_scoped_by_path(self):
        """The ordinary case must not regress: /blog/ means /blog/."""
        from chronicle.sources.generic import GenericSource
        src = GenericSource({"id": 1, "name": "E", "homepage": self.BASE},
                            {"path_prefix": "/blog"})
        self.assertTrue(src.in_scope(f"{self.BASE}/blog/a-post"))
        self.assertFalse(src.in_scope(f"{self.BASE}/elsewhere/a-post"))


class TestQueue(unittest.TestCase):
    """Ordering, de-duplication and date-confidence rules in the store."""

    def setUp(self):
        from chronicle import db
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = db
        self.conn = db.connect(self.tmp.name)
        db.init(self.conn)
        self.sid = db.add_source(self.conn, "t", "Test", "generic", "https://t.example")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def add(self, guid, iso, precision="day", confidence="exact", order=0,
            title=None):
        aid, _ = self.db.upsert_article(
            self.conn, self.sid, guid, url=f"https://t.example/{guid}",
            title=title or guid, published_at=iso, date_precision=precision,
            date_confidence=confidence, date_source="test", source_order=order)
        self.conn.execute(
            "UPDATE articles SET content_status='ok', content_html='<p>x</p>' "
            "WHERE id=?", (aid,))
        return aid

    def test_orders_oldest_first_and_undated_last(self):
        self.add("c", "2015-01-01T00:00:00")
        self.add("a", "2001-01-01T00:00:00")
        self.add("z", None, "unknown", "unknown")
        self.add("b", "2009-06-01T00:00:00")
        titles = [r["title"] for r in self.db.queue(self.conn)]
        self.assertEqual(titles, ["a", "b", "c", "z"])

    def test_upsert_does_not_duplicate(self):
        first = self.add("same", "2010-01-01T00:00:00")
        second = self.add("same", "2010-01-01T00:00:00")
        self.assertEqual(first, second)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"], 1)

    def test_a_known_date_always_replaces_the_stored_one(self):
        """The source is authoritative for its own articles, in both directions.

        A corrected adapter may become *less* certain -- a date it used to read
        out of prose becoming an honest estimate -- and that has to be able to
        land.
        """
        aid = self.add("x", "2010-01-01T00:00:00", confidence="medium")
        self.db.upsert_article(
            self.conn, self.sid, "x", published_at="2011-02-03T00:00:00",
            date_precision="day", date_confidence="exact", date_source="better")
        self.assertEqual(
            self.db.get_article(self.conn, aid)["published_at"],
            "2011-02-03T00:00:00")

        self.db.upsert_article(
            self.conn, self.sid, "x", published_at="2009-01-01T00:00:00",
            date_precision="year", date_confidence="inferred",
            date_source="downgraded")
        row = self.db.get_article(self.conn, aid)
        self.assertEqual(row["published_at"], "2009-01-01T00:00:00")
        self.assertEqual(row["date_confidence"], "inferred")

    def test_an_unknown_date_never_wipes_a_known_one(self):
        """A failed fetch must not erase a date we already have."""
        aid = self.add("x", "2010-01-01T00:00:00", confidence="exact")
        self.db.upsert_article(self.conn, self.sid, "x", published_at=None,
                               date_precision="unknown",
                               date_confidence="unknown", date_source="")
        self.assertEqual(
            self.db.get_article(self.conn, aid)["published_at"],
            "2010-01-01T00:00:00")

    def test_equal_confidence_correction_lands(self):
        """A re-sync must be able to correct a date the adapter read wrongly."""
        aid = self.add("x", "1998-06-01T00:00:00", precision="month",
                       confidence="medium")
        self.db.upsert_article(
            self.conn, self.sid, "x", published_at="2012-01-01T00:00:00",
            date_precision="month", date_confidence="medium",
            date_source="text:dateline")
        self.assertEqual(
            self.db.get_article(self.conn, aid)["published_at"],
            "2012-01-01T00:00:00")

    def test_neighbour_walks_the_queue(self):
        a = self.add("a", "2001-01-01T00:00:00")
        b = self.add("b", "2002-01-01T00:00:00")
        c = self.add("c", "2003-01-01T00:00:00")
        self.assertEqual(self.db.neighbour(self.conn, b, +1)["id"], c)
        self.assertEqual(self.db.neighbour(self.conn, b, -1)["id"], a)
        self.assertIsNone(self.db.neighbour(self.conn, c, +1))
        self.assertIsNone(self.db.neighbour(self.conn, a, -1))

    def test_same_date_is_broken_by_source_order(self):
        first = self.add("f", "2005-01-01T00:00:00", order=1)
        second = self.add("s", "2005-01-01T00:00:00", order=2)
        self.assertEqual(self.db.neighbour(self.conn, first, +1)["id"], second)

    def test_position_in_queue(self):
        self.add("a", "2001-01-01T00:00:00")
        b = self.add("b", "2002-01-01T00:00:00")
        self.add("c", "2003-01-01T00:00:00")
        self.assertEqual(self.db.position_in_queue(self.conn, b), (2, 3))

    def test_read_and_favourite_state(self):
        aid = self.add("a", "2001-01-01T00:00:00")
        self.assertEqual(self.db.queue_counts(self.conn)["unread"], 1)
        self.db.set_read(self.conn, aid, True)
        self.assertEqual(self.db.queue_counts(self.conn)["unread"], 0)
        self.assertTrue(self.db.toggle_favourite(self.conn, aid))
        self.assertEqual(self.db.queue_counts(self.conn)["favourites"], 1)
        self.assertFalse(self.db.toggle_favourite(self.conn, aid))

    def test_hide_read_filters_the_queue_scopes(self):
        a = self.add("a", "2001-01-01T00:00:00")
        self.add("b", "2002-01-01T00:00:00")
        self.db.set_read(self.conn, a, True)

        self.assertEqual(len(self.db.queue(self.conn)), 2)
        self.assertEqual(len(self.db.queue(self.conn, hide_read=True)), 1)

    def test_hide_read_leaves_favourites_alone(self):
        """Favouriting happens while reading, so favourites are mostly read.

        Applying hide-read here would empty the one list a reader curated by
        hand, at exactly the moment they went looking for it.
        """
        a = self.add("a", "2001-01-01T00:00:00")
        self.db.set_read(self.conn, a, True)
        self.db.toggle_favourite(self.conn, a)

        self.assertEqual(len(self.db.queue(self.conn, scope="favourites")), 1)
        self.assertEqual(
            len(self.db.queue(self.conn, scope="favourites", hide_read=True)), 1)

    def test_hide_read_leaves_the_read_scope_alone(self):
        """It would otherwise contradict itself and always show nothing."""
        a = self.add("a", "2001-01-01T00:00:00")
        self.db.set_read(self.conn, a, True)
        self.assertEqual(
            len(self.db.queue(self.conn, scope="read", hide_read=True)), 1)

    def test_unread_favourites_still_listed_when_hiding_read(self):
        """The exemption widens the list; it must not narrow it."""
        a = self.add("a", "2001-01-01T00:00:00")
        b = self.add("b", "2002-01-01T00:00:00")
        self.db.toggle_favourite(self.conn, a)
        self.db.toggle_favourite(self.conn, b)
        self.db.set_read(self.conn, a, True)
        titles = [r["title"] for r in
                  self.db.queue(self.conn, scope="favourites", hide_read=True)]
        self.assertEqual(titles, ["a", "b"])

    def test_navigation_still_skips_read_articles(self):
        """The exemption is scoped to the favourites list, not to hide-read."""
        a = self.add("a", "2001-01-01T00:00:00")
        b = self.add("b", "2002-01-01T00:00:00")
        c = self.add("c", "2003-01-01T00:00:00")
        self.db.set_read(self.conn, b, True)
        self.db.toggle_favourite(self.conn, b)
        nxt = self.db.neighbour(self.conn, a, +1, hide_read=True)
        self.assertEqual(nxt["id"], c)

    # -- skipping ---------------------------------------------------------

    def test_skipping_takes_an_article_out_of_the_queue(self):
        """The point of the button: the queue you work through shrinks."""
        self.add("a", "2001-01-01T00:00:00")
        b = self.add("b", "2002-01-01T00:00:00")
        self.db.set_skipped(self.conn, b, True)

        self.assertEqual([r["title"] for r in self.db.queue(self.conn)], ["a"])
        self.assertEqual(self.db.queue_counts(self.conn)["all"], 1)
        self.assertEqual(self.db.queue_counts(self.conn)["skipped"], 1)

    def test_a_skip_is_not_a_read(self):
        """Conflating them would make the per-blog percentage unanswerable."""
        a = self.add("a", "2001-01-01T00:00:00")
        self.db.set_skipped(self.conn, a, True)
        self.assertTrue(self.db.is_skipped(self.conn, a))
        row = self.conn.execute(
            "SELECT read_at FROM reading_state WHERE article_id=?", (a,)).fetchone()
        self.assertIsNone(row["read_at"])

    def test_the_skipped_scope_shows_them_back(self):
        a = self.add("a", "2001-01-01T00:00:00")
        self.add("b", "2002-01-01T00:00:00")
        self.db.set_skipped(self.conn, a, True)
        self.assertEqual(
            [r["title"] for r in self.db.queue(self.conn, scope="skipped")], ["a"])

    def test_unskipping_restores_the_article(self):
        a = self.add("a", "2001-01-01T00:00:00")
        self.db.set_skipped(self.conn, a, True)
        self.db.set_skipped(self.conn, a, False)
        self.assertEqual(len(self.db.queue(self.conn)), 1)
        self.assertEqual(self.db.queue_counts(self.conn)["skipped"], 0)

    def test_navigation_passes_over_skipped_articles(self):
        a = self.add("a", "2001-01-01T00:00:00")
        b = self.add("b", "2002-01-01T00:00:00")
        c = self.add("c", "2003-01-01T00:00:00")
        self.db.set_skipped(self.conn, b, True)
        self.assertEqual(self.db.neighbour(self.conn, a, +1)["id"], c)

    def test_skip_rate_counts_only_readable_articles(self):
        """A half-built archive must not read as a low skip rate."""
        a = self.add("a", "2001-01-01T00:00:00")
        self.add("b", "2002-01-01T00:00:00")
        pending, _ = self.db.upsert_article(
            self.conn, self.sid, "p", url="https://t.example/p", title="p",
            published_at="2003-01-01T00:00:00")
        self.db.set_skipped(self.conn, a, True)

        skipped, total = self.db.skip_rates(self.conn)[self.sid]
        self.assertEqual((skipped, total), (1, 2))

    # -- highlights list ----------------------------------------------------

    def test_highlighted_scope_lists_only_marked_articles(self):
        a = self.add("a", "2001-01-01T00:00:00")
        b = self.add("b", "2002-01-01T00:00:00")
        self.db.add_highlight(self.conn, a, "a marked passage")
        self.db.set_note(self.conn, b, "a note, but nothing highlighted")

        self.assertEqual(
            [r["title"] for r in self.db.queue(self.conn, scope="highlighted")], ["a"])
        # Notes still lists both -- the two lists answer different questions.
        self.assertEqual(
            [r["title"] for r in self.db.queue(self.conn, scope="annotated")], ["a", "b"])
        self.assertEqual(self.db.queue_counts(self.conn)["highlighted"], 1)

    def test_highlight_list_prefers_the_marked_passage(self):
        """In the Highlights list the passage is the point, not the note."""
        from chronicle.ui.style import note_line
        a = self.add("a", "2001-01-01T00:00:00")
        self.db.add_highlight(self.conn, a, "the words they marked")
        self.db.set_note(self.conn, a, "a note about the whole article")
        row = self.db.queue(self.conn, scope="highlighted")[0]

        self.assertIn("the words they marked", note_line(row, prefer_mark=True))
        self.assertIn("a note about the whole article", note_line(row))

    def test_disabled_source_leaves_the_queue(self):
        self.add("a", "2001-01-01T00:00:00")
        self.db.set_source_enabled(self.conn, self.sid, False)
        self.assertEqual(self.db.queue_counts(self.conn)["all"], 0)

    def test_search(self):
        self.add("a", "2001-01-01T00:00:00", title="Beating the Averages")
        self.add("b", "2002-01-01T00:00:00", title="Taste for Makers")
        hits = self.db.queue(self.conn, search="averages")
        self.assertEqual([r["title"] for r in hits], ["Beating the Averages"])


class TestMigration(unittest.TestCase):
    """Opening a library written by an older Chronicle must not break it."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _old_library(self):
        """A v2 reading_state: no skipped_at column."""
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        conn.executescript("""
            CREATE TABLE reading_state (
                article_id INTEGER PRIMARY KEY, read_at TEXT,
                favourite_at TEXT, scroll_pos REAL NOT NULL DEFAULT 0,
                last_opened_at TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES('schema_version','2');
            INSERT INTO reading_state(article_id, read_at, scroll_pos)
                VALUES(7,'2020-01-01T00:00:00', 0.5);
        """)
        conn.commit()
        conn.close()

    def test_the_new_column_is_added_to_an_existing_library(self):
        from chronicle import db
        self._old_library()
        conn = db.connect(self.tmp.name)
        db.init(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(reading_state)")}
        self.assertIn("skipped_at", cols)
        conn.close()

    def test_existing_reading_state_survives(self):
        """A migration that lost where the reader had got to would be worse
        than no migration at all."""
        from chronicle import db
        self._old_library()
        conn = db.connect(self.tmp.name)
        db.init(conn)
        row = conn.execute("SELECT * FROM reading_state WHERE article_id=7").fetchone()
        self.assertEqual(row["read_at"], "2020-01-01T00:00:00")
        self.assertEqual(row["scroll_pos"], 0.5)
        self.assertIsNone(row["skipped_at"])
        conn.close()

    def test_opening_twice_is_harmless(self):
        from chronicle import db
        self._old_library()
        conn = db.connect(self.tmp.name)
        db.init(conn)
        db.init(conn)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(reading_state)")]
        self.assertEqual(cols.count("skipped_at"), 1)
        conn.close()


    def test_mis_decoded_text_is_repaired_on_open(self):
        """A library built before schema 5 has em dashes stored as U+0097."""
        from chronicle import db
        conn = db.connect(self.tmp.name)
        db.init(conn)
        conn.execute("INSERT INTO sources(slug,name,plugin,added_at) "
                     "VALUES('s','S','generic','2020-01-01T00:00:00')")
        conn.execute(
            "INSERT INTO articles(id,source_id,guid,url,discovered_at,"
            "title,excerpt,content_html) "
            "VALUES(1,1,'g','https://e.com/a','2020-01-01T00:00:00',?,?,?)",
            ("A \u0097 B", "x \u0092s", "<p>machines \u0097 CPU</p>"))
        conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        conn.commit()
        conn.close()

        conn = db.connect(self.tmp.name)
        db.init(conn)
        row = conn.execute("SELECT * FROM articles WHERE id=1").fetchone()
        self.assertEqual(row["title"], "A \u2014 B")
        self.assertEqual(row["excerpt"], "x \u2019s")
        self.assertEqual(row["content_html"], "<p>machines \u2014 CPU</p>")
        self.assertEqual(
            conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            .fetchone()[0], "5")
        conn.close()


class TestDecoding(unittest.TestCase):
    """Bytes to text.

    Getting this wrong corrupts the archive rather than the display, and it
    does so invisibly: paulgraham.com serves windows-1252 with no charset at
    all, and decoding that as ISO-8859-1 turns every em dash into U+0097, a
    control character that renders as nothing.
    """

    def decode(self, body, ctype="text/html"):
        return net.Response("u", 200, {"content-type": ctype}, body).text()

    def test_undeclared_windows_1252_keeps_its_punctuation(self):
        """The Paul Graham case, exactly."""
        got = self.decode(b"machines \x97 CPU \x97 sitting, Women\x92s")
        self.assertEqual(got, "machines \u2014 CPU \u2014 sitting, Women\u2019s")

    def test_declared_latin_1_is_decoded_as_windows_1252(self):
        got = self.decode(b"dash \x97", "text/html; charset=iso-8859-1")
        self.assertEqual(got, "dash \u2014")

    def test_utf8_wins_over_a_wrong_latin_1_declaration(self):
        """Otherwise a mislabelled page mojibakes into 'a<euro>' everywhere."""
        body = "dash \u2014 emoji \U0001F600".encode()
        self.assertEqual(self.decode(body, "text/html; charset=ISO-8859-1"),
                         "dash \u2014 emoji \U0001F600")

    def test_meta_charset_is_honoured_when_the_header_is_silent(self):
        body = "<meta charset='windows-1251'>\u041f\u0440\u0438\u0432\u0435\u0442".encode("cp1251")
        self.assertIn("\u041f\u0440\u0438\u0432\u0435\u0442", self.decode(body))

    def test_a_bom_outranks_a_wrong_declaration(self):
        body = "dash \u2014".encode("utf-8-sig")
        self.assertEqual(self.decode(body, "text/html; charset=iso-8859-1"),
                         "dash \u2014")

    def test_utf16_is_not_mistaken_for_utf8(self):
        """UTF-16 ASCII text is full of NULs, which are valid UTF-8."""
        body = "dash \u2014 emoji \U0001F600".encode("utf-16")
        self.assertEqual(self.decode(body, "text/html; charset=utf-16"),
                         "dash \u2014 emoji \U0001F600")

    def test_a_truncated_body_is_still_utf8(self):
        """_decompress hands back partial bodies; one cut mid-sequence must
        not send the whole page down the single-byte path."""
        body = "dash \u2014 emoji \U0001F600".encode()[:-2]
        self.assertTrue(self.decode(body).startswith("dash \u2014 emoji "))

    def test_a_real_multibyte_encoding_is_left_alone(self):
        body = "\u3053\u3093\u306b\u3061\u306f".encode("shift_jis")
        self.assertEqual(self.decode(body, "text/html; charset=shift-jis"),
                         "\u3053\u3093\u306b\u3061\u306f")

    def test_an_unknown_label_falls_through_to_utf8(self):
        body = "dash \u2014 \U0001F600".encode()
        self.assertEqual(self.decode(body, "text/html; charset=x-nonsense"),
                         "dash \u2014 \U0001F600")

    def test_c1_controls_from_upstream_are_repaired(self):
        """A page that is valid UTF-8 but carries C1 controls was mangled by
        someone else's toolchain. In prose they are never anything but a
        dash or a quote."""
        body = "dash \u0097 quote \u0092".encode()
        self.assertEqual(self.decode(body, "text/html; charset=utf-8"),
                         "dash \u2014 quote \u2019")

    def test_emoji_survive_every_route(self):
        for ctype in ("text/html", "text/html; charset=utf-8",
                      "text/html; charset=iso-8859-1"):
            with self.subTest(ctype=ctype):
                self.assertIn("\U0001F600",
                              self.decode("hi \U0001F600".encode(), ctype))


class TestPaulGraham(unittest.TestCase):
    """The extractor for 1997-era markup and the refusal to invent dates."""

    def setUp(self):
        from chronicle.sources.paulgraham import PaulGrahamSource
        self.src = PaulGrahamSource(None, {})

    def test_br_pairs_become_paragraphs(self):
        raw = ('<html><body><font size="2" face="verdana">February 2002<br /><br />'
               'First paragraph here.<br /><br />Second paragraph here.'
               "</font></body></html>")
        out = self.src._extract(raw, "https://paulgraham.com/x.html")
        self.assertEqual(out.html.count("<p>"), 2)
        self.assertNotIn("February 2002", out.html)

    def test_yellow_table_becomes_blockquote(self):
        raw = ('<html><body><font size="2" face="verdana">March 2003<br /><br />'
               '<table><tr><td bgcolor="#ffffdd">A quoted passage.</td></tr></table>'
               "<br /><br />Body text follows here.</font></body></html>")
        out = self.src._extract(raw, "https://paulgraham.com/x.html")
        self.assertIn("<blockquote>", out.html)
        self.assertNotIn("<table", out.html)

    def test_yc_promo_is_removed(self):
        raw = ('<html><body><font size="2" face="verdana">'
               "<table><tr><td><b>Want to start a startup?</b> Get funded by "
               '<a href="http://ycombinator.com">Y Combinator</a>.</td></tr></table>'
               "April 2005<br /><br />The actual essay begins here."
               "</font></body></html>")
        out = self.src._extract(raw, "https://paulgraham.com/x.html")
        self.assertNotIn("Want to start a startup", out.html)
        self.assertIn("actual essay", out.html)

    def test_dateline_must_open_its_line(self):
        """A title naming a period is not a dateline.

        'Snapshot: Viaweb, June 1998' was written in January 2012.
        """
        lines = ["Snapshot: Viaweb, June 1998", "-->", "January 2012",
                 "A few hours before the Yahoo acquisition in June 1998"]
        self.assertEqual(self.src._dateline(lines).iso, "2012-01-01T00:00:00")

    def test_dateline_keeps_only_the_original_date(self):
        for line, expected in (
                ("August 2006, rev. April 2007, September 2010", "2006-08-01T00:00:00"),
                ("December 2001 (rev. May 2002)", "2001-12-01T00:00:00"),
                ("November 2004, corrected June 2006", "2004-11-01T00:00:00")):
            self.assertEqual(self.src._dateline(["Title", "-->", line]).iso, expected)

    def test_a_date_in_prose_is_not_a_dateline(self):
        """An essay with no dateline stays undated rather than borrowing one."""
        lines = ["Lisp for Web-Based Applications", "-->",
                 "here are some excerpts from a talk I gave in April 2001 at",
                 "BBN Labs in Cambridge, MA."]
        self.assertFalse(self.src._dateline(lines).known)

    def test_bracketing_refuses_non_monotonic_neighbours(self):
        from chronicle.sources.base import Stub

        def mk(name, iso):
            d = dates.parse_iso(iso, source="t") if iso else dates.UNKNOWN
            return Stub(guid=name, url=name, title=name, date=d)

        stubs = [mk("newer", "2002-05-01"), mk("undated", None),
                 mk("older", "2002-02-01")]
        self.src._bracket_undated(stubs)
        self.assertTrue(stubs[1].date.known)
        self.assertEqual(stubs[1].date.confidence, "inferred")

        # An out-of-order neighbour must not produce a confident-looking guess.
        stubs = [mk("newer", "2001-04-01"), mk("undated", None),
                 mk("older", "2016-11-01")]
        self.src._bracket_undated(stubs)
        self.assertFalse(stubs[1].date.known)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAnnotations(unittest.TestCase):
    """Notes and highlights: the one part of the library the reader wrote."""

    def setUp(self):
        from chronicle import db
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = db
        self.conn = db.connect(self.tmp.name)
        db.init(self.conn)
        self.sid = db.add_source(self.conn, "t", "Test", "generic",
                                 "https://t.example")
        self.aid, _ = db.upsert_article(
            self.conn, self.sid, "a", url="https://t.example/a", title="A",
            published_at="2010-01-01T00:00:00", date_precision="day",
            date_confidence="exact", date_source="test")
        self.conn.execute(
            "UPDATE articles SET content_status='ok', content_html='<p>x</p>' "
            "WHERE id=?", (self.aid,))

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_note_roundtrips(self):
        self.db.set_note(self.conn, self.aid, "  worth rereading  ")
        self.assertEqual(self.db.get_note(self.conn, self.aid), "worth rereading")

    def test_emptying_a_note_removes_it(self):
        self.db.set_note(self.conn, self.aid, "something")
        self.db.set_note(self.conn, self.aid, "   ")
        self.assertEqual(self.db.get_note(self.conn, self.aid), "")
        self.assertEqual(self.db.annotation_counts(self.conn, self.aid), (0, False))

    def test_highlights_are_listed_in_reading_order(self):
        self.db.add_highlight(self.conn, self.aid, "second", start_offset=90)
        self.db.add_highlight(self.conn, self.aid, "first", start_offset=10)
        quotes = [r["quote"] for r in self.db.list_highlights(self.conn, self.aid)]
        self.assertEqual(quotes, ["first", "second"])

    def test_orphans_sort_after_anchored_highlights(self):
        """A highlight whose words are gone is kept, but out of the way."""
        lost = self.db.add_highlight(self.conn, self.aid, "gone", start_offset=5)
        self.db.add_highlight(self.conn, self.aid, "here", start_offset=50)
        self.db.reanchor_highlight(self.conn, lost, None)
        rows = self.db.list_highlights(self.conn, self.aid)
        self.assertEqual([r["quote"] for r in rows], ["here", "gone"])
        self.assertIsNotNone(rows[1]["orphaned_at"])

    def test_reanchoring_clears_an_earlier_orphan_flag(self):
        """Re-fetching an article can bring back words a previous fetch lost."""
        hid = self.db.add_highlight(self.conn, self.aid, "q", start_offset=5)
        self.db.reanchor_highlight(self.conn, hid, None)
        self.db.reanchor_highlight(self.conn, hid, 42)
        row = self.db.list_highlights(self.conn, self.aid)[0]
        self.assertIsNone(row["orphaned_at"])
        self.assertEqual(row["start_offset"], 42)

    def test_annotations_survive_a_content_refetch(self):
        """The whole point of quote-anchoring: a re-sync must not erase notes."""
        self.db.add_highlight(self.conn, self.aid, "a memorable line")
        self.db.set_note(self.conn, self.aid, "my thoughts")
        self.db.update_content(self.conn, self.aid, "<p>completely rewritten</p>",
                               status="ok", source="direct", word_count=2,
                               image_count=0, excerpt="", content_hash="new")
        self.assertEqual(self.db.annotation_counts(self.conn, self.aid), (1, True))
        self.assertEqual(self.db.get_note(self.conn, self.aid), "my thoughts")

    def test_deleting_an_article_takes_its_annotations(self):
        self.db.add_highlight(self.conn, self.aid, "q")
        self.db.set_note(self.conn, self.aid, "n")
        self.db.delete_source(self.conn, self.sid)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM highlights").fetchone()["c"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"], 0)

    def test_annotated_scope_collects_notes_and_highlights(self):
        """Anything the reader wrote on, by either means."""
        noted = self.aid
        self.db.set_note(self.conn, noted, "a thought")
        marked, _ = self.db.upsert_article(
            self.conn, self.sid, "b", url="https://t.example/b", title="B",
            published_at="2011-01-01T00:00:00", date_precision="day",
            date_confidence="exact", date_source="t")
        self.conn.execute("UPDATE articles SET content_status='ok', "
                          "content_html='<p>x</p>' WHERE id=?", (marked,))
        self.db.add_highlight(self.conn, marked, "a phrase")
        plain, _ = self.db.upsert_article(
            self.conn, self.sid, "c", url="https://t.example/c", title="C",
            published_at="2012-01-01T00:00:00", date_precision="day",
            date_confidence="exact", date_source="t")
        self.conn.execute("UPDATE articles SET content_status='ok', "
                          "content_html='<p>x</p>' WHERE id=?", (plain,))

        titles = [r["title"] for r in
                  self.db.queue(self.conn, scope="annotated")]
        self.assertEqual(titles, ["A", "B"])
        self.assertEqual(self.db.queue_counts(self.conn)["annotated"], 2)

    def test_annotated_scope_ignores_hide_read(self):
        """Notes are a collection asked for by name, not a queue to work off."""
        self.db.set_note(self.conn, self.aid, "a thought")
        self.db.set_read(self.conn, self.aid, True)
        self.assertEqual(
            len(self.db.queue(self.conn, scope="annotated", hide_read=True)), 1)

    def test_clearing_the_last_annotation_leaves_the_scope(self):
        self.db.set_note(self.conn, self.aid, "a thought")
        self.assertEqual(self.db.queue_counts(self.conn)["annotated"], 1)
        self.db.set_note(self.conn, self.aid, "")
        self.assertEqual(self.db.queue_counts(self.conn)["annotated"], 0)

    def test_queue_carries_the_text_to_preview(self):
        """The row shows the note; failing that, something highlighted."""
        self.db.add_highlight(self.conn, self.aid, "the marked words")
        row = self.db.queue(self.conn, scope="annotated")[0]
        self.assertIsNone(row["note_body"])
        self.assertEqual(row["first_mark"], "the marked words")

        self.db.set_note(self.conn, self.aid, "what I thought")
        row = self.db.queue(self.conn, scope="annotated")[0]
        self.assertEqual(row["note_body"], "what I thought")

    def test_a_highlights_own_note_is_preferred_to_its_quote(self):
        hid = self.db.add_highlight(self.conn, self.aid, "the marked words")
        self.db.set_highlight_note(self.conn, hid, "why it mattered")
        row = self.db.queue(self.conn, scope="annotated")[0]
        self.assertEqual(row["first_mark"], "why it mattered")

    def test_queue_reports_annotation_counts(self):
        self.db.add_highlight(self.conn, self.aid, "one")
        self.db.add_highlight(self.conn, self.aid, "two")
        self.db.set_note(self.conn, self.aid, "note")
        row = self.db.queue(self.conn)[0]
        self.assertEqual(row["highlight_count"], 2)
        self.assertEqual(row["note_count"], 1)


class TestHighlightPayload(unittest.TestCase):
    """The JSON handed to the reading surface."""

    def test_script_terminator_in_a_quote_cannot_break_out(self):
        from chronicle.ui import style

        class Row(dict):
            def __getitem__(self, k):
                return dict.get(self, k)

        payload = style.highlights_json([Row({
            "id": 1, "quote": "</script><img onerror=x>", "prefix": "",
            "suffix": "", "start_offset": 0, "note": ""})])
        self.assertNotIn("</script>", payload)


class TestUrlElision(unittest.TestCase):
    """The hovered-link line along the foot of the reader."""

    @staticmethod
    def _elide(uri):
        from chronicle.ui.style import elide_url
        return elide_url(uri)

    def test_short_urls_are_untouched(self):
        u = "https://example.com/a-post"
        self.assertEqual(self._elide(u), u)

    def test_long_urls_are_capped(self):
        from chronicle.ui.style import URL_MAX
        out = self._elide("https://example.com/" + "x" * 300)
        self.assertLessEqual(len(out), URL_MAX)
        self.assertTrue(out.endswith("\u2026"))

    def test_never_cuts_a_percent_escape_in_half(self):
        """A trailing "%E" renders as mojibake in the tooltip."""
        import re as _re
        for pad in range(60, 90):
            out = self._elide("https://e.com/" + "y" * pad + "%E2%80%99tail")
            self.assertIsNone(
                _re.search(r"%[0-9A-Fa-f]?$", out.rstrip("\u2026")),
                f"split escape at pad={pad}: {out!r}")


class TestNotePreview(unittest.TestCase):
    """The line of the reader's own writing shown under a queue row."""

    @staticmethod
    def _line(note=None, mark=None):
        from chronicle.ui.style import note_line
        return note_line({"note_body": note, "first_mark": mark})

    def test_nothing_written_gives_no_line(self):
        self.assertEqual(self._line(), "")
        self.assertEqual(self._line(note="", mark=""), "")

    def test_the_articles_own_note_wins(self):
        self.assertEqual(self._line(note="my note", mark="a quote"), "my note")

    def test_a_highlight_stands_in_when_there_is_no_note(self):
        """Quoted, because it is the article's words rather than the reader's."""
        self.assertEqual(self._line(mark="a quote"), "\u201ca quote\u201d")

    def test_whitespace_is_collapsed_to_one_line(self):
        self.assertEqual(self._line(note="two\n\nlines   here"),
                         "two lines here")

    def test_long_notes_are_trimmed(self):
        from chronicle.ui.style import NOTE_PREVIEW
        out = self._line(note="x" * 400)
        self.assertLessEqual(len(out), NOTE_PREVIEW)
        self.assertTrue(out.endswith("\u2026"))


class TestPageScroller(unittest.TestCase):
    """The reader hands its scrolling to the page, so the two must agree.

    The page animates the scroll itself and lands only on whole device
    pixels, which is what keeps the composited text off half-pixel offsets.
    Nothing here can exercise the animation -- that needs a browser -- but a
    rename on either side of the boundary is silent at runtime, because a
    call to a function the page does not define simply does nothing.
    """

    @staticmethod
    def _reader_source():
        from pathlib import Path
        import chronicle.ui as ui
        return (Path(ui.__file__).parent / "reader.py").read_text()

    def _script(self):
        from chronicle.ui.style import SCRIPT
        return SCRIPT

    def test_the_page_defines_every_scroller_the_reader_calls(self):
        script, source = self._script(), self._reader_source()
        for name in re.findall(r"window\.(chronicleScroll\w*)", source):
            self.assertIn(f"window.{name} = function", script,
                          f"reader.py calls {name}, which the page never defines")

    def test_the_reader_asks_for_a_scroll_and_never_drives_one(self):
        """Scrolling the window from out here would bypass the rounding."""
        source = self._reader_source()
        for name in ("window.scrollBy", "window.scrollTo"):
            self.assertFalse(name in source,
                             f"reader.py drives the scroll itself with {name}")

    def test_the_page_never_lands_on_a_fraction_of_a_pixel(self):
        """Every scrollTo in the page goes through the rounding."""
        for call in re.findall(r"window\.scrollTo\(([^;]*?)\);", self._script()):
            self.assertIn("wholePixel(", call, f"unrounded scroll: {call}")


class TestTimeRemaining(unittest.TestCase):
    """The reading time along the foot of the reader.

    Before you start it shows how long the article takes; once you are into
    it, how much is left. Long articles are the case that matters -- they are
    why the reader remembers a position at all.
    """

    @staticmethod
    def _t(total, fraction):
        from chronicle.ui.style import time_remaining
        return time_remaining(total, fraction)

    def test_unstarted_shows_the_whole_article(self):
        self.assertEqual(self._t(33, 0.0), "33 min")

    def test_a_nudge_off_the_top_is_still_unstarted(self):
        """Opening an article jitters the scroll a little; that is not progress."""
        from chronicle.ui.style import STARTED_THRESHOLD
        self.assertEqual(self._t(33, STARTED_THRESHOLD), "33 min")

    def test_partway_through_counts_down(self):
        self.assertEqual(self._t(33, 0.43), "19 min left")

    def test_time_left_falls_as_you_read(self):
        seen = [self._t(60, f) for f in (0.1, 0.3, 0.5, 0.7, 0.9)]
        minutes = [int(s.split()[0]) for s in seen]
        self.assertEqual(minutes, sorted(minutes, reverse=True))

    def test_the_last_screen_never_says_zero(self):
        """Rounding reaches 0 before the article ends; 0 min left is a lie."""
        self.assertEqual(self._t(33, 0.999), "under a minute left")
        for f in (0.985, 0.99, 0.995, 1.0):
            self.assertNotIn("0 min", self._t(33, f))

    def test_a_short_article_survives_the_whole_range(self):
        """A 1-minute article rounds to zero almost immediately."""
        for f in (0.0, 0.25, 0.5, 0.75, 1.0):
            out = self._t(1, f)
            self.assertNotIn("0 min", out)
            self.assertTrue(out.endswith("min") or out.endswith("left"), out)

    def test_an_unmeasured_article_does_not_count_down(self):
        """word_count can be 0; "0 min left" would be worse than the total."""
        self.assertEqual(self._t(0, 0.5), "0 min")


class TestResume(unittest.TestCase):
    """Picking up where the reader left off.

    The reader used to reopen at the top and then flush that fresh near-zero
    scroll back over the stored one, so every launch quietly erased the
    position it was meant to restore. These pin the round trip.
    """

    def setUp(self):
        from chronicle import db
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = db
        self.conn = db.connect(self.tmp.name)
        db.init(self.conn)
        self.sid = db.add_source(self.conn, "t", "Test", "generic",
                                 "https://t.example")

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def add(self, guid, iso="2010-01-01T00:00:00", order=0):
        aid, _ = self.db.upsert_article(
            self.conn, self.sid, guid, url=f"https://t.example/{guid}",
            title=guid, published_at=iso, date_precision="day",
            date_confidence="exact", date_source="test", source_order=order)
        self.conn.execute(
            "UPDATE articles SET content_status='ok', content_html='<p>x</p>' "
            "WHERE id=?", (aid,))
        return aid

    def test_scroll_survives_a_round_trip(self):
        aid = self.add("a")
        self.db.set_scroll(self.conn, aid, 0.43)
        row = self.conn.execute(
            "SELECT scroll_pos FROM reading_state WHERE article_id=?",
            (aid,)).fetchone()
        self.assertAlmostEqual(row["scroll_pos"], 0.43)

    def test_resume_returns_the_remembered_article(self):
        first = self.add("a", "2001-01-01T00:00:00", order=0)
        later = self.add("b", "2009-01-01T00:00:00", order=1)
        self.db.state_set(self.conn, "current_article_id", later)
        self.assertEqual(self.db.resume_article(self.conn)["id"], later,
                         "resumed the queue head instead of the open article")

    def test_resume_falls_back_to_the_first_unread(self):
        first = self.add("a", "2001-01-01T00:00:00", order=0)
        self.add("b", "2009-01-01T00:00:00", order=1)
        self.assertEqual(self.db.resume_article(self.conn)["id"], first)

    def test_an_unfetched_article_is_not_resumed(self):
        """A remembered article whose content never arrived cannot be shown."""
        good = self.add("a", "2001-01-01T00:00:00", order=0)
        bad = self.add("b", "2009-01-01T00:00:00", order=1)
        self.conn.execute(
            "UPDATE articles SET content_status='error' WHERE id=?", (bad,))
        self.db.state_set(self.conn, "current_article_id", bad)
        self.assertEqual(self.db.resume_article(self.conn)["id"], good)


class TestResumeScroll(unittest.TestCase):
    """Where reopening an article puts the reader, and whether it says so.

    Launch used to open at the top regardless of the stored position. That
    lost the place and then destroyed it: the reader reports the fresh
    near-zero scroll, and that gets flushed over the stored value on the way
    out, so each launch erased what it should have restored.
    """

    @staticmethod
    def _scroll(stored, remember=True):
        from chronicle.ui.style import resume_scroll
        return resume_scroll(stored, remember)

    @staticmethod
    def _hint(stored):
        from chronicle.ui.style import shows_resume_hint
        return shows_resume_hint(stored)

    def test_a_stored_position_is_restored(self):
        self.assertAlmostEqual(self._scroll(0.61), 0.61)

    def test_an_untouched_article_opens_at_the_top(self):
        self.assertEqual(self._scroll(0.0), 0.0)
        self.assertEqual(self._scroll(None), 0.0)

    def test_opening_deliberately_at_the_top_still_can(self):
        """Following a link into an article is not resuming it."""
        self.assertEqual(self._scroll(0.61, remember=False), 0.0)

    def test_a_position_outside_the_page_is_clamped(self):
        self.assertEqual(self._scroll(1.4), 1.0)
        self.assertEqual(self._scroll(-0.2), 0.0)

    def test_the_hint_explains_a_restored_position(self):
        self.assertTrue(self._hint(0.43))

    def test_no_hint_when_nothing_was_restored(self):
        """Saying so at the top would be noise -- that is where it opens anyway."""
        for stored in (None, 0.0, 0.001, 0.02):
            self.assertFalse(self._hint(stored), stored)

    def test_hint_and_scroll_agree(self):
        """A hint that appears without a restored position would be a lie."""
        for stored in (None, 0.0, 0.01, 0.02, 0.03, 0.5, 1.0):
            if self._hint(stored):
                self.assertGreater(self._scroll(stored), 0.0, stored)


class TestDictionaryWords(unittest.TestCase):
    """Which selections are worth offering a definition for."""

    @staticmethod
    def _word(text):
        from chronicle import dictionary
        return dictionary.normalise(text)

    def test_a_single_word_is_a_lookup(self):
        self.assertEqual(self._word("quixotic"), "quixotic")
        self.assertEqual(self._word("  Quixotic  "), "quixotic")

    def test_the_punctuation_a_selection_drags_in_is_dropped(self):
        for text in ('"quixotic,"', "(quixotic)", "quixotic.", "‘quixotic’"):
            self.assertEqual(self._word(text), "quixotic", text)

    def test_a_possessive_is_looked_up_as_the_word_under_it(self):
        self.assertEqual(self._word("reader's"), "reader")
        self.assertEqual(self._word("reader’s"), "reader")

    def test_punctuation_inside_a_word_is_kept(self):
        self.assertEqual(self._word("ne'er-do-well"), "ne'er-do-well")

    def test_a_phrase_is_not_a_lookup(self):
        """No dictionary has an entry for a sentence; offering one would lie."""
        for text in ("the most quixotic", "quixotic\nreader", "", "   "):
            self.assertIsNone(self._word(text))

    def test_things_that_are_not_words_are_not_looked_up(self):
        for text in ("1832", "£40", "—", "http://example.com"):
            self.assertIsNone(self._word(text), text)


class TestDictionaryEntries(unittest.TestCase):
    """Folding Wiktionary's answer into one card's worth."""

    SAMPLE = {
        "en": [
            {"partOfSpeech": "Adjective", "language": "English",
             "definitions": [
                 {"definition": '<span class="usage-label-sense"></span> '
                                'Possessing   or  acting with\n'
                                '<a href="/wiki/idealism">idealism</a>.',
                  "examples": ["a quixotic  scheme"]},
                 {"definition": "Impulsive &amp; rash."},
                 {"definition": "A third sense, beyond what a card shows."},
             ]},
            {"partOfSpeech": "Noun",
             "definitions": [{"definition": "One who is quixotic."}]},
        ],
        # Same spelling in another language; the card is an English one.
        "la": [{"partOfSpeech": "Verb",
                "definitions": [{"definition": "Latin, and not wanted here."}]}],
    }

    def _parse(self, payload=None, word="quixotic"):
        from chronicle import dictionary
        return dictionary.parse(word, self.SAMPLE if payload is None else payload)

    def test_the_card_carries_the_word_and_its_senses(self):
        entry = self._parse()
        self.assertEqual(entry["word"], "quixotic")
        self.assertEqual(entry["senses"][0]["pos"], "adjective")
        self.assertEqual(entry["senses"][0]["example"], "a quixotic scheme")

    def test_markup_and_whitespace_from_wiktionary_are_stripped(self):
        """Definitions arrive as page fragments; the card sets them as prose."""
        senses = self._parse()["senses"]
        self.assertEqual(senses[0]["definition"],
                         "Possessing or acting with idealism.")
        self.assertEqual(senses[1]["definition"], "Impulsive & rash.")

    def test_only_the_english_entry_is_used(self):
        """A word spelled the same in Latin brings the Latin entry back too."""
        for sense in self._parse()["senses"]:
            self.assertNotIn("Latin", sense["definition"])

    def test_senses_are_capped_but_span_the_parts_of_speech(self):
        from chronicle import dictionary
        senses = self._parse()["senses"]
        self.assertLessEqual(len(senses), dictionary.MAX_SENSES)
        self.assertIn("noun", [s["pos"] for s in senses])

    def test_a_repeated_definition_is_shown_once(self):
        payload = {"en": [
            {"partOfSpeech": "Noun", "definitions": [{"definition": "A thing."}]},
            {"partOfSpeech": "Verb", "definitions": [{"definition": "A thing."}]},
        ]}
        self.assertEqual(len(self._parse(payload, "x")["senses"]), 1)

    def test_a_sense_that_is_only_markup_is_dropped(self):
        payload = {"en": [{"partOfSpeech": "Noun", "definitions": [
            {"definition": '<span class="usage-label-sense"></span>'},
            {"definition": "A real one."}]}]}
        self.assertEqual([s["definition"] for s in self._parse(payload, "x")["senses"]],
                         ["A real one."])

    def test_nonsense_from_the_endpoint_yields_an_empty_entry(self):
        """An unreadable answer must read as 'no entry', never as a crash."""
        for payload in ({}, [], {"en": None}, {"en": [None]},
                        {"en": [{"definitions": "not a list"}]}):
            entry = self._parse(payload, "x")
            self.assertEqual(entry["senses"], [], payload)
            self.assertEqual(entry["word"], "x")


class TestDictionaryCache(unittest.TestCase):
    """A word looked up once stays readable with the network off."""

    def setUp(self):
        from chronicle import db
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = db
        self.conn = db.connect(self.tmp.name)
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_an_entry_roundtrips(self):
        from chronicle import dictionary
        self.assertIsNone(dictionary.cached(self.conn, "quixotic"))
        self.db.set_definition(self.conn, "quixotic",
                               '{"word": "quixotic", "senses": []}')
        self.assertEqual(dictionary.cached(self.conn, "quixotic")["word"],
                         "quixotic")

    def test_a_word_with_no_entry_is_remembered_as_such(self):
        """'No such word' is an answer; asking again only gets it slower."""
        from chronicle import dictionary
        dictionary.remember(self.conn, "zzzzzz", {"word": "zzzzzz", "senses": []})
        self.assertEqual(dictionary.cached(self.conn, "zzzzzz")["senses"], [])

    def test_remembering_a_word_twice_keeps_the_later_answer(self):
        from chronicle import dictionary
        dictionary.remember(self.conn, "x", {"word": "x", "senses": []})
        dictionary.remember(self.conn, "x", {"word": "x", "senses": [{"pos": "noun"}]})
        self.assertEqual(len(dictionary.cached(self.conn, "x")["senses"]), 1)

    def test_a_corrupt_cache_row_is_treated_as_a_miss(self):
        from chronicle import dictionary
        self.db.set_definition(self.conn, "quixotic", "{not json")
        self.assertIsNone(dictionary.cached(self.conn, "quixotic"))
