"""Command-line access to the library.

The GUI is the point, but a CLI makes the archive scriptable: build it on a
schedule, check what was imported, or export it for another device.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from . import dates, db, images, net, paths, sync, sources


def _fmt_int(n) -> str:
    return f"{n:,}".replace(",", " ")


def cmd_sources(args) -> int:
    conn = db.get_conn()
    rows = db.list_sources(conn)
    print(f"{'SLUG':<18} {'PLUGIN':<16} {'ON':<3} {'ARTICLES':>9}  LAST SYNC")
    for r in rows:
        n = conn.execute("SELECT COUNT(*) c FROM articles WHERE source_id=?",
                         (r["id"],)).fetchone()["c"]
        print(f"{r['slug']:<18} {r['plugin']:<16} {'yes' if r['enabled'] else 'no ':<3} "
              f"{_fmt_int(n):>9}  {r['last_sync_at'] or 'never'} "
              f"{r['last_sync_status'] or ''}")
    return 0


def cmd_add(args) -> int:
    conn = db.get_conn()
    print(f"Inspecting {args.url} …")
    try:
        spec = sources.detect(args.url)
    except sources.DetectError as exc:
        print(f"Cannot add this blog: {exc}", file=sys.stderr)
        return 1
    print(f"  detected: {spec['detected']}")
    if spec.get("partial"):
        print("  note    : this route exposes only recent posts, so the archive "
              "will be incomplete.")
    print(f"  name    : {spec['name']}")
    print(f"  plugin  : {spec['plugin']}")
    slug = args.slug or spec["homepage"].split("//")[-1].split("/")[0] \
        .replace("www.", "").replace(".", "-")
    config = dict(spec.get("config") or {})
    config["detected"] = spec["detected"]
    sid = db.add_source(conn, slug, spec["name"], spec["plugin"],
                        spec["homepage"], config)
    print(f"Added as '{slug}' (id {sid}). Run: chronicle sync --source {slug}")
    return 0


def cmd_remove(args) -> int:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sources WHERE slug=?", (args.slug,)).fetchone()
    if not row:
        print(f"No such source: {args.slug}", file=sys.stderr)
        return 1
    n = conn.execute("SELECT COUNT(*) c FROM articles WHERE source_id=?",
                     (row["id"],)).fetchone()["c"]
    if not args.yes:
        print(f"This deletes {row['name']} and its {_fmt_int(n)} articles. "
              f"Re-run with --yes to confirm.")
        return 1
    db.delete_source(conn, row["id"])
    print(f"Removed {row['name']} and {_fmt_int(n)} articles.")
    return 0


def cmd_rename(args) -> int:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sources WHERE slug=?", (args.slug,)).fetchone()
    if not row:
        print(f"No such source: {args.slug}", file=sys.stderr)
        return 1
    db.rename_source(conn, row["id"], args.name)
    print(f"{row['name']} is now {args.name}")
    return 0


def cmd_enable(args) -> int:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM sources WHERE slug=?", (args.slug,)).fetchone()
    if not row:
        print(f"No such source: {args.slug}", file=sys.stderr)
        return 1
    db.set_source_enabled(conn, row["id"], not args.off)
    print(f"{row['name']} is now {'disabled' if args.off else 'enabled'}.")
    return 0


def cmd_sync(args) -> int:
    conn = db.get_conn()
    if args.rate is not None:
        net.set_rate(args.rate)

    ids = None
    if args.source:
        ids = []
        for slug in args.source:
            row = conn.execute("SELECT id FROM sources WHERE slug=?", (slug,)).fetchone()
            if not row:
                print(f"No such source: {slug}", file=sys.stderr)
                return 1
            ids.append(row["id"])

    last = [0.0]

    def report(p):
        now = time.monotonic()
        if now - last[0] < 0.4 and not p.done:
            return
        last[0] = now
        bar = ""
        if p.fraction is not None:
            filled = int(p.fraction * 24)
            bar = " [" + "#" * filled + "." * (24 - filled) + "]"
        line = (f"\r{p.message[:78]:<78}{bar} "
                f"new={p.new} got={p.fetched} fail={p.failed}")
        sys.stdout.write(line[:150])
        sys.stdout.flush()
        if p.done:
            sys.stdout.write("\n")

    syncer = sync.Syncer(on_progress=report)
    result = syncer.sync_all(ids, fetch_content=not args.no_content,
                             cache_images=not args.no_images,
                             newest_only=args.newest)
    if result.error:
        print(f"\nError: {result.error}", file=sys.stderr)
        return 1
    return 0


def cmd_stats(args) -> int:
    conn = db.get_conn()
    s = db.stats(conn)
    counts = db.queue_counts(conn)
    print("Chronicle library")
    print(f"  database      {paths.DB_PATH}")
    print(f"  articles      {_fmt_int(s['articles'])} "
          f"({_fmt_int(s['with_content'])} with content)")
    print(f"  images        {_fmt_int(s['images'])} "
          f"({s['image_bytes'] / 1e6:.1f} MB)")
    print(f"  span          {(s['oldest'] or '?')[:10]} → {(s['newest'] or '?')[:10]}")
    print(f"  queue         {_fmt_int(counts['all'])} readable, "
          f"{_fmt_int(counts['unread'])} unread, "
          f"{_fmt_int(counts['favourites'])} favourites")
    if counts["undated"]:
        print(f"  undated       {_fmt_int(counts['undated'])}")
    print()
    print(f"  {'SOURCE':<20} {'ARTICLES':>8} {'DATED':>7}  DATE CONFIDENCE")
    for r in db.list_sources(conn):
        n = conn.execute("SELECT COUNT(*) c FROM articles WHERE source_id=?",
                         (r["id"],)).fetchone()["c"]
        if not n:
            continue
        dated = conn.execute(
            "SELECT COUNT(*) c FROM articles WHERE source_id=? AND published_at IS NOT NULL",
            (r["id"],)).fetchone()["c"]
        conf = conn.execute(
            "SELECT date_confidence k, COUNT(*) c FROM articles WHERE source_id=? "
            "GROUP BY k ORDER BY c DESC", (r["id"],)).fetchall()
        blurb = ", ".join(f"{x['k']} {x['c']}" for x in conf)
        print(f"  {r['name']:<20} {_fmt_int(n):>8} {_fmt_int(dated):>7}  {blurb}")
    return 0


def cmd_queue(args) -> int:
    conn = db.get_conn()
    rows = db.queue(conn, scope=args.scope, limit=args.limit)
    for r in rows:
        d = dates.format_short(r["published_at"], r["date_precision"])
        flag = "*" if r["favourite_at"] else " "
        read = "." if r["read_at"] else " "
        print(f"{d:<10} {flag}{read} {r['source_name'][:18]:<18} {r['title'][:64]}")
    print(f"\n{len(rows)} shown ({args.scope})")
    return 0


def cmd_export(args) -> int:
    """Dump the library as JSON — the seam a Kindle/EPUB exporter builds on."""
    conn = db.get_conn()
    rows = db.queue(conn, scope=args.scope, limit=args.limit)
    out = []
    for r in rows:
        art = db.get_article(conn, r["id"])
        out.append({
            "title": art["title"], "source": art["source_name"],
            "url": art["url"], "published_at": art["published_at"],
            "date_precision": art["date_precision"],
            "date_confidence": art["date_confidence"],
            "date_source": art["date_source"],
            "word_count": art["word_count"],
            "content_html": art["content_html"] if args.content else None,
        })
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
    return 0


def cmd_prune(args) -> int:
    conn = db.get_conn()
    n = images.prune_orphans(conn)
    print(f"Removed {n} orphaned images.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chronicle", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("sources", help="list followed blogs").set_defaults(fn=cmd_sources)

    a = sub.add_parser("add", help="follow a new blog")
    a.add_argument("url"); a.add_argument("--slug")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("remove", help="stop following a blog")
    r.add_argument("slug"); r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=cmd_remove)

    rn = sub.add_parser("rename", help="change a blog's display name")
    rn.add_argument("slug"); rn.add_argument("name")
    rn.set_defaults(fn=cmd_rename)

    e = sub.add_parser("enable", help="enable or disable a blog")
    e.add_argument("slug"); e.add_argument("--off", action="store_true")
    e.set_defaults(fn=cmd_enable)

    s = sub.add_parser("sync", help="build or update the archive")
    s.add_argument("--source", action="append", help="slug (repeatable)")
    s.add_argument("--no-content", action="store_true")
    s.add_argument("--no-images", action="store_true")
    s.add_argument("--rate", type=float, help="seconds between requests per host")
    s.add_argument("--newest", action="store_true",
                   help="fetch only posts newer than what is archived, instead "
                        "of scanning each blog's whole history")
    s.set_defaults(fn=cmd_sync)

    sub.add_parser("stats", help="library overview").set_defaults(fn=cmd_stats)

    q = sub.add_parser("queue", help="show the reading queue")
    q.add_argument("--scope", default="all",
                   choices=["all", "unread", "read", "favourites"])
    q.add_argument("--limit", type=int, default=40)
    q.set_defaults(fn=cmd_queue)

    x = sub.add_parser("export", help="dump the library as JSON")
    x.add_argument("--scope", default="all")
    x.add_argument("--limit", type=int, default=100000)
    x.add_argument("--content", action="store_true")
    x.set_defaults(fn=cmd_export)

    sub.add_parser("prune", help="delete unreferenced cached images").set_defaults(fn=cmd_prune)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    paths.ensure_dirs()
    if not db.acquire_library_lock():
        print("The library is open in another Chronicle process "
              "(close the app first).", file=sys.stderr)
        return 2
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        try:
            db.checkpoint(db.get_conn())
        except Exception:                             # noqa: BLE001
            pass
        db.release_library_lock()


if __name__ == "__main__":
    sys.exit(main())
