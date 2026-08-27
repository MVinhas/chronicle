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
