"""Image caching.

Article images are downloaded once, stored content-addressed on disk and
re-pointed at a private URI scheme the reader serves locally. Two articles
embedding the same picture share one file, and the library keeps working
offline and after the original host takes the image down.
"""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from . import db, htmlutil, net, paths

log = logging.getLogger("chronicle.images")

SCHEME = "chronicle-img"
MAX_BYTES = 12_000_000
MIN_BYTES = 900          # below this it is a spacer, bug or tracking pixel

_EXT_BY_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "image/avif": ".avif", "image/bmp": ".bmp", "image/tiff": ".tif",
}
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),
    (b"<svg", "image/svg+xml", ".svg"),
    (b"<?xml", "image/svg+xml", ".svg"),
)


def _sniff(body: bytes, mime_hint: str) -> tuple[str, str] | None:
    head = body[:16]
    for magic, mime, ext in _MAGIC:
        if head.startswith(magic):
            if mime == "image/webp" and body[8:12] != b"WEBP":
                continue
            return mime, ext
    mime = (mime_hint or "").split(";")[0].strip().lower()
    if mime in _EXT_BY_MIME:
        return mime, _EXT_BY_MIME[mime]
    return None


def cache_images_for(conn, article_id: int, html: str, *,
                     max_workers: int = 4, should_stop=lambda: False) -> tuple[str, int]:
    """Download every image in `html`; return (rewritten_html, cached_count)."""
    urls = [u for u in htmlutil.image_urls(html) if u.lower().startswith("http")]
    if not urls:
        return html, 0

    mapping: dict[str, str] = {}
    todo: list[str] = []
    for url in urls:
        row = db.find_image(conn, url)
        if row and (paths.IMAGE_DIR / row["relpath"]).exists():
            mapping[url] = f"{SCHEME}://{row['relpath']}"
        else:
            todo.append(url)

    if todo and not should_stop():
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for url, result in zip(todo, pool.map(_download, todo)):
                if should_stop():
                    break
                if result is None:
                    continue
                digest, mime, ext, body = result
                relpath = paths.image_relpath(digest, ext)
                target = paths.image_path(digest, ext)
                if not target.exists():
                    try:
                        target.write_bytes(body)
                    except OSError as exc:
                        log.debug("image write failed %s: %s", url, exc)
                        continue
                db.record_image(conn, digest, url, mime, len(body), relpath)
                mapping[url] = f"{SCHEME}://{relpath}"

    return htmlutil.rewrite_image_srcs(html, mapping), len(mapping)


def _download(url: str):
    try:
        resp = net.fetch(url, timeout=40, retries=2, max_bytes=MAX_BYTES,
                         headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
    except net.FetchError:
        return None
    body = resp.body
    if len(body) < MIN_BYTES:
        return None
    sniffed = _sniff(body, resp.headers.get("content-type", ""))
    if sniffed is None:
        return None
    mime, ext = sniffed
    return hashlib.sha256(body).hexdigest(), mime, ext, body


def resolve(relpath: str):
    """Map a chronicle-img:// path back to a file on disk."""
    relpath = re.sub(r"^/+", "", relpath or "")
    if ".." in relpath or relpath.startswith("/"):
        return None
    target = (paths.IMAGE_DIR / relpath).resolve()
    try:
        target.relative_to(paths.IMAGE_DIR.resolve())
    except ValueError:
        return None
    return target if target.exists() else None


def prune_orphans(conn) -> int:
    """Delete cached files no article references any more."""
    referenced: set[str] = set()
    for row in conn.execute(
            "SELECT content_html FROM articles WHERE content_html IS NOT NULL"):
        for m in re.finditer(rf"{SCHEME}://([^\"'\s>]+)", row["content_html"] or ""):
            referenced.add(m.group(1))
    removed = 0
    for row in conn.execute("SELECT id, relpath FROM images").fetchall():
        if row["relpath"] in referenced:
            continue
        path = paths.IMAGE_DIR / row["relpath"]
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        conn.execute("DELETE FROM images WHERE id=?", (row["id"],))
        removed += 1
    return removed
