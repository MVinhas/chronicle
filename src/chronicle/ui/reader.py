"""The reading view: a WebKit surface showing one article at a time."""
from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
import urllib.parse
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, GObject, Gtk, WebKit  # noqa: E402

from .. import db, dictionary, images, paths  # noqa: E402
from . import style  # noqa: E402

log = logging.getLogger("chronicle.reader")

ASSET_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "data"


def _asset_root() -> Path:
    """Assets live beside the source in a checkout, under /app/share in Flatpak."""
    for candidate in (ASSET_ROOT,
                      Path("/app/share/chronicle"),
                      Path(__file__).resolve().parent.parent / "data"):
        if (candidate / "fonts").is_dir():
            return candidate
    return ASSET_ROOT


class ReaderView(Gtk.Box):
    """WebKit-backed article renderer.

    Also doubles as the app's Cloudflare-capable fetcher: it is a complete
    browser engine running from the user's own machine, so sites that refuse
    scripted clients will serve it normally.
    """

    __gtype_name__ = "ChronicleReaderView"

    __gsignals__ = {
        "scrolled": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "link-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Raised when the page wants a highlight's note edited; the window
        # owns the dialog, because the reader has no toplevel of its own.
        "note-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # Something the reader wrote was stored; the library shows it.
        "annotations-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # True while a text field *inside the page* has focus. GTK cannot see
        # that on its own -- it only knows the WebView is focused -- and the
        # window needs it to stand down the single-key shortcuts.
        "editing": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        # The link under the pointer, or "" when there is none.
        "hovering-link": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.article_id: int | None = None
        self._pending_scroll = 0.0
        self._last_scroll = 0.0
        self._article = None
        self._placeholder: tuple[str, str] | None = None
        self._editing = False
        self.dark = False

        self.web_context = WebKit.WebContext()
        security = self.web_context.get_security_manager()
        for scheme in (images.SCHEME, style.ASSET_SCHEME):
            # Must be "secure", not "local": the document is loaded with the
            # article's https base URI, and a remote origin may neither load a
            # local scheme nor a non-secure one (mixed content).
            security.register_uri_scheme_as_secure(scheme)
            security.register_uri_scheme_as_cors_enabled(scheme)
        self.web_context.register_uri_scheme(images.SCHEME, self._serve_image)
        self.web_context.register_uri_scheme(style.ASSET_SCHEME, self._serve_asset)

        self.ucm = WebKit.UserContentManager()
        self.ucm.register_script_message_handler("chronicle", None)
        self.ucm.connect("script-message-received::chronicle", self._on_message)

        self.webview = WebKit.WebView(
            web_context=self.web_context, user_content_manager=self.ucm,
            vexpand=True, hexpand=True)

        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_javascript(True)
        settings.set_enable_page_cache(False)
        settings.set_enable_html5_database(False)
        settings.set_enable_html5_local_storage(False)
        settings.set_enable_media(False)
        settings.set_enable_webaudio(False)
        settings.set_enable_webgl(False)
        settings.set_javascript_can_open_windows_automatically(False)
        settings.set_enable_back_forward_navigation_gestures(False)
        settings.set_default_font_family("serif")
        # The page animates its own scrolling, on whole device pixels. WebKit's
        # animator works in fractions of one, which leaves the composited text
        # off its raster grid and soft until something repaints it; anything
        # this one still handles is better off jumping, which at least lands
        # somewhere sharp.
        settings.set_enable_smooth_scrolling(False)

        self.webview.set_background_color(_rgba(style.BACKGROUND["light"]))
        self.webview.connect("mouse-target-changed", self._on_mouse_target)
        self.webview.connect("decide-policy", self._on_policy)
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("context-menu", lambda *_: True)

        self.append(self.webview)

    # -- custom schemes ----------------------------------------------------

    def _serve_image(self, request, *_):
        relpath = (request.get_uri().split("://", 1)[-1]).split("?")[0]
        target = images.resolve(relpath)
        if target is None:
            request.finish_error(GLib.Error.new_literal(
                Gio.io_error_quark(), "not found", Gio.IOErrorEnum.NOT_FOUND))
            return
        self._finish_file(request, target)

    def _serve_asset(self, request, *_):
        rel = (request.get_uri().split("://", 1)[-1]).split("?")[0].lstrip("/")
        if ".." in rel:
            request.finish_error(GLib.Error.new_literal(
                Gio.io_error_quark(), "denied", Gio.IOErrorEnum.PERMISSION_DENIED))
            return
        target = (_asset_root() / rel).resolve()
        try:
            target.relative_to(_asset_root().resolve())
        except ValueError:
            request.finish_error(GLib.Error.new_literal(
                Gio.io_error_quark(), "denied", Gio.IOErrorEnum.PERMISSION_DENIED))
            return
        if not target.exists():
            request.finish_error(GLib.Error.new_literal(
                Gio.io_error_quark(), "not found", Gio.IOErrorEnum.NOT_FOUND))
            return
        self._finish_file(request, target)

    @staticmethod
    def _finish_file(request, target: Path) -> None:
        mime, _ = mimetypes.guess_type(target.name)
        if target.suffix == ".woff2":
            mime = "font/woff2"
        try:
            data = target.read_bytes()
        except OSError:
            request.finish_error(GLib.Error.new_literal(
                Gio.io_error_quark(), "unreadable", Gio.IOErrorEnum.FAILED))
            return
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        request.finish(stream, len(data), mime or "application/octet-stream")

    # -- loading -----------------------------------------------------------

    def show_article(self, article, scroll: float = 0.0) -> None:
        self.article_id = article["id"]
        self._article = article
        self._placeholder = None
        self._pending_scroll = scroll or 0.0
        self._last_scroll = self._pending_scroll
        self._set_editing(False)
        conn = db.get_conn()
        self.webview.load_html(
            style.build_document(article, self.dark,
                                 note=db.get_note(conn, article["id"]),
                                 highlights=db.list_highlights(conn, article["id"])),
            article["url"] or None)

    def show_placeholder(self, heading: str, body: str) -> None:
        self._set_editing(False)
        self.article_id = None
        self._article = None
        self._placeholder = (heading, body)
        self._pending_scroll = 0.0
        self.webview.load_html(style.placeholder(heading, body, self.dark), None)

    def set_dark(self, dark: bool) -> None:
        """Repaint for the desktop's colour scheme, keeping the reader's place."""
        if dark == self.dark:
            return
        self.dark = dark
        self.webview.set_background_color(
            _rgba(style.BACKGROUND["dark" if dark else "light"]))
        if self._article is not None:
            self.show_article(self._article, self._last_scroll)
        elif self._placeholder is not None:
            self.show_placeholder(*self._placeholder)

    def _on_load_changed(self, _view, event) -> None:
        if event == WebKit.LoadEvent.FINISHED and self._pending_scroll > 0.01:
            frac = self._pending_scroll
            self._pending_scroll = 0.0
            GLib.timeout_add(120, lambda: self._run_js(
                f"window.chronicleScrollTo({frac});") and False)

    def _run_js(self, script: str) -> bool:
        try:
            self.webview.evaluate_javascript(script, -1, None, None, None, None, None)
        except Exception as exc:                      # noqa: BLE001
            log.debug("js failed: %s", exc)
        return True

    def scroll_by_page(self, direction: int) -> None:
        # Through the page's own scroller rather than the engine's: that one
        # keeps every frame on a whole device pixel, and it knows where the
        # lines are. The guard is for the placeholder, which has no script.
        self._run_js(f"window.chronicleScrollPage && "
                     f"window.chronicleScrollPage({direction});")

    def scroll_home(self) -> None:
        self._run_js("window.chronicleScrollHome && window.chronicleScrollHome();")

    # -- events ------------------------------------------------------------

    def _on_message(self, _ucm, value) -> None:
        try:
            payload = json.loads(value.to_json(0) if hasattr(value, "to_json")
                                 else str(value))
            if isinstance(payload, str):
                payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        kind = payload.get("type")
        if kind == "scroll":
            self._last_scroll = float(payload.get("value") or 0.0)
            self.emit("scrolled", self._last_scroll)
        elif kind == "editing":
            self._set_editing(bool(payload.get("value")))
        elif kind == "define":
            self._define(payload.get("word") or "")
        elif kind == "search":
            self._search(payload.get("text") or "")
        elif kind in ("note", "highlight-add", "highlight-remove",
                      "highlight-note", "anchors"):
            self._on_annotation(kind, payload)

    def _set_editing(self, editing: bool) -> None:
        if editing == self._editing:
            return
        self._editing = editing
        self.emit("editing", editing)

    # -- looking things up -------------------------------------------------

    def _define(self, word: str) -> None:
        """Answer the page's request for a definition.

        A word already in the library is answered on the spot; anything else
        needs the network, which cannot happen on this thread — the reader
        would freeze mid-page for as long as the lookup took. The worker does
        nothing but fetch: the library stays on the main thread, where there
        is one connection rather than one per word looked up.
        """
        headword = dictionary.normalise(word)
        if not headword:
            return
        entry = dictionary.cached(db.get_conn(), headword)
        if entry is not None:
            self._send_definition(headword, entry)
            return
        threading.Thread(target=self._define_worker, args=(headword,),
                         daemon=True).start()

    def _define_worker(self, headword: str) -> None:
        try:
            entry = dictionary.fetch(headword)
        except OSError as exc:
            log.debug("lookup of %r failed: %s", headword, exc)
            # Not stored: a failure to reach Wiktionary says nothing about
            # the word, and caching it would make the next attempt fail too.
            entry = {"word": headword,
                     "error": "Could not reach the dictionary."}
        GLib.idle_add(self._settle_definition, headword, entry)

    def _settle_definition(self, headword: str, entry: dict) -> bool:
        if not entry.get("error"):
            dictionary.remember(db.get_conn(), headword, entry)
        return self._send_definition(headword, entry)

    def _send_definition(self, headword: str, entry: dict) -> bool:
        # `lookup` is the word that was asked about, which the dictionary's
        # own headword need not match; the page uses it to drop an answer
        # that arrived after the reader moved on.
        payload = dict(entry, lookup=headword)
        self._run_js(f"window.chronicleDefinition && "
                     f"window.chronicleDefinition({json.dumps(payload)});")
        return False

    def _search(self, text: str) -> None:
        """Hand the selected words to a web search, outside the application.

        Reuses `link-activated` rather than adding a signal of its own: as far
        as the window is concerned this is the same act — the page asked for
        an address to be opened in the browser.
        """
        query = " ".join(text.split())[:300]
        if not query:
            return
        self.emit("link-activated",
                  "https://www.google.com/search?q=" +
                  urllib.parse.quote_plus(query))

    # -- annotations -------------------------------------------------------

    def _on_annotation(self, kind: str, payload: dict) -> None:
        """Apply one change the reading surface asked for.

        Everything here is keyed on the article the reader is *currently*
        showing rather than on anything the page sent: a message that arrives
        as the next article loads must not write onto the wrong article.
        """
        if self.article_id is None:
            return
        conn = db.get_conn()
        article_id = self.article_id

        if kind == "note":
            db.set_note(conn, article_id, payload.get("body") or "")
            self.emit("annotations-changed")
            return

        if kind == "anchors":
            # Where the page found each highlight after laying it over the
            # text. Corrects drifted offsets and flags ones that vanished.
            for entry in payload.get("anchors") or []:
                try:
                    hid = int(entry.get("id"))
                except (TypeError, ValueError):
                    continue
                offset = entry.get("offset")
                db.reanchor_highlight(
                    conn, hid, None if offset is None else int(offset))
            return

        if kind == "highlight-add":
            quote = (payload.get("quote") or "").strip()
            if not quote:
                return
            db.add_highlight(conn, article_id, quote,
                             prefix=payload.get("prefix") or "",
                             suffix=payload.get("suffix") or "",
                             start_offset=int(payload.get("start_offset") or 0))
            self._push_highlights()
            self.emit("annotations-changed")
            return

        if kind == "highlight-remove":
            try:
                db.delete_highlight(conn, int(payload.get("id")))
            except (TypeError, ValueError):
                return
            self._push_highlights()
            self.emit("annotations-changed")
            return

        if kind == "highlight-note":
            try:
                self.emit("note-requested", int(payload.get("id")))
            except (TypeError, ValueError):
                pass

    def _push_highlights(self) -> None:
        """Re-send the stored highlights so the page shows what was saved."""
        if self.article_id is None:
            return
        rows = db.list_highlights(db.get_conn(), self.article_id)
        self._run_js(
            f"window.chronicleSetHighlights({style.highlights_json(rows)});")

    def blur_editor(self) -> None:
        """Let go of a focused field inside the page.

        The blur handler there saves the note and reports focus lost, which is
        what re-arms the reader's single-key shortcuts.
        """
        self._run_js("if (document.activeElement && document.activeElement.blur) "
                     "document.activeElement.blur();")

    def flush_note(self, wait: bool = False) -> None:
        """Commit a half-typed note before the reader moves on.

        The page posts the text back over the script-message channel, which is
        asynchronous. That is fine when navigating -- the message arrives a
        moment later and lands on the right article, because the handler keys
        on the article showing at the time. On the way out of the application
        there may be no "moment later", so `wait` pumps the main loop briefly
        to let the message be delivered before the process goes away.
        """
        self._run_js("window.chronicleFlushNote && window.chronicleFlushNote();")
        if not wait:
            return
        deadline = time.monotonic() + 0.5
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            if not context.pending():
                # Nothing left in flight; the note has either arrived or was
                # never dirty in the first place.
                break
            context.iteration(False)

    def _on_mouse_target(self, _view, hit, _modifiers) -> None:
        """Report the link under the pointer so the window can show it.

        Articles are full of links whose text says nothing about where they
        go ("this", "here", a quoted phrase), and the reader cannot see a
        target the way a browser's status bar shows one.
        """
        uri = hit.get_link_uri() if hit.context_is_link() else ""
        self.emit("hovering-link", uri or "")

    def _on_policy(self, _view, decision, decision_type) -> bool:
        if decision_type != WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        action = decision.get_navigation_action()
        if action.get_navigation_type() != WebKit.NavigationType.LINK_CLICKED:
            return False
        uri = action.get_request().get_uri()
        decision.ignore()
        if uri and uri.startswith(("http://", "https://")):
            self.emit("link-activated", uri)
        return True


def _rgba(hex_colour: str):
    from gi.repository import Gdk
    rgba = Gdk.RGBA()
    rgba.parse(hex_colour)
    return rgba
