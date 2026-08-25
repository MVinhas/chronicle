"""The reading view: a WebKit surface showing one article at a time."""
from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, GObject, Gtk, WebKit  # noqa: E402

from .. import db, images, paths  # noqa: E402
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
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.article_id: int | None = None
        self._pending_scroll = 0.0

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

        self.webview.set_background_color(_rgba("#fdfcfa"))
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
        self._pending_scroll = scroll or 0.0
        self.webview.load_html(style.build_document(article), article["url"] or None)

    def show_placeholder(self, heading: str, body: str) -> None:
        self.article_id = None
        self._pending_scroll = 0.0
        self.webview.load_html(style.placeholder(heading, body), None)

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
        self._run_js(
            f"window.scrollBy({{top: {direction} * (window.innerHeight - 80), "
            f"behavior: 'smooth'}});")

    def scroll_home(self) -> None:
        self._run_js("window.scrollTo({top: 0, behavior: 'smooth'});")

    # -- events ------------------------------------------------------------

    def _on_message(self, _ucm, value) -> None:
        try:
            payload = json.loads(value.to_json(0) if hasattr(value, "to_json")
                                 else str(value))
            if isinstance(payload, str):
                payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        if payload.get("type") == "scroll":
            self.emit("scrolled", float(payload.get("value") or 0.0))

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
