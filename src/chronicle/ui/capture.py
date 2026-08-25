"""Self-capture for visual review.

GNOME 49 refuses compositor screenshots to unsandboxed callers, so instead the
app renders its own window through GSK — the same renderer that draws it — and
writes a PNG. That works on Wayland and X11 alike and needs no portal.

Driven entirely by environment variables so it never affects normal runs:
    CHRONICLE_SHOT          output PNG path (enables capture)
    CHRONICLE_SHOT_PAGE     reader | library | sources   (default: reader)
    CHRONICLE_SHOT_DELAY    seconds to settle before capturing (default 3.5)
    CHRONICLE_SHOT_QUIT     1 to exit after capturing (default 1)
    CHRONICLE_SHOT_SIZE     WxH to force the window to, e.g. 1280x800
    CHRONICLE_SHOT_ARTICLE  article id to open, from the top, unscrolled
    CHRONICLE_SHOT_SCROLL   0..1 fraction to scroll the library list to

CHRONICLE_DEMO=1 instead walks the window through a short scripted tour, so a
screen recording shows the real application being used rather than a slideshow.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

log = logging.getLogger("chronicle.capture")


def enabled() -> bool:
    return bool(os.environ.get("CHRONICLE_SHOT"))


def demo(window) -> None:
    """Walk the window through a short tour, for screen recording.

    Each step is a real action on the real window -- the same handlers the
    keyboard shortcuts call -- so what gets recorded is the application being
    used, just without a hand on the keyboard.
    """
    if not os.environ.get("CHRONICLE_DEMO"):
        return

    # Fill the screen so a full-screen recording is all application.
    if os.environ.get("CHRONICLE_DEMO_MAXIMIZE", "1") != "0":
        window.maximize()

    reader = window.reader
    steps = [
        (2600, lambda: window.show_page("reader")),
        (3000, lambda: reader.scroll_by_page(1)),
        (2600, lambda: reader.scroll_by_page(1)),
        (2200, lambda: window.go_next()),
        (3000, lambda: reader.scroll_by_page(1)),
        (2400, lambda: window.go_next()),
        (2600, lambda: window.show_page("library")),
        (2600, lambda: _scroll_library(window, 0.28, delay=200)),
        (2600, lambda: _scroll_library(window, 0.42, delay=200)),
        (2400, lambda: window.toggle_hide_read()),
        (2600, lambda: window.show_page("sources")),
        (2600, lambda: window.cycle_theme()),
        (2800, lambda: window.show_page("reader")),
        (3000, lambda: window.cycle_theme()),
        (2600, lambda: None),
    ]

    state = {"i": 0}

    def tick() -> bool:
        i = state["i"]
        if i >= len(steps):
            if os.environ.get("CHRONICLE_DEMO_QUIT", "1") != "0":
                window.get_application().quit()
            return False
        wait, action = steps[i]
        state["i"] = i + 1
        try:
            action()
        except Exception:                             # noqa: BLE001
            log.exception("demo step %s failed", i)
        GLib.timeout_add(wait, tick)
        return False

    GLib.timeout_add(1800, tick)


def _scroll_library(window, fraction: float, delay: int = 700) -> None:
    """Scroll the queue so a screenshot can show a representative stretch."""
    try:
        adj = window.library.scroller.get_vadjustment()
    except AttributeError:
        return

    def apply() -> bool:
        span = adj.get_upper() - adj.get_page_size()
        if span > 0:
            adj.set_value(span * max(0.0, min(1.0, fraction)))
        return False

    # The list virtualises, so its extent is only known after a layout pass.
    GLib.timeout_add(delay, apply)


def capture_window(window: Gtk.Window, path: str | Path) -> bool:
    """Render a realized window to a PNG via its own GSK renderer."""
    native = window.get_native()
    if native is None:
        log.warning("window has no native surface yet")
        return False
    renderer = native.get_renderer()
    if renderer is None:
        log.warning("no GSK renderer available")
        return False

    width = window.get_allocated_width() or window.get_width()
    height = window.get_allocated_height() or window.get_height()
    if width <= 0 or height <= 0:
        log.warning("window not sized yet (%sx%s)", width, height)
        return False

    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        log.warning("nothing to render")
        return False

    texture = renderer.render_texture(
        node, Graphene.Rect().init(0, 0, width, height))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    texture.save_to_png(str(path))
    log.info("captured %sx%s to %s", width, height, path)
    return True


def arm(window) -> None:
    """Schedule a capture if the environment asked for one."""
    if not enabled():
        return
    out = os.environ["CHRONICLE_SHOT"]
    page = os.environ.get("CHRONICLE_SHOT_PAGE", "reader")
    delay = float(os.environ.get("CHRONICLE_SHOT_DELAY", "3.5"))
    should_quit = os.environ.get("CHRONICLE_SHOT_QUIT", "1") != "0"
    size = os.environ.get("CHRONICLE_SHOT_SIZE")
    article = os.environ.get("CHRONICLE_SHOT_ARTICLE")
    scroll = os.environ.get("CHRONICLE_SHOT_SCROLL")

    if size:
        try:
            w, h = (int(v) for v in size.lower().split("x", 1))
            window.unmaximize()
            window.set_default_size(w, h)
            window.set_size_request(w, h)
        except (ValueError, AttributeError):
            log.warning("bad CHRONICLE_SHOT_SIZE %r", size)

    def run() -> bool:
        try:
            if article and hasattr(window, "open_article"):
                # remember=False opens at the top rather than restoring scroll
                window.open_article(int(article), remember=False)
            if page in ("reader", "library", "sources"):
                window.show_page(page)
            if scroll and page == "library":
                _scroll_library(window, float(scroll))
            # Let the page settle (WebKit paint, list realisation) before capture.
            GLib.timeout_add(int(delay * 1000 * 0.45), finish)
        except Exception:                             # noqa: BLE001
            log.exception("capture setup failed")
            if should_quit:
                window.get_application().quit()
        return False

    def finish() -> bool:
        try:
            capture_window(window, out)
        except Exception:                             # noqa: BLE001
            log.exception("capture failed")
        if should_quit:
            GLib.timeout_add(200, lambda: window.get_application().quit() or False)
        return False

    GLib.timeout_add(int(delay * 1000), run)
