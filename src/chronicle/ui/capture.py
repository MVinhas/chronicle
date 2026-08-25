"""Self-capture for visual review.

GNOME 49 refuses compositor screenshots to unsandboxed callers, so instead the
app renders its own window through GSK — the same renderer that draws it — and
writes a PNG. That works on Wayland and X11 alike and needs no portal.

Driven entirely by environment variables so it never affects normal runs:
    CHRONICLE_SHOT       output PNG path (enables capture)
    CHRONICLE_SHOT_PAGE  reader | library | sources   (default: reader)
    CHRONICLE_SHOT_DELAY seconds to settle before capturing (default 3.5)
    CHRONICLE_SHOT_QUIT  1 to exit after capturing (default 1)
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

    def run() -> bool:
        try:
            if page in ("reader", "library", "sources"):
                window.show_page(page)
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
