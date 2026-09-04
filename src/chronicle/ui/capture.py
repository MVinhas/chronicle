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
    CHRONICLE_SHOT_SCOPE    library filter to select (e.g. highlighted, skipped)
    CHRONICLE_SHOT_SELECT   word to select in the article, raising its popup
    CHRONICLE_SHOT_DEFINE   1 to then press Define, for the dictionary card

CHRONICLE_DEMO=1 instead walks the window through a short scripted tour.
Setting CHRONICLE_DEMO_FRAMES to a directory records that tour frame by frame,
straight from the window's own renderer. That is deliberate: a compositor screen
recording would capture the whole desktop, and everything else on it. This
captures only the application.
"""
from __future__ import annotations

import json
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

    if os.environ.get("CHRONICLE_DEMO_MAXIMIZE") == "1":
        window.maximize()

    frames_dir = os.environ.get("CHRONICLE_DEMO_FRAMES")
    if frames_dir:
        # Start once the opening article has loaded, so the first frame shows
        # the masthead rather than a half-painted page.
        GLib.timeout_add(1500, lambda: _record_frames(
            window, frames_dir, int(os.environ.get("CHRONICLE_DEMO_FPS", "10")))
            or False)

    # Open on a chosen article so the opening frame is representative.
    article = os.environ.get("CHRONICLE_SHOT_ARTICLE")
    if article and hasattr(window, "open_article"):
        try:
            window.open_article(int(article), remember=False)
        except Exception:                             # noqa: BLE001
            log.exception("could not open demo article %s", article)

    reader = window.reader
    steps = [
        (2400, lambda: reader.scroll_home()),
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


def _record_frames(window, directory: str, fps: int) -> None:
    """Snapshot the window on a timer, for assembling into a video."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("frame-*.png"):
        stale.unlink()

    interval = max(20, int(1000 / max(1, fps)))
    state = {"n": 0}

    def snap() -> bool:
        try:
            capture_window(window, out / f"frame-{state['n']:05d}.png")
        except Exception:                             # noqa: BLE001
            return True
        state["n"] += 1
        return True

    GLib.timeout_add(interval, snap)


# Selecting text is the one reader interaction a screenshot cannot stage on
# its own: the popup and the dictionary card only exist in response to a
# pointer. This drives the same handlers a real selection does, from inside
# the page, so what gets captured is the real control and not a mock-up.
_SELECT_JS = """
(function (word) {
  var prose = document.querySelector('.prose');
  if (!prose) return;
  var walker = document.createTreeWalker(prose, NodeFilter.SHOW_TEXT, null), n;
  while ((n = walker.nextNode())) {
    var i = n.nodeValue.indexOf(word);
    if (i < 0) continue;
    var range = document.createRange();
    range.setStart(n, i);
    range.setEnd(n, i + word.length);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    if (n.parentNode && n.parentNode.scrollIntoView) {
      n.parentNode.scrollIntoView({ block: 'center' });
    }
    // After the scroll has settled, not before: the popup hides itself when
    // the page moves under it, so a mouseup raised mid-scroll raises a strip
    // that the scroll's own event then takes away again.
    setTimeout(function () {
      document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    }, 300);
    return;
  }
})(%s);
"""

_PRESS_JS = """
Array.prototype.slice.call(document.querySelectorAll('#hl-pop button'))
  .filter(function (b) { return b.textContent === %s; })
  .forEach(function (b) {
    b.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  });
"""


def _select_in_reader(window, word: str, define: bool, delay: int = 1400) -> None:
    reader = getattr(window, "reader", None)
    if reader is None:
        return

    def select() -> bool:
        reader._run_js(_SELECT_JS % json.dumps(word))
        if define:
            # Long enough for the button strip to exist, and for the lookup it
            # starts to come back before the shutter.
            GLib.timeout_add(900, lambda: reader._run_js(
                _PRESS_JS % json.dumps("Define")) and False)
        return False

    # `load_html` is asynchronous: script evaluated the instant an article is
    # opened runs against the page being replaced, and finds nothing.
    GLib.timeout_add(delay, select)


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


def capture_reader(window, path: str | Path, done) -> bool:
    """Capture the reading surface through WebKit's own renderer.

    `Gtk.WidgetPaintable` snapshots the widget tree, and for a WebView that
    yields whatever texture the web process last handed the compositor. In
    practice a page that has just relaid out comes back with its text missing
    -- the marks and rules paint, the glyphs do not -- which looks exactly
    like a rendering bug in the page itself. Asking WebKit for the snapshot
    instead goes to the process that actually knows how to paint it.
    """
    reader = getattr(window, "reader", None)
    view = getattr(reader, "webview", None)
    if view is None:
        return False

    def finished(obj, res, _user):
        try:
            texture = obj.get_snapshot_finish(res)
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            texture.save_to_png(str(out))
            log.info("captured the reading surface to %s", out)
        except Exception:                             # noqa: BLE001
            log.exception("webkit snapshot failed")
        done()

    try:
        from gi.repository import WebKit
        view.get_snapshot(WebKit.SnapshotRegion.FULL_DOCUMENT,
                          WebKit.SnapshotOptions.NONE, None, finished, None)
    except Exception:                                 # noqa: BLE001
        log.exception("webkit snapshot unavailable")
        return False
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
    select = os.environ.get("CHRONICLE_SHOT_SELECT")

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
            # Select a library filter, so a shot can show one of the scopes
            # rather than only the default "All".
            wanted = os.environ.get("CHRONICLE_SHOT_SCOPE")
            if wanted and page == "library":
                button = window.library._buttons.get(wanted)
                if button is not None:
                    button.set_active(True)
            if scroll and page == "library":
                _scroll_library(window, float(scroll))
            if select and page == "reader":
                _select_in_reader(
                    window, select,
                    os.environ.get("CHRONICLE_SHOT_DEFINE") == "1")
            # Let the page settle (WebKit paint, list realisation) before capture.
            GLib.timeout_add(int(delay * 1000 * 0.45), finish)
        except Exception:                             # noqa: BLE001
            log.exception("capture setup failed")
            if should_quit:
                window.get_application().quit()
        return False

    def quit_later() -> None:
        if should_quit:
            GLib.timeout_add(200, lambda: window.get_application().quit() or False)

    def finish() -> bool:
        try:
            # The reader is a WebView, and only WebKit can render it reliably;
            # the other pages are ordinary widgets, which GSK renders fine.
            # FULL_DOCUMENT renders the whole article and drops the window
            # chrome, which is right for checking typography and useless for
            # checking the viewport -- scroll position and the bottom bar only
            # exist in a window shot.
            chrome = os.environ.get("CHRONICLE_SHOT_CHROME") == "1"
            if page == "reader" and not chrome and capture_reader(
                    window, out, quit_later):
                return False
            capture_window(window, out)
        except Exception:                             # noqa: BLE001
            log.exception("capture failed")
        quit_later()
        return False

    GLib.timeout_add(int(delay * 1000), run)
