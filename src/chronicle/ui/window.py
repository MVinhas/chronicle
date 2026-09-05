"""The main window: reader, library and blog management."""
from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import __version__, dates, db, sync  # noqa: E402
from .library import LibraryView  # noqa: E402
from .reader import ReaderView  # noqa: E402
from .sources_view import SourcesView  # noqa: E402
from .style import (elide_url, reading_minutes,  # noqa: E402
                    resume_scroll, shows_resume_hint, time_remaining)

log = logging.getLogger("chronicle.window")

# Reading past this fraction of an article counts as having read it.
READ_THRESHOLD = 0.92

# How long the bottom bar says it put you back where you were, before it
# returns to the ordinary position line.
RESUME_HINT_SECONDS = 6

# A title long enough to know which article the toast means, short enough not
# to push its Undo button off a narrow window.
TOAST_TITLE_CHARS = 48


def _shorten(text: str, limit: int = TOAST_TITLE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


APP_CSS = b"""
.chronicle-filter { padding: 2px 14px; min-height: 26px; }
.chronicle-position { font-size: 0.82em; }
.chronicle-reader-title { font-weight: 600; }
/* The reader's own writing in the queue: italic and accented, so it reads as
   a different voice from the article's metadata rather than more of it. */
.chronicle-note {
  font-style: italic;
  opacity: 0.85;
  margin-top: 1px;
}
"""


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "ChronicleWindow"

    def __init__(self, app):
        super().__init__(application=app, title="Chronicle",
                         default_width=1120, default_height=820)
        self.set_size_request(560, 480)
        self._conn = db.get_conn()
        self.current = None
        self._scroll_frac = 0.0
        self._marked_read = False
        self._reader_editing = False
        self._hovered_link = ""
        self._position_text = ""
        self._position_prefix = ""
        self._total_minutes = 0
        self._resume_hint_id = 0

        self.syncer = sync.Syncer(on_progress=self._on_sync_progress)

        self._load_css()
        self._build_ui()
        self._follow_color_scheme()
        self._install_actions()
        self.refresh_library()
        self.resume()

    # -- infrastructure ----------------------------------------------------

    def conn(self):
        return self._conn

    @property
    def hide_read(self) -> bool:
        """Shared with the library view, so the queue and the reader agree."""
        return db.state_get(self._conn, "hide_read", "0") == "1"

    @staticmethod
    def _load_css() -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(APP_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # -- layout ------------------------------------------------------------

    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()

        self.stack.add_titled_with_icon(
            self._build_reader_page(), "reader", "Read", "format-text-rich-symbolic")
        self.stack.add_titled_with_icon(
            self._build_library_page(), "library", "Library", "view-list-symbolic")
        self.stack.add_titled_with_icon(
            self._build_sources_page(), "sources", "Blogs", "user-bookmarks-symbolic")

        switcher = Adw.ViewSwitcher(stack=self.stack,
                                    policy=Adw.ViewSwitcherPolicy.WIDE)
        self.header = Adw.HeaderBar(title_widget=switcher)

        theme_menu = Gio.Menu()
        theme_menu.append("Follow system", "win.theme::system")
        theme_menu.append("Light", "win.theme::light")
        theme_menu.append("Dark", "win.theme::dark")

        menu = Gio.Menu()
        menu.append("Fetch new posts", "win.sync")
        menu.append("Full archive scan", "win.full-scan")
        menu.append("Open original in browser", "win.open-external")
        menu.append_section("Appearance", theme_menu)
        extras = Gio.Menu()
        extras.append("Keyboard shortcuts", "win.shortcuts")
        extras.append("About Chronicle", "win.about")
        menu.append_section(None, extras)
        self.header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic",
                                            menu_model=menu, tooltip_text="Menu"))

        self.sync_button = Gtk.Button(icon_name="view-refresh-symbolic",
                                      tooltip_text="Fetch new posts (F5)",
                                      action_name="win.sync")
        self.header.pack_start(self.sync_button)

        outer = Adw.ToolbarView()
        outer.add_top_bar(self.header)
        outer.set_content(self.stack)
        self.toasts.set_child(outer)
        self.set_content(self.toasts)

        self.stack.connect("notify::visible-child-name", self._on_page_changed)

    def _follow_color_scheme(self) -> None:
        """Track the desktop light/dark preference and repaint the reader."""
        manager = Adw.StyleManager.get_default()

        def apply(*_args):
            self.reader.set_dark(manager.get_dark())

        manager.connect("notify::dark", apply)
        apply()

    def _build_reader_page(self) -> Gtk.Widget:
        self.reader = ReaderView()
        self.reader.connect("scrolled", self._on_scrolled)
        self.reader.connect("link-activated", self._on_link)
        self.reader.connect("note-requested", self._on_highlight_note)
        self.reader.connect("editing", self._on_reader_editing)
        self.reader.connect("hovering-link", self._on_hovering_link)
        self.reader.connect("annotations-changed",
                            lambda *_: self.refresh_library())

        view = Adw.ToolbarView()
        view.set_content(self.reader)

        bar = Gtk.CenterBox(margin_start=10, margin_end=10,
                            margin_top=5, margin_bottom=5)

        self.prev_button = Gtk.Button(icon_name="go-previous-symbolic",
                                      tooltip_text="Previous article (←)",
                                      action_name="win.previous",
                                      css_classes=["flat"])
        self.next_button = Gtk.Button(icon_name="go-next-symbolic",
                                      tooltip_text="Next article (→)",
                                      action_name="win.next",
                                      css_classes=["flat"])

        left = Gtk.Box(spacing=4)
        left.append(self.prev_button)
        self.fav_button = Gtk.ToggleButton(icon_name="non-starred-symbolic",
                                           tooltip_text="Favourite (F)",
                                           css_classes=["flat"])
        self.fav_button.connect("toggled", self._on_favourite_toggled)
        left.append(self.fav_button)

        self.read_button = Gtk.ToggleButton(icon_name="object-select-symbolic",
                                            tooltip_text="Mark as read (R)",
                                            css_classes=["flat"])
        self.read_button.connect("toggled", self._on_read_toggled)
        left.append(self.read_button)

        # Skip sits with the other judgements about the article rather than
        # with the arrows: it is a decision, not a movement, even though it
        # happens to move you on.
        self.skip_button = Gtk.Button(icon_name="go-jump-symbolic",
                                      tooltip_text="Skip this article (S)",
                                      css_classes=["flat"])
        self.skip_button.connect("clicked", lambda *_: self.skip_article())
        left.append(self.skip_button)

        right = Gtk.Box(spacing=4)
        right.append(self.next_button)

        self.position_label = Gtk.Label(css_classes=["dim-label",
                                                     "chronicle-position"])
        bar.set_start_widget(left)
        bar.set_center_widget(self.position_label)
        bar.set_end_widget(right)
        view.add_bottom_bar(bar)
        return view

    def _build_library_page(self) -> Gtk.Widget:
        self.library = LibraryView(self.conn)
        self.library.connect("article-chosen", self._on_article_chosen)
        self.library.connect(
            "hide-read-changed",
            lambda *_: self._update_reader_chrome(self.current) if self.current else None)
        return self.library

    def _build_sources_page(self) -> Gtk.Widget:
        self.sources_view = SourcesView(self.conn, self)
        self.sources_view.connect("sync-requested", self._on_sync_requested)
        self.sources_view.connect("cancel-requested", lambda *_: self.syncer.cancel())
        self.sources_view.connect("library-changed", lambda *_: self.refresh_library())
        return self.sources_view

    # -- actions -----------------------------------------------------------

    def _install_actions(self) -> None:
        # Accelerators that need no modifier are suspended while a text field
        # has focus -- otherwise typing "n" in the search box jumps to the next
        # article instead of entering a letter, because a window accelerator is
        # matched before the focused widget ever sees the key.
        specs = [
            ("next", self.go_next, ["<Alt>Right"], ["n", "j", "Page_Down"]),
            ("previous", self.go_previous, ["<Alt>Left"], ["p", "k", "Page_Up"]),
            ("favourite", self.toggle_favourite, [], ["f"]),
            ("toggle-read", self.toggle_read, [], ["r"]),
            ("skip", self.skip_article, [], ["s"]),
            ("library", lambda *_: self.show_page("library"), [], ["l"]),
            ("reader", lambda *_: self.show_page("reader"), ["<Control>1"], []),
            ("sources", lambda *_: self.show_page("sources"), ["<Control>2"], []),
            ("search", self.focus_search, ["<Control>f"], ["slash"]),
            ("sync", self.start_sync, ["F5", "<Control>r"], []),
            ("full-scan", self.start_full_scan, ["<Shift>F5"], []),
            ("open-external", self.open_external, ["<Control>o"], []),
            ("scroll-down", lambda *_: self.reader.scroll_by_page(1), [], ["space"]),
            ("scroll-up", lambda *_: self.reader.scroll_by_page(-1), [], ["<Shift>space"]),
            ("top", lambda *_: self.reader.scroll_home(), [], ["Home"]),
            ("hide-read", self.toggle_hide_read, [], ["h"]),
            ("cycle-theme", self.cycle_theme, ["<Control>t"], []),
            ("shortcuts", self.show_shortcuts, ["<Control>question"], []),
            ("escape", self.on_escape, ["Escape"], []),
            ("about", self.show_about, [], []),
        ]
        app = self.get_application()
        self._install_theme_action()
        self._bare_accels: dict[str, list[str]] = {}

        for name, handler, modified, bare in specs:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            detailed = f"win.{name}"
            if bare:
                self._bare_accels[detailed] = bare
            if modified or bare:
                app.set_accels_for_action(detailed, modified + bare)

        self.connect("notify::focus-widget", self._on_focus_changed)

    # -- keyboard focus ----------------------------------------------------

    def _is_text_entry(self, widget) -> bool:
        return isinstance(widget, (Gtk.Editable, Gtk.TextView))

    def _typing(self) -> bool:
        """Is the user entering text anywhere -- GTK widget or inside a page?

        A focused GtkEntry is visible to GTK. A focused <textarea> in the
        reader is not: the focused widget there is the WebView as a whole,
        so the page reports it and the reader passes it along.
        """
        return self._is_text_entry(self.get_focus()) or self._reader_editing

    def _on_focus_changed(self, *_args) -> None:
        self._set_bare_accels(not self._typing())

    def _on_reader_editing(self, _reader, editing: bool) -> None:
        self._reader_editing = editing
        self._set_bare_accels(not self._typing())

    def _set_bare_accels(self, enabled: bool) -> None:
        app = self.get_application()
        if app is None:
            return
        for detailed, bare in self._bare_accels.items():
            modified = [a for a in app.get_accels_for_action(detailed)
                        if a not in bare]
            app.set_accels_for_action(detailed, modified + (bare if enabled else []))

    def on_escape(self, *_args) -> None:
        """Leave the search field first; only then leave the page."""
        # Escape keeps its accelerator while the note box has focus (it is
        # registered as a modified accel, not a bare one), so it arrives here
        # rather than at the page. Hand it back: blurring the textarea saves
        # the note and re-arms the single-key shortcuts.
        if self._reader_editing:
            self.reader.blur_editor()
            return
        focus = self.get_focus()
        if self._is_text_entry(focus):
            if hasattr(self, "library") and focus is self.library.search:
                if self.library.search.get_text():
                    self.library.search.set_text("")
                    return
            self.set_focus(None)
            return
        self.show_page("library")

    # Follow system / light / dark, remembered between sessions.
    THEMES = ("system", "light", "dark")
    _SCHEMES = {
        "system": Adw.ColorScheme.DEFAULT,
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
    }

    def _install_theme_action(self) -> None:
        current = db.state_get(self._conn, "theme", "system")
        if current not in self.THEMES:
            current = "system"
        action = Gio.SimpleAction.new_stateful(
            "theme", GLib.VariantType.new("s"), GLib.Variant("s", current))
        action.connect("activate", self._on_theme)
        self.add_action(action)
        self._theme_action = action
        self._apply_theme(current)

    def _on_theme(self, action, param) -> None:
        name = param.get_string()
        action.set_state(param)
        db.state_set(self._conn, "theme", name)
        self._apply_theme(name)

    def _apply_theme(self, name: str) -> None:
        Adw.StyleManager.get_default().set_color_scheme(
            self._SCHEMES.get(name, Adw.ColorScheme.DEFAULT))

    def cycle_theme(self, *_):
        """Ctrl+T steps through system -> light -> dark."""
        current = self._theme_action.get_state().get_string()
        nxt = self.THEMES[(self.THEMES.index(current) + 1) % len(self.THEMES)]
        self._theme_action.activate(GLib.Variant("s", nxt))
        self.toasts.add_toast(Adw.Toast(
            title={"system": "Following the system theme",
                   "light": "Light theme", "dark": "Dark theme"}[nxt], timeout=2))

    def toggle_hide_read(self, *_):
        """Hide or show articles already read, in the queue and while reading."""
        now = not self.hide_read
        db.state_set(self._conn, "hide_read", "1" if now else "0")
        if hasattr(self, "library"):
            self.library.hide_read = now
            self.library.hide_read_button.handler_block_by_func(
                self.library._on_hide_read)
            self.library.hide_read_button.set_active(now)
            self.library.hide_read_button.handler_unblock_by_func(
                self.library._on_hide_read)
            self.library.reload()
        if self.current is not None:
            self._update_reader_chrome(self.current)
        self.toasts.add_toast(Adw.Toast(
            title="Hiding articles you have read" if now
            else "Showing all articles", timeout=2))

    def show_page(self, name: str) -> None:
        self.stack.set_visible_child_name(name)

    def _on_page_changed(self, *_):
        name = self.stack.get_visible_child_name()
        if name == "library":
            self.library.reload()
        elif name == "sources":
            self.sources_view.reload()
        # Put focus somewhere that is not a text field, or the single-key
        # shortcuts would be suspended the moment you open the page -- the
        # search box is the first focusable widget on the library page and
        # would otherwise claim focus by default.
        self._focus_page_content(name)

    def _focus_page_content(self, name: str) -> None:
        target = None
        if name == "library":
            target = self.library.listview
        elif name == "reader":
            target = self.reader.webview
        if target is not None:
            target.grab_focus()

    def focus_search(self, *_):
        self.show_page("library")
        self.library.focus_search()

    # -- reading -----------------------------------------------------------

    def resume(self) -> None:
        """Open wherever the reader left off, or the oldest unread article."""
        article = db.resume_article(self._conn)
        if article is None:
            counts = db.queue_counts(self._conn)
            if counts["all"]:
                self.reader.show_placeholder(
                    "Everything read",
                    "You have reached the end of the queue. New articles appear "
                    "here after the next archive update.")
            else:
                self.reader.show_placeholder(
                    "Your library is empty",
                    "Open the <b>Blogs</b> tab and add a blog you read. "
                    "Chronicle then works out how to recover its full history "
                    "and builds the archive. The first build of a long-running "
                    "blog takes a while — it fetches every article ever "
                    "published, not just the recent ones.")
            self._update_reader_chrome(None)
            self.show_page("sources" if not db.list_sources(self._conn)
                       else "reader")
            return
        # Reopening at the top would throw away the position saved on the way
        # out, which is exactly the thing a long article needs remembered.
        state = self._state_of(article["id"])
        stored = state["scroll_pos"] if state else 0.0
        self.open_article(article["id"])
        if shows_resume_hint(stored):
            self._show_resume_hint(stored)

    def open_article(self, article_id: int, remember: bool = True) -> None:
        article = db.get_article(self._conn, article_id)
        if article is None:
            return
        self.current = article
        self._marked_read = bool(self._state_of(article_id)["read_at"]
                                 if self._state_of(article_id) else False)

        state = self._state_of(article_id)
        scroll = resume_scroll(state["scroll_pos"] if state else 0.0, remember)
        # The chrome below reads this, so the time left is right on the first
        # paint rather than a frame of the previous article's figure.
        self._scroll_frac = scroll
        self.reader.show_article(article, scroll)
        db.state_set(self._conn, "current_article_id", article_id)
        self._update_reader_chrome(article)
        self.show_page("reader")

    def _state_of(self, article_id: int):
        return self._conn.execute(
            "SELECT * FROM reading_state WHERE article_id=?", (article_id,)).fetchone()

    def _update_reader_chrome(self, article) -> None:
        if article is None:
            self._position_text = ""
            self.position_label.set_label("")
            for w in (self.prev_button, self.next_button, self.fav_button,
                      self.read_button, self.skip_button):
                w.set_sensitive(False)
            return
        for w in (self.prev_button, self.next_button, self.fav_button,
                  self.read_button, self.skip_button):
            w.set_sensitive(True)

        label = dates.format_display(article["published_at"],
                                     article["date_precision"],
                                     article["date_confidence"])
        minutes = reading_minutes(article["word_count"])

        if self.hide_read:
            # With read articles hidden, a position within the whole archive
            # is not what you want to know -- how much is still ahead is.
            left = db.queue_counts(self._conn)["unread"]
            place = f"{left:,} article{'' if left == 1 else 's'} left".replace(",", " ")
        else:
            pos, total = db.position_in_queue(self._conn, article["id"])
            place = f"{pos:,} of {total:,}".replace(",", " ")

        # The line is rebuilt from a stable prefix plus a time that changes on
        # every scroll; keeping them apart means scrolling does not have to
        # redo the queue queries above.
        self._position_prefix = (
            f"{place}   ·   {article['source_name']}   ·   {label}")
        self._total_minutes = minutes
        self._refresh_position_text()

        state = self._state_of(article["id"])
        self.fav_button.handler_block_by_func(self._on_favourite_toggled)
        self.fav_button.set_active(bool(state and state["favourite_at"]))
        self.fav_button.set_icon_name(
            "starred-symbolic" if (state and state["favourite_at"])
            else "non-starred-symbolic")
        self.fav_button.handler_unblock_by_func(self._on_favourite_toggled)

        self.read_button.handler_block_by_func(self._on_read_toggled)
        self.read_button.set_active(bool(state and state["read_at"]))
        self.read_button.handler_unblock_by_func(self._on_read_toggled)

        self.prev_button.set_sensitive(
            db.neighbour(self._conn, article["id"], -1,
                         hide_read=self.hide_read) is not None)
        self.next_button.set_sensitive(
            db.neighbour(self._conn, article["id"], +1,
                         hide_read=self.hide_read) is not None)

    def go_next(self, *_):
        if self.current is None:
            return
        self._flush_scroll()
        db.set_read(self._conn, self.current["id"], True)
        nxt = db.neighbour(self._conn, self.current["id"], +1,
                           hide_read=self.hide_read)
        if nxt is None:
            self.toasts.add_toast(Adw.Toast(title="That was the last article",
                                            timeout=3))
            self._update_reader_chrome(self.current)
            return
        self.open_article(nxt["id"])

    def go_previous(self, *_):
        if self.current is None:
            return
        self._flush_scroll()
        prev = db.neighbour(self._conn, self.current["id"], -1,
                            hide_read=self.hide_read)
        if prev is None:
            self.toasts.add_toast(Adw.Toast(title="This is the oldest article",
                                            timeout=3))
            return
        self.open_article(prev["id"])

    def skip_article(self, *_):
        """Pass this article over and move on to the next one.

        The next article is worked out *before* the skip is recorded: once
        skipped, this article is no longer in the queue, and asking for its
        neighbour afterwards would be asking about a row that is no longer
        there. Deliberately not marked read -- a skip is its own verdict.
        """
        if self.current is None:
            return
        article_id = self.current["id"]
        title = self.current["title"] or "Untitled"
        self._flush_scroll()
        nxt = db.neighbour(self._conn, article_id, +1, hide_read=self.hide_read)
        db.set_skipped(self._conn, article_id, True)

        toast = Adw.Toast(title=f"Skipped “{_shorten(title)}”", timeout=5,
                          button_label="Undo")
        toast.connect("button-clicked", self._undo_skip, article_id)
        self.toasts.add_toast(toast)

        if nxt is None:
            # Nothing after it: stay put rather than leaving a blank reader,
            # but the chrome has to be rebuilt so the queue figures are right.
            self.refresh_library()
            self._update_reader_chrome(self.current)
            return
        self.open_article(nxt["id"])
        self.refresh_library()

    def _undo_skip(self, _toast, article_id: int) -> None:
        """Put a skipped article back, and return to it."""
        db.set_skipped(self._conn, article_id, False)
        self.open_article(article_id)
        self.refresh_library()

    def toggle_favourite(self, *_):
        self.fav_button.set_active(not self.fav_button.get_active())

    def _on_favourite_toggled(self, button) -> None:
        if self.current is None:
            return
        now = db.toggle_favourite(self._conn, self.current["id"])
        button.set_icon_name("starred-symbolic" if now
                             else "non-starred-symbolic")
        self.toasts.add_toast(Adw.Toast(
            title="Added to favourites" if now else "Removed from favourites",
            timeout=2))

    def toggle_read(self, *_):
        self.read_button.set_active(not self.read_button.get_active())

    def _on_read_toggled(self, button) -> None:
        if self.current is None:
            return
        db.set_read(self._conn, self.current["id"], button.get_active())
        self._marked_read = button.get_active()

    def _on_scrolled(self, _reader, fraction: float) -> None:
        self._scroll_frac = fraction
        self._refresh_position_text()
        if fraction >= READ_THRESHOLD and not self._marked_read and self.current:
            self._marked_read = True
            db.set_read(self._conn, self.current["id"], True)
            self.read_button.handler_block_by_func(self._on_read_toggled)
            self.read_button.set_active(True)
            self.read_button.handler_unblock_by_func(self._on_read_toggled)

    def _flush_scroll(self) -> None:
        self._clear_resume_hint()
        if self.current is not None:
            db.set_scroll(self._conn, self.current["id"], self._scroll_frac)
            # A note being typed when the reader moves on is committed by the
            # page itself; asking for it here means the keystroke that
            # navigates away cannot lose the sentence in progress.
            self.reader.flush_note()
        self._scroll_frac = 0.0

    # -- annotations -------------------------------------------------------

    def _on_highlight_note(self, _reader, highlight_id: int) -> None:
        """Attach or edit the note on one highlight."""
        rows = [r for r in db.list_highlights(self._conn, self.current["id"])
                if r["id"] == highlight_id] if self.current else []
        if not rows:
            return
        row = rows[0]
        quote = row["quote"]
        dialog = Adw.AlertDialog(
            heading="Note on this highlight",
            body=f"“{quote[:180]}{'…' if len(quote) > 180 else ''}”")
        entry = Gtk.Entry(text=row["note"] or "", activates_default=True,
                          placeholder_text="What did you want to remember?")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def done(_d, response):
            if response != "save":
                return
            db.set_highlight_note(self._conn, highlight_id, entry.get_text())
            self.reader._push_highlights()
            self.refresh_library()

        dialog.connect("response", done)
        dialog.present(self)
        entry.grab_focus()

    def _on_article_chosen(self, _view, article_id: int) -> None:
        self._flush_scroll()
        self.open_article(article_id)

    def _refresh_position_text(self) -> None:
        """Rebuild the position line around the time that is actually left."""
        if not self._position_prefix:
            return
        time_text = time_remaining(self._total_minutes, self._scroll_frac)
        self._position_text = f"{self._position_prefix}   ·   {time_text}"
        # A hovered link or a live resume hint owns the label until it expires.
        if not self._hovered_link and not self._resume_hint_id:
            self.position_label.set_label(self._position_text)

    def _show_resume_hint(self, fraction: float) -> None:
        """Say, quietly and briefly, that the reader was put back where it was.

        This borrows the bottom bar the same way a hovered link does: the
        position line is worth less, for a few seconds, than knowing why the
        article did not open at the top.
        """
        self._clear_resume_hint()
        self.position_label.set_label(
            f"Resumed where you left off   ·   {round(fraction * 100)}%")
        self._resume_hint_id = GLib.timeout_add_seconds(
            RESUME_HINT_SECONDS, self._end_resume_hint)

    def _end_resume_hint(self) -> bool:
        self._resume_hint_id = 0
        # A link under the pointer owns the label; let it keep it.
        if not self._hovered_link:
            self.position_label.set_label(self._position_text)
        return GLib.SOURCE_REMOVE

    def _clear_resume_hint(self) -> None:
        """Retire a pending hint so it cannot overwrite a later label.

        Navigating away inside the hint's few seconds has to put the position
        line back itself; the timeout that would have done it is gone.
        """
        if self._resume_hint_id:
            GLib.source_remove(self._resume_hint_id)
            self._resume_hint_id = 0
            if not self._hovered_link:
                self.position_label.set_label(self._position_text)

    def _on_hovering_link(self, _reader, uri: str) -> None:
        """Show where a link goes, in place of the position line.

        The bottom bar is where the eye already is, and the position is worth
        less than the destination for as long as a link is under the pointer.
        """
        self._hovered_link = uri
        self.position_label.set_label(elide_url(uri) if uri
                                      else self._position_text)
        self.position_label.set_tooltip_text(uri or None)

    def _on_link(self, _reader, uri: str) -> None:
        Gtk.UriLauncher(uri=uri).launch(self, None, None, None)

    def open_external(self, *_):
        if self.current and self.current["url"]:
            self._on_link(None, self.current["url"])

    # -- syncing -----------------------------------------------------------

    def start_sync(self, *_):
        """F5: the routine update — what the blogs published since last time."""
        self._on_sync_requested(None, None, False)

    def start_full_scan(self, *_):
        self._on_sync_requested(None, None, True)

    def _on_sync_requested(self, _widget, source_ids, full_scan: bool) -> None:
        if self.syncer.running:
            self.toasts.add_toast(Adw.Toast(title="An update is already running",
                                            timeout=3))
            return
        self.sync_button.set_sensitive(False)
        self.show_page("sources")
        threading.Thread(
            target=self.syncer.sync_all,
            args=(source_ids,), kwargs={"newest_only": not full_scan},
            daemon=True).start()

    def _on_sync_progress(self, prog) -> None:
        GLib.idle_add(self._apply_progress, prog)

    def _apply_progress(self, prog) -> bool:
        self.sources_view.set_progress(prog)
        if prog.done:
            self.sync_button.set_sensitive(True)
            self.sources_view.reload()
            self.refresh_library()
            if prog.error:
                self.toasts.add_toast(Adw.Toast(title=f"Update failed: {prog.error}",
                                                timeout=8))
            else:
                self.toasts.add_toast(Adw.Toast(title=prog.message, timeout=5))
            if self.current is None:
                self.resume()
        return False

    def refresh_library(self) -> None:
        if hasattr(self, "library"):
            self.library.refresh_if_at_top()
        if self.current is not None:
            self._update_reader_chrome(self.current)

    # -- dialogs -----------------------------------------------------------

    def show_shortcuts(self, *_):
        pairs = [
            ("Next article", "→ / N / J / Page Down"),
            ("Previous article", "← / P / K / Page Up"),
            ("Scroll a page", "Space / Shift+Space"),
            ("Scroll a few lines", "\u2191 / \u2193"),
            ("Start / end of article", "Home / End"),
            ("Favourite", "F"),
            ("Mark read / unread", "R"),
            ("Skip this article", "S"),
            ("Library", "L / Esc"),
            ("Search", "Ctrl+F or /"),
            ("Fetch new posts", "F5"),
            ("Full archive scan", "Shift+F5"),
            ("Hide / show read articles", "H"),
            ("Switch theme", "Ctrl+T"),
            ("Open original", "Ctrl+O"),
        ]
        body = "\n".join(f"{k}   —   {v}" for k, v in pairs)
        dialog = Adw.AlertDialog(heading="Keyboard shortcuts", body=body)
        dialog.add_response("close", "Close")
        dialog.present(self)

    def show_about(self, *_):
        s = db.stats(self._conn)
        about = Adw.AboutDialog(
            application_name="Chronicle",
            application_icon="io.github.mvinhas.Chronicle",
            version=__version__,
            developer_name="MVinhas",
            comments=("A personal chronological library of the blogs you love.\n\n"
                      f"{s['articles']:,} articles archived, spanning "
                      f"{(s['oldest'] or '')[:4]} to {(s['newest'] or '')[:4]}."
                      ).replace(",", " "),
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/mvinhas/chronicle")
        about.add_credit_section("Typography", ["Source Serif 4 — Adobe (OFL)"])
        about.present(self)

    # -- shutdown ----------------------------------------------------------

    def do_close_request(self) -> bool:
        # On the way out the page's reply has to be waited for: there is no
        # later main-loop turn in which to deliver it.
        self.reader.flush_note(wait=True)
        self._flush_scroll()
        if self.syncer.running:
            self.syncer.cancel()
        return False
