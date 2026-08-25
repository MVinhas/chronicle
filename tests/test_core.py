"""Unit tests for the parts that decide reading order and reading quality.

Run: tools/run-tests.sh
"""
from __future__ import annotations

import os
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

    def test_disabled_source_leaves_the_queue(self):
        self.add("a", "2001-01-01T00:00:00")
        self.db.set_source_enabled(self.conn, self.sid, False)
        self.assertEqual(self.db.queue_counts(self.conn)["all"], 0)

    def test_search(self):
        self.add("a", "2001-01-01T00:00:00", title="Beating the Averages")
        self.add("b", "2002-01-01T00:00:00", title="Taste for Makers")
        hits = self.db.queue(self.conn, search="averages")
        self.assertEqual([r["title"] for r in hits], ["Beating the Averages"])


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
