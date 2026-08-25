"""Application entry point."""
from __future__ import annotations

import logging
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__, db, paths, sync  # noqa: E402


class ChronicleApp(Adw.Application):
    def __init__(self):
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS
        if os.environ.get("CHRONICLE_DEV"):
            # A dev run may have no session bus; don't let registration fail it.
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.window = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        paths.ensure_dirs()

        self.locked = db.acquire_library_lock()
        if self.locked:
            db.get_conn()   # open and migrate the library before any UI is built

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q", "<Control>w"])

    def do_activate(self) -> None:
        from .ui.window import MainWindow
        from .ui import capture
        if not self.locked:
            capture.arm(self._present_busy())
            return
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()
        capture.arm(self.window)
        capture.demo(self.window)

    def _present_busy(self):
        """Another process holds the library; showing it would show wrong data."""
        window = Adw.ApplicationWindow(application=self, title="Chronicle",
                                       default_width=480, default_height=300)
        page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title="Library in use",
            description="Another Chronicle process has the library open. "
                        "Close it and try again.")
        button = Gtk.Button(label="Quit", halign=Gtk.Align.CENTER,
                            css_classes=["pill", "suggested-action"])
        button.connect("clicked", lambda *_: self.quit())
        page.set_child(button)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        view.set_content(page)
        window.set_content(view)
        window.present()
        window.show_page = lambda *_: None   # capture.arm() calls this
        return window

    def do_shutdown(self) -> None:
        try:
            if self.locked:
                db.checkpoint(db.get_conn())
        except Exception:                             # noqa: BLE001
            pass
        db.release_library_lock()
        Adw.Application.do_shutdown(self)


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    GLib.set_application_name("Chronicle")
    GLib.set_prgname(APP_ID)
    return ChronicleApp().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
