"""Blog management: add, remove, enable/disable and update each archive."""
from __future__ import annotations

import json
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from .. import db, sources  # noqa: E402


def _format_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "< 1 min"
    minutes = seconds // 60
    if minutes < 60:
        return f"~{minutes} min"
    hours = minutes // 60
    return f"~{hours}h {minutes % 60}min"


class SourcesView(Gtk.Box):
    __gtype_name__ = "ChronicleSourcesView"

    __gsignals__ = {
        # (source_ids or None, full_scan) — full_scan False means "fetch new
        # posts": only what each blog has published since its last update.
        "sync-requested": (GObject.SignalFlags.RUN_FIRST, None, (object, bool)),
        "cancel-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "library-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, get_conn, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.get_conn = get_conn
        self.window = window
        self._rows: list[Adw.ActionRow] = []
        self._build_ui()

    def _build_ui(self) -> None:
        self.scroller = Gtk.ScrolledWindow(vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=720, margin_top=18, margin_bottom=28,
                          margin_start=14, margin_end=14)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(self.content)
        self.scroller.set_child(clamp)

        # -- progress strip, shown only while a sync runs
        self.progress_group = Adw.PreferencesGroup(title="Updating")
        self.progress_label = Gtk.Label(xalign=0, wrap=True,
                                        css_classes=["dim-label", "caption"])
        self.progress_bar = Gtk.ProgressBar(show_text=False, margin_top=6)
        self.progress_stats = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        stop = Gtk.Button(label="Stop", halign=Gtk.Align.END, margin_top=8,
                          css_classes=["destructive-action"])
        stop.connect("clicked", lambda *_: self.emit("cancel-requested"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=10, margin_bottom=10,
                      margin_start=12, margin_end=12)
        for w in (self.progress_label, self.progress_bar, self.progress_stats, stop):
            box.append(w)
        self.progress_group.add(box)
        self.progress_group.set_visible(False)
        self.content.append(self.progress_group)

        self.group = Adw.PreferencesGroup(
            title="Blogs you follow",
            description="Each blog is archived from every route it offers — a "
                        "REST or content API where one exists, otherwise its "
                        "feed, sitemaps and archive pages combined.")

        actions = Gtk.Box(spacing=8)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a blog",
                         css_classes=["flat"])
        add.connect("clicked", lambda *_: self.open_add_dialog())

        # Two different jobs, so two buttons rather than one that sometimes
        # takes four seconds and sometimes forty minutes. The cheap one is the
        # routine action and gets the emphasis; the expensive one sits beside
        # it, plainly labelled, because it is the one you reach for rarely.
        fetch = Gtk.Button(label="Fetch new posts",
                           tooltip_text="Get what these blogs have published "
                                        "since their last update (F5)",
                           css_classes=["suggested-action"])
        fetch.connect("clicked", lambda *_: self.emit("sync-requested", None, False))

        full = Gtk.Button(label="Full archive scan",
                          tooltip_text="Re-examine every blog's whole history. "
                                       "Slow — use it to fill gaps or after "
                                       "adding a blog.",
                          css_classes=["flat"])
        full.connect("clicked", lambda *_: self.emit("sync-requested", None, True))

        actions.append(add)
        actions.append(full)
        actions.append(fetch)
        self.group.set_header_suffix(actions)
        self.content.append(self.group)

        self.empty_hint = Adw.PreferencesGroup()
        hint = Adw.ActionRow(
            title="No blogs yet",
            subtitle="Add the address of a blog you read and Chronicle will "
                     "work out how to recover its archive. Sites that need "
                     "special handling can get a purpose-built adapter — "
                     "request one on the issue tracker.")
        hint.set_subtitle_lines(4)
        issues = Gtk.Button(label="Request an adapter", valign=Gtk.Align.CENTER,
                            css_classes=["flat"])
        issues.connect("clicked", lambda *_: Gtk.UriLauncher(
            uri="https://github.com/MVinhas/chronicle/issues/new?labels=adapter-request"
        ).launch(self.window, None, None, None))
        hint.add_suffix(issues)
        self.empty_hint.add(hint)
        self.content.append(self.empty_hint)

        self.stats_group = Adw.PreferencesGroup(title="Library")
        self.stats_row = Adw.ActionRow(title="—", subtitle="")
        self.stats_group.add(self.stats_row)
        self.content.append(self.stats_group)

        self.append(self.scroller)

    # -- rendering ---------------------------------------------------------

    def reload(self) -> None:
        for row in self._rows:
            self.group.remove(row)
        self._rows.clear()

        conn = self.get_conn()
        for src in db.list_sources(conn):
            count = conn.execute(
                "SELECT COUNT(*) c FROM articles WHERE source_id=?",
                (src["id"],)).fetchone()["c"]
            ready = conn.execute(
                "SELECT COUNT(*) c FROM articles WHERE source_id=? AND "
                "content_status IN ('ok','partial','paywalled')",
                (src["id"],)).fetchone()["c"]
            span = conn.execute(
                "SELECT MIN(published_at) a, MAX(published_at) b FROM articles "
                "WHERE source_id=? AND published_at IS NOT NULL",
                (src["id"],)).fetchone()

            bits = [f"{ready:,} readable".replace(",", " ")]
            if count != ready:
                bits.append(f"{count - ready} pending")
            if span["a"]:
                bits.append(f"{span['a'][:4]}–{span['b'][:4]}")
            if src["last_sync_at"]:
                bits.append(f"updated {src['last_sync_at'][:10]}")
            elif not count:
                bits.append("not built yet")
            if src["last_sync_status"] == "error":
                bits.append("last update failed")

            row = Adw.ActionRow(title=src["name"], subtitle="  ·  ".join(bits))
            row.set_subtitle_lines(2)

            # How this archive is built, and what the last update found —
            # useful detail that would crowd the subtitle.
            try:
                config = json.loads(src["config"] or "{}")
            except json.JSONDecodeError:
                config = {}
            detail = [config.get("detected") or ""]
            if src["last_sync_message"]:
                detail.append(f"Last update: {src['last_sync_message']}")
            detail = "\n".join(d for d in detail if d)
            if detail:
                row.set_tooltip_text(detail)

            toggle = Gtk.Switch(active=bool(src["enabled"]), valign=Gtk.Align.CENTER,
                                tooltip_text="Include in the reading queue")
            toggle.connect("state-set", self._on_toggle, src["id"])
            row.add_suffix(toggle)

            rename = Gtk.Button(icon_name="document-edit-symbolic",
                                valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Rename this blog")
            rename.connect("clicked", self._on_rename, src)
            row.add_suffix(rename)

            sync_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                  valign=Gtk.Align.CENTER, css_classes=["flat"],
                                  tooltip_text="Fetch this blog's new posts")
            sync_btn.connect("clicked", self._on_sync_one, src["id"])
            row.add_suffix(sync_btn)

            scan_btn = Gtk.Button(icon_name="folder-download-symbolic",
                                  valign=Gtk.Align.CENTER, css_classes=["flat"],
                                  tooltip_text="Scan this blog's whole history")
            scan_btn.connect("clicked", self._on_scan_one, src["id"])
            row.add_suffix(scan_btn)

            remove = Gtk.Button(icon_name="user-trash-symbolic",
                                valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Remove this blog")
            remove.connect("clicked", self._on_remove, src)
            row.add_suffix(remove)

            self.group.add(row)
            self._rows.append(row)

        self.empty_hint.set_visible(not self._rows)
        self.stats_group.set_visible(bool(self._rows))

        s = db.stats(conn)
        self.stats_row.set_title(
            f"{s['articles']:,} articles  ·  {s['images']:,} images "
            f"({s['image_bytes'] / 1e6:.0f} MB)".replace(",", " "))
        if s["oldest"]:
            self.stats_row.set_subtitle(
                f"Spanning {s['oldest'][:10]} to {s['newest'][:10]}")

    # -- progress ----------------------------------------------------------

    def set_progress(self, prog) -> None:
        if prog.done:
            self.progress_group.set_visible(False)
            return
        self.progress_group.set_visible(True)
        self.progress_label.set_label(prog.message or "Working…")
        if prog.fraction is None:
            self.progress_bar.pulse()
        else:
            self.progress_bar.set_fraction(max(0.0, min(1.0, prog.fraction)))
        eta = prog.eta_seconds
        self.progress_stats.set_label(
            f"{prog.discovered} checked  ·  {prog.new} new  ·  "
            f"{prog.fetched} retrieved" +
            (f"  ·  {prog.failed} unavailable" if prog.failed else "") +
            (f"  ·  {_format_eta(eta)} left" if eta is not None else ""))

    # -- actions -----------------------------------------------------------

    def _on_toggle(self, _switch, state, source_id) -> bool:
        db.set_source_enabled(self.get_conn(), source_id, state)
        self.emit("library-changed")
        return False

    def _on_sync_one(self, _btn, source_id) -> None:
        self.emit("sync-requested", [source_id], False)

    def _on_scan_one(self, _btn, source_id) -> None:
        self.emit("sync-requested", [source_id], True)

    def _on_rename(self, _btn, src) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Rename {src['name']}",
            body="Chronicle guesses a name from the site itself, which is not "
                 "always the one you would pick.")
        entry = Gtk.Entry(text=src["name"], activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")

        def done(_d, response):
            if response == "rename":
                db.rename_source(self.get_conn(), src["id"], entry.get_text())
                self.reload()
                self.emit("library-changed")

        dialog.connect("response", done)
        dialog.present(self.window)
        entry.grab_focus()

    def _on_remove(self, _btn, src) -> None:
        conn = self.get_conn()
        count = conn.execute("SELECT COUNT(*) c FROM articles WHERE source_id=?",
                             (src["id"],)).fetchone()["c"]
        dialog = Adw.AlertDialog(
            heading=f"Remove {src['name']}?",
            body=(f"This deletes {count:,} archived articles and their reading "
                  f"history. The blog can be added again later, but the archive "
                  f"would need rebuilding.").replace(",", " "))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_remove_response, src["id"])
        dialog.present(self.window)

    def _on_remove_response(self, _dialog, response, source_id) -> None:
        if response != "remove":
            return
        db.delete_source(self.get_conn(), source_id)
        self.reload()
        self.emit("library-changed")

    # -- add ---------------------------------------------------------------

    def open_add_dialog(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Add a blog",
            body="Paste the address of a blog. Chronicle works out how to build "
                 "its archive — a REST API, a sitemap, or a feed.")
        entry = Gtk.Entry(placeholder_text="https://example.com",
                          input_purpose=Gtk.InputPurpose.URL, activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("add")
        dialog.set_response_enabled("add", False)

        def on_changed(_e):
            text = entry.get_text().strip()
            dialog.set_response_enabled("add", len(text) > 3)
        entry.connect("changed", on_changed)
        dialog.connect("response", self._on_add_response, entry)
        dialog.present(self.window)
        entry.grab_focus()

    def _on_add_response(self, dialog, response, entry) -> None:
        if response != "add":
            return
        url = entry.get_text().strip()
        toast = Adw.Toast(title=f"Inspecting {url}…", timeout=3)
        self.window.toasts.add_toast(toast)
        threading.Thread(target=self._detect_and_add, args=(url,), daemon=True).start()

    def _detect_and_add(self, url: str) -> None:
        try:
            spec = sources.detect(url)
        except Exception as exc:                      # noqa: BLE001
            GLib.idle_add(self._added, None, str(exc))
            return
        conn = db.connect()
        db.init(conn)
        slug = spec["homepage"].split("//")[-1].split("/")[0] \
            .replace("www.", "").replace(".", "-")
        config = dict(spec.get("config") or {})
        config["detected"] = spec["detected"]
        try:
            db.add_source(conn, slug, spec["name"], spec["plugin"],
                          spec["homepage"], config)
        except Exception as exc:                      # noqa: BLE001
            GLib.idle_add(self._added, None, str(exc))
            return
        finally:
            conn.close()
        GLib.idle_add(self._added, spec, None)

    def _added(self, spec, error) -> bool:
        if error or spec is None:
            self.window.toasts.add_toast(
                Adw.Toast(title=f"Could not add that blog: {error}", timeout=6))
            return False
        self.reload()
        self.emit("library-changed")
        title = f"Added {spec['name']} — via {spec['detected']}"
        toast = Adw.Toast(title=title, button_label="Build archive",
                          timeout=12 if spec.get("partial") else 8)
        # A blog just added has no history at all, so this is the full scan.
        toast.connect("button-clicked",
                      lambda *_: self.emit("sync-requested", None, True))
        self.window.toasts.add_toast(toast)
        return False
