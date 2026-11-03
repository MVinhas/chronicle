"""Reading direction: oldest-first (default) or newest-first ("read backwards").

Run: tools/run-tests.sh
"""
from __future__ import annotations

import os
import tempfile
import unittest


class TestReadingDirection(unittest.TestCase):
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

    def add(self, guid, iso, order=0):
        aid, _ = self.db.upsert_article(
            self.conn, self.sid, guid, url=f"https://t.example/{guid}",
            title=guid, published_at=iso,
            date_precision="day" if iso else "unknown",
            date_confidence="exact" if iso else "unknown",
            date_source="test", source_order=order)
        self.conn.execute(
            "UPDATE articles SET content_status='ok', content_html='<p>x</p>' "
            "WHERE id=?", (aid,))
        return aid

    def messy(self):
        """Dated, tied and undated articles, added out of order."""
        ids = {}
        ids["c"] = self.add("c", "2003-01-01T00:00:00")
        ids["a"] = self.add("a", "2001-01-01T00:00:00")
        ids["t1"] = self.add("t1", "2002-01-01T00:00:00", order=1)
        ids["t2"] = self.add("t2", "2002-01-01T00:00:00", order=2)
        ids["t3"] = self.add("t3", "2002-01-01T00:00:00", order=2)  # id tie
        ids["u1"] = self.add("u1", None, order=1)
        ids["u2"] = self.add("u2", None, order=2)
        return ids

    def names(self, rows):
        return [r["title"] for r in rows]

    def test_queue_newest_first_keeps_undated_last(self):
        self.messy()
        got = self.names(self.db.queue(self.conn, newest_first=True))
        self.assertEqual(got, ["c", "t3", "t2", "t1", "a", "u2", "u1"])

    def test_neighbour_matches_the_listing_in_both_directions(self):
        ids = self.messy()
        for newest in (False, True):
            rows = self.db.queue(self.conn, newest_first=newest)
            order = [r["id"] for r in rows]
            for i, aid in enumerate(order):
                nxt = self.db.neighbour(self.conn, aid, +1, newest_first=newest)
                prv = self.db.neighbour(self.conn, aid, -1, newest_first=newest)
                self.assertEqual(nxt["id"] if nxt else None,
                                 order[i + 1] if i + 1 < len(order) else None,
                                 f"next of #{i} newest_first={newest}")
                self.assertEqual(prv["id"] if prv else None,
                                 order[i - 1] if i else None,
                                 f"prev of #{i} newest_first={newest}")
                self.assertEqual(self.db.position_in_queue(
                    self.conn, aid, newest_first=newest),
                    (i + 1, len(order)))

    def test_next_is_the_older_article_when_reading_backwards(self):
        ids = self.messy()
        self.assertEqual(self.db.neighbour(
            self.conn, ids["c"], +1, newest_first=True)["id"], ids["t3"])
        self.assertEqual(self.db.neighbour(
            self.conn, ids["a"], +1, newest_first=True)["id"], ids["u2"])
        self.assertIsNone(self.db.neighbour(
            self.conn, ids["u1"], +1, newest_first=True))
        self.assertIsNone(self.db.neighbour(
            self.conn, ids["c"], -1, newest_first=True))

    def test_default_is_still_oldest_first(self):
        ids = self.messy()
        self.assertEqual(self.db.neighbour(self.conn, ids["a"], +1)["id"],
                         ids["t1"])
        self.assertEqual(self.db.neighbour(self.conn, ids["u1"], -1)["id"],
                         ids["c"])

    def test_neighbour_of_a_deleted_row_in_reverse(self):
        ids = self.messy()
        held = self.db.get_article(self.conn, ids["t2"])
        self.conn.execute("DELETE FROM articles WHERE id=?", (ids["t2"],))
        self.assertEqual(self.db.neighbour_of(
            self.conn, held, +1, newest_first=True)["id"], ids["t1"])
        self.assertEqual(self.db.neighbour_of(
            self.conn, held, -1, newest_first=True)["id"], ids["t3"])

    def test_hide_read_interplay(self):
        ids = self.messy()
        self.db.set_read(self.conn, ids["t2"], True)
        self.db.set_read(self.conn, ids["t3"], True)
        # Reading backwards from c, the read ties are skipped over.
        self.assertEqual(self.db.neighbour(
            self.conn, ids["c"], +1, hide_read=True,
            newest_first=True)["id"], ids["t1"])
        # The article just read is itself hidden: position still resolves.
        self.assertEqual(self.db.neighbour(
            self.conn, ids["t3"], +1, hide_read=True,
            newest_first=True)["id"], ids["t1"])
        self.assertEqual(self.db.neighbour(
            self.conn, ids["t3"], -1, hide_read=True,
            newest_first=True)["id"], ids["c"])

    def test_resume_picks_newest_unread_when_reading_backwards(self):
        ids = self.messy()
        self.db.set_read(self.conn, ids["c"], True)
        self.assertEqual(self.db.resume_article(
            self.conn, newest_first=True)["id"], ids["t3"])
        self.assertEqual(self.db.resume_article(self.conn)["id"], ids["a"])

    def test_resume_prefers_the_remembered_article_in_both_modes(self):
        ids = self.messy()
        self.db.state_set(self.conn, "current_article_id", ids["t1"])
        for newest in (False, True):
            self.assertEqual(self.db.resume_article(
                self.conn, newest_first=newest)["id"], ids["t1"])

    def test_the_setting_persists_and_defaults_off(self):
        self.assertFalse(self.db.reading_newest_first(self.conn))
        self.db.set_reading_newest_first(self.conn, True)
        self.assertTrue(self.db.reading_newest_first(self.conn))
        self.db.set_reading_newest_first(self.conn, False)
        self.assertFalse(self.db.reading_newest_first(self.conn))

    def test_cli_order_flag_overrides_the_saved_direction(self):
        from unittest import mock
        import contextlib, io
        from chronicle import cli
        self.add("old", "2001-01-01T00:00:00")
        self.add("new", "2003-01-01T00:00:00")

        def titles(argv):
            out = io.StringIO()
            with mock.patch.object(self.db, "get_conn", return_value=self.conn), \
                    contextlib.redirect_stdout(out):
                cli.main(argv)
            return [l.split()[-1] for l in out.getvalue().splitlines()
                    if l.strip() and " shown (" not in l]

        self.assertEqual(titles(["queue"]), ["old", "new"])
        self.db.set_reading_newest_first(self.conn, True)
        self.assertEqual(titles(["queue"]), ["new", "old"])
        self.assertEqual(titles(["queue", "--order", "oldest"]), ["old", "new"])
        self.db.set_reading_newest_first(self.conn, False)
        self.assertEqual(titles(["queue", "--order", "newest"]), ["new", "old"])


if __name__ == "__main__":
    unittest.main()

