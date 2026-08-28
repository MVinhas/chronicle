"""The library view: the unified chronological queue."""
from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .. import dates, db  # noqa: E402
from .style import note_line, reading_minutes  # noqa: E402

PAGE_SIZE = 400


class RowItem(GObject.Object):
    """One line in the queue — either a year heading or an article."""

    __gtype_name__ = "ChronicleRowItem"

    def __init__(self, kind: str, label: str = "", row=None):
        super().__init__()
        self.kind = kind          # 'header' | 'article'
        self.label = label
        self.row = row
        self.article_id = row["id"] if row is not None else 0


class LibraryView(Gtk.Box):
    """Search, filter and pick from the whole archive in date order."""

    __gtype_name__ = "ChronicleLibraryView"

    __gsignals__ = {
        "article-chosen": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "hide-read-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, get_conn):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_conn = get_conn
        self.scope = "all"
        self.search_text = ""
        self.hide_read = db.state_get(get_conn(), "hide_read", "0") == "1"
        self._offset = 0
        self._exhausted = False

        self.store = Gio.ListStore(item_type=RowItem)
        self._build_ui()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        header_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                               margin_start=18, margin_end=18,
                               margin_top=10, margin_bottom=6, spacing=10)
        header = Adw.Clamp(maximum_size=900, child=header_inner)

        self.search = Gtk.SearchEntry(placeholder_text="Search titles…",
                                      hexpand=True)
        self.search.connect("search-changed", self._on_search)
        header_inner.append(self.search)

        switcher = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self._buttons: dict[str, Gtk.ToggleButton] = {}
        first = None
        for scope, label in (("all", "All"), ("unread", "Unread"),
                             ("favourites", "Favourites"), ("annotated", "Notes"),
                             ("read", "Read"), ("skipped", "Skipped")):
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("chronicle-filter")
            if first is None:
                first = btn
                btn.set_active(True)
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_scope, scope)
            self._buttons[scope] = btn
            switcher.append(btn)

        self.hide_read_button = Gtk.ToggleButton(
            icon_name="view-conceal-symbolic", active=self.hide_read,
            tooltip_text="Hide articles you have already read",
            css_classes=["chronicle-filter"], margin_start=10)
        self.hide_read_button.connect("toggled", self._on_hide_read)
        switcher.append(self.hide_read_button)
        header_inner.append(switcher)

        self.summary = Gtk.Label(xalign=0.5, css_classes=["dim-label", "caption"])
        header_inner.append(self.summary)
        self.append(header)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_row)
        factory.connect("bind", self._bind_row)

        self.selection = Gtk.NoSelection(model=self.store)
        self.listview = Gtk.ListView(model=self.selection, factory=factory,
                                     vexpand=True, single_click_activate=True)
        self.listview.add_css_class("navigation-sidebar")
        self.listview.connect("activate", self._on_activate)

        # The ListView must be the ScrolledWindow's direct child. Wrapped in a
        # Clamp it is no longer the scrollable, so GTK allocates the whole
        # list as one giant widget — and anything past GTK's ~32k-pixel
        # allocation limit silently stops rendering, which a large library
        # hits mid-scroll as a blank screen. Width is clamped per row instead.
        self.scroller = Gtk.ScrolledWindow(vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroller.set_child(self.listview)
        self.scroller.get_vadjustment().connect("value-changed", self._maybe_load_more)

        self.empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="Nothing here yet",
            description="Add a blog from the Blogs tab, then build its archive "
                        "to start reading.")

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.scroller, "list")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

    # -- row rendering -----------------------------------------------------

    def _setup_row(self, _factory, item) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14,
                      margin_start=16, margin_end=16,
                      margin_top=7, margin_bottom=7)

        date = Gtk.Label(xalign=0, width_chars=11, css_classes=["dim-label",
                                                               "numeric", "caption"])
        date.set_valign(Gtk.Align.START)
        box.append(date)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                          lines=2, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        title.set_max_width_chars(64)
        meta = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"],
                         ellipsize=Pango.EllipsizeMode.END)
        # What the reader wrote, shown only on rows that have something. It
        # sits below the meta line and is styled apart from it, so a queue of
        # ordinary articles looks exactly as it did before.
        note = Gtk.Label(xalign=0, css_classes=["caption", "chronicle-note"],
                         ellipsize=Pango.EllipsizeMode.END, visible=False)
        note.set_max_width_chars(64)
        text.append(title)
        text.append(meta)
        text.append(note)
        box.append(text)

        star = Gtk.Image(icon_name="starred-symbolic", css_classes=["accent"])
        star.set_valign(Gtk.Align.CENTER)
        box.append(star)

        heading = Gtk.Label(xalign=0, css_classes=["heading"],
                            margin_start=16, margin_top=20, margin_bottom=4)

        wrapper = Gtk.Stack()
        wrapper.add_named(box, "article")
        wrapper.add_named(heading, "header")
        item.set_child(Adw.Clamp(maximum_size=900, child=wrapper))
        item._parts = (wrapper, date, title, meta, note, star, heading)

    def _bind_row(self, _factory, item) -> None:
        wrapper, date, title, meta, note, star, heading = item._parts
        obj = item.get_item()

        if obj.kind == "header":
            note.set_visible(False)
            heading.set_label(obj.label)
            wrapper.set_visible_child_name("header")
            item.set_activatable(False)
            item.set_selectable(False)
            return

        wrapper.set_visible_child_name("article")
        item.set_activatable(True)
        row = obj.row

        date.set_label(dates.format_short(row["published_at"], row["date_precision"]))
        if row["date_confidence"] in ("inferred", "unknown"):
            date.set_tooltip_text(dates.CONFIDENCE_NOTE.get(row["date_confidence"], ""))
            date.add_css_class("warning")
        else:
            date.set_tooltip_text(None)
            date.remove_css_class("warning")

        title.set_label(row["title"] or "Untitled")
        if row["read_at"]:
            title.add_css_class("dim-label")
        else:
            title.remove_css_class("dim-label")

        bits = [row["source_name"]]
        if row["word_count"]:
            bits.append(f"{reading_minutes(row['word_count'])} min")
        if row["image_count"]:
            bits.append(f"{row['image_count']} image" +
                        ("s" if row["image_count"] != 1 else ""))
        if row["content_status"] == "paywalled":
            bits.append("partial — paywalled")
        # What the reader left behind, so an annotated article is findable in
        # the queue without opening it.
        marks = row["highlight_count"] or 0
        if marks:
            bits.append(f"{marks} highlight" + ("s" if marks != 1 else ""))
        if row["note_count"]:
            bits.append("noted")
        if row["skipped_at"]:
            bits.append("skipped")
        meta.set_label("  ·  ".join(bits))

        # Rows are recycled as the list scrolls, so this has to be cleared on
        # articles without one -- otherwise a note bleeds onto a later row.
        written = note_line(row)
        note.set_label(written)
        note.set_visible(bool(written))
        note.set_tooltip_text(written or None)

        star.set_visible(bool(row["favourite_at"]))

    # -- data --------------------------------------------------------------

    def reload(self) -> None:
        self._offset = 0
        self._exhausted = False
        self.store.remove_all()
        self._last_year = None
        self._load_page()
        self._update_summary()
        empty = not self.store.get_n_items()
        if empty:
            self._describe_empty()
        self.stack.set_visible_child_name("empty" if empty else "list")

    def _describe_empty(self) -> None:
        """Say why the list is empty, which depends on what was asked for.

        An empty library and an empty filter are different situations, and
        "add a blog" is unhelpful advice for someone who has simply not
        written any notes yet.
        """
        if self.search_text:
            self.empty.set_icon_name("system-search-symbolic")
            self.empty.set_title("No matches")
            self.empty.set_description(
                f"Nothing in the library matches “{self.search_text}”.")
            return
        if db.queue_counts(self.get_conn())["all"] == 0:
            self.empty.set_icon_name("document-open-recent-symbolic")
            self.empty.set_title("Nothing here yet")
            self.empty.set_description(
                "Add a blog from the Blogs tab, then build its archive "
                "to start reading.")
            return
        blurb = {
            "annotated": ("No notes yet",
                          "Notes and highlights you leave while reading "
                          "collect here. Select any passage in an article to "
                          "highlight it, or write a note at the foot of one."),
            "favourites": ("No favourites yet",
                           "Press F while reading, or the star in the "
                           "reader's bottom bar, to keep an article here."),
            "unread": ("Nothing unread",
                       "You have read everything in the queue."),
            "read": ("Nothing read yet",
                     "Articles you finish collect here."),
            "skipped": ("Nothing skipped",
                        "Press S while reading to pass an article over. "
                        "Skipped articles leave the queue and collect here, "
                        "where you can put any of them back."),
        }.get(self.scope)
        if blurb is None:
            blurb = ("Nothing here", "No articles match this filter.")
        self.empty.set_icon_name({
            "annotated": "format-text-rich-symbolic",
            "skipped": "go-jump-symbolic",
        }.get(self.scope, "view-list-symbolic"))
        self.empty.set_title(blurb[0])
        self.empty.set_description(blurb[1])

    def refresh_if_at_top(self) -> None:
        """Pick up newly-synced articles, but never while mid-scroll.

        Called every time a background sync finishes, which can be many times
        during "Update all" -- a plain reload() wipes the store back to page
        one and resets scroll position each time, which from the reader's
        side looks exactly like the list "stopped loading" partway down.
        Skipping the refresh while scrolled costs nothing: the next visit to
        the top (or the next explicit reload()) catches up anyway.
        """
        if self.scroller.get_vadjustment().get_value() > 0:
            self._update_summary()
            return
        self.reload()

    def _load_page(self) -> None:
        if self._exhausted:
            return
        conn = self.get_conn()
        rows = db.queue(conn, scope=self.scope, limit=PAGE_SIZE,
                        offset=self._offset, search=self.search_text or None,
                        hide_read=self.hide_read)
        if len(rows) < PAGE_SIZE:
            self._exhausted = True
        self._offset += len(rows)

        additions = []
        for row in rows:
            year = self._year_label(row)
            if year != getattr(self, "_last_year", None):
                additions.append(RowItem("header", year))
                self._last_year = year
            additions.append(RowItem("article", row=row))
        for item in additions:
            self.store.append(item)

    @staticmethod
    def _year_label(row) -> str:
        if not row["published_at"]:
            return "Undated"
        return str(datetime.fromisoformat(row["published_at"]).year)

    def _maybe_load_more(self, adj) -> None:
        if self._exhausted:
            return
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 600:
            self._load_page()

    def _update_summary(self) -> None:
        counts = db.queue_counts(self.get_conn())
        text = (f"{counts['all']:,} articles  ·  {counts['unread']:,} unread  ·  "
                f"{counts['favourites']:,} favourites").replace(",", " ")
        if counts["annotated"]:
            text += f"  ·  {counts['annotated']:,} with notes".replace(",", " ")
        if counts["skipped"]:
            text += f"  ·  {counts['skipped']:,} skipped".replace(",", " ")
        if counts["undated"]:
            text += f"  ·  {counts['undated']} undated"
        if self.hide_read and self.scope not in db.HIDE_READ_EXEMPT:
            text += "  ·  read articles hidden"
        self.summary.set_label(text)

    # -- events ------------------------------------------------------------

    def _on_search(self, entry) -> None:
        self.search_text = entry.get_text().strip()
        self.reload()

    def _on_scope(self, button, scope) -> None:
        if button.get_active():
            self.scope = scope
            # Greyed out where it does not apply, rather than left looking
            # active while quietly doing nothing.
            self.hide_read_button.set_sensitive(
                scope not in db.HIDE_READ_EXEMPT)
            self.reload()

    def _on_hide_read(self, button) -> None:
        self.hide_read = button.get_active()
        db.state_set(self.get_conn(), "hide_read", "1" if self.hide_read else "0")
        self.reload()
        self.emit("hide-read-changed")

    def _on_activate(self, _view, position) -> None:
        obj = self.store.get_item(position)
        if obj is not None and obj.kind == "article":
            self.emit("article-chosen", obj.article_id)

    def focus_search(self) -> None:
        self.search.grab_focus()
