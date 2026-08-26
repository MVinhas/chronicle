"""Internet Archive fallback, shared by any source whose origin is unreachable.

Two independent things live here: `is_dead()`, a quick check for "this site
does not currently answer at all" (used by `detect()` to redirect a brand-new
blog straight to its Wayback archive instead of failing outright), and
`list_snapshots()` / `fetch_snapshot()`, which do the CDX query and page fetch
that `mrmoneymustache.py` pioneered. That adapter needed this because its
origin actively blocks bots; here it also covers a site that is simply down.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .. import net

CDX = ("http://web.archive.org/cdx/search/cdx?url={host}&matchType=domain"
       "&fl=original,timestamp&collapse=urlkey"
       "&filter=statuscode:200&filter=mimetype:text/html{window}")
WAYBACK = "https://web.archive.org/web/{ts}id_/{url}"
AVAILABLE = "http://archive.org/wayback/available?url={url}"


def is_dead(base: str) -> bool:
    """Does the site fail to answer at all (DNS/connection), not just 404/403?

    A `FetchError` with a `status` means the server answered — that is a live
    site being fussy, not a dead one. `status is None` means the request never
    got a response at all, which is what "the site is down" looks like.
    """
    try:
        net.fetch(base, timeout=15, retries=1, use_cache=False)
        return False
    except net.FetchError as exc:
        return exc.status is None


def list_snapshots(host: str, *, since_year: int | None = None,
                   timeout: float = 300) -> list[tuple[str, str]]:
    """Every archived URL under `host`, each with its most recent snapshot id."""
    window = f"&from={since_year}" if since_year else ""
    url = CDX.format(host=host.replace("www.", ""), window=window)
    try:
        text = net.fetch_text(url, timeout=timeout, retries=2, max_bytes=80_000_000)
    except net.FetchError:
        return []

    best: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        raw, timestamp = parts[0], parts[1]
        try:
            key = urlparse(raw).path.rstrip("/") or "/"
        except ValueError:
            continue
        if key not in best or timestamp > best[key][1]:
            best[key] = (raw, timestamp)
    return list(best.values())


def best_snapshot(url: str) -> str | None:
    """Timestamp of the closest archived snapshot of a single URL, if any."""
    clean = url.replace("https://", "").replace("http://", "")
    try:
        data = net.fetch_json(AVAILABLE.format(url=clean), timeout=45, retries=2)
    except Exception:
        return None
    snap = (data.get("archived_snapshots") or {}).get("closest") or {}
    return snap.get("timestamp") if snap.get("available") else None


def fetch_snapshot(url: str, snapshot: str | None = None, *, timeout: float = 90):
    """The archived HTML for `url`, resolving a snapshot id if none is given."""
    ts = snapshot or best_snapshot(url)
    if not ts:
        return None
    return net.fetch(WAYBACK.format(ts=ts, url=url), timeout=timeout)


def strip_banner(soup) -> None:
    """Remove the Wayback Machine's own injected chrome from an archived page."""
    for node in soup.select('[id^="wm-"]'):
        node.decompose()
