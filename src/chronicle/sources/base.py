"""Source plugin interface.

A source knows two things: how to enumerate everything a site has ever
published, and how to turn one of those URLs into clean reader HTML.
Sources deliberately differ — forcing a WordPress REST API and a 1990s
static site through one mechanism would lose information from both.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from .. import dates, htmlutil, net

log = logging.getLogger("chronicle.sources")


@dataclass
class Stub:
    """One discovered article. Content is optional — some feeds carry it."""
    guid: str
    url: str
    title: str
    date: dates.PubDate = dates.UNKNOWN
    author: str | None = None
    source_order: int = 0
    raw_html: str | None = None      # publisher-supplied body, still unsanitised
    base_url: str | None = None      # for resolving relative links in raw_html
    content_source: str = ""
    status_hint: str | None = None   # e.g. 'paywalled'
    extra: dict = field(default_factory=dict)  # adapter-specific, e.g. a snapshot id


@dataclass
class Content:
    html: str
    status: str = "ok"        # ok | partial | paywalled | empty | error
    source: str = "direct"    # direct | api | feed | wayback | browser


@dataclass
class Context:
    """Services a source may use during a sync, plus cancellation/progress."""
    progress: Callable[[str, float | None], None] = lambda msg, frac=None: None
    should_stop: Callable[[], bool] = lambda: False
    browser_fetch: Callable[[str], str] | None = None   # WebKit-backed fetcher
    # What the library already holds for this source: guid -> (dated,
    # content_state, date_confidence_rank) where content_state is "ok",
    # "gone" or "missing". Lets discovery skip refetching what is already
    # settled — and judge whether cheap new evidence improves a stored date.
    known: dict[str, tuple] | None = None
    # Guids fetched on an earlier sync and judged not to be articles, plus the
    # callback for reporting new such judgements. Together they stop a re-sync
    # paying one request per non-article page, every time.
    rejected: set[str] | None = None
    reject: Callable[[str], None] = lambda guid: None
    # Config hints a source learned during this sync (where the feed lives,
    # where the archive index is — or that there is none). Persisted by the
    # Syncer so later syncs skip the search.
    config_updates: dict = field(default_factory=dict)
    # A one-line summary the source may leave behind ("sitemap 138, feed 10 —
    # 142 unique"); recorded as the sync result the user sees.
    result_note: str = ""
    # "Fetch new posts" rather than a full archive scan: enumerate only the
    # routes that put the newest posts first (the feed, the front of the
    # archive index) and stop as soon as they run into what is already
    # archived. The expensive completeness routes -- the whole sitemap, deep
    # archive pagination -- are skipped. A source that cannot enumerate
    # cheaply may ignore this and do its normal pass; correctness never
    # depends on the flag, only cost.
    newest_only: bool = False
    # Publication date of the newest article already archived for this source,
    # as naive-UTC ISO, or None when the archive is empty. In newest_only mode
    # a candidate older than this is already accounted for, so enumeration can
    # stop rather than walk back through the whole history.
    newest_known: str | None = None

    def check(self) -> None:
        if self.should_stop():
            raise Cancelled()

    def say(self, msg: str, frac: float | None = None) -> None:
        self.progress(msg, frac)

    def settled(self, guid: str) -> bool:
        """Is this article already archived with both a date and content?"""
        k = (self.known or {}).get(guid)
        return bool(k and k[0] and k[1] == "ok")

    def no_direct(self, guid: str) -> bool:
        """Should a direct page fetch be skipped? True when the article is
        settled — or dated but permanently gone at the origin, where only a
        new route (such as a feed-supplied body) is worth trying."""
        k = (self.known or {}).get(guid)
        return bool(k and k[0] and k[1] in ("ok", "gone"))

    def known_rank(self, guid: str) -> int:
        """Confidence rank of the stored date for this article; -1 if none."""
        k = (self.known or {}).get(guid)
        if not k or not k[0]:
            return -1
        return k[2] if len(k) > 2 else 0

    def predates_archive(self, date) -> bool:
        """In newest_only mode, is this date older than everything we hold?

        Only ever True when a real comparison is possible: with no archive
        yet, or an undated candidate, the answer is False and the candidate
        is considered. A cheap enumeration must never *drop* an article on
        this test -- it only uses it to decide when it can stop early.
        """
        if not self.newest_only or not self.newest_known:
            return False
        return bool(date is not None and getattr(date, "known", False)
                    and date.iso is not None and date.iso < self.newest_known)


class Cancelled(Exception):
    pass


def probe_all(ctx: "Context", items, fn, *, workers: int = 5,
              label: str = "", total: int | None = None):
    """Run `fn` over `items` in parallel, yielding results in the input order.

    Discovery for a static site means fetching every page to read its metadata.
    Done one at a time that is minutes of waiting before a single article is
    stored, so it is worth overlapping -- while still yielding in order, since
    several adapters depend on the site's own ordering.

    Submits only a bounded window of work ahead of what has been yielded,
    rather than the whole list at once (as `pool.map` would): `net.fetch()` has
    no way to abort a request that is already in flight, so the only way to
    make cancellation responsive is to stop *starting new ones* the moment it
    is requested, and a large pre-submitted queue defeats that -- `Cancelled`
    would still have to wait for every future already handed to a worker.
    """
    from concurrent.futures import ThreadPoolExecutor

    items = list(items)
    total = total or len(items)
    workers = max(1, workers)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: dict = {}
        it = iter(enumerate(items))

        def fill():
            for idx, item in it:
                if ctx.should_stop():
                    break
                pending[idx] = pool.submit(fn, item)
                if len(pending) >= workers * 2:
                    break

        fill()
        next_idx = 0
        try:
            while pending:
                ctx.check()
                if next_idx not in pending:
                    fill()
                future = pending.pop(next_idx)
                result = future.result()
                next_idx += 1
                fill()
                done += 1
                if label and (done % 10 == 0 or done == total):
                    ctx.say(f"{label} {done}/{total}", done / max(1, total))
                yield result
        finally:
            for future in pending.values():
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)


def assess(html: str) -> str:
    """Is this a usable article body?

    Word count alone is the wrong test: plenty of posts are a single drawing
    or photograph with no prose at all, and those are complete articles.
    """
    if not html or not html.strip():
        return "empty"
    if htmlutil.word_count(html) >= 20:
        return "ok"
    if htmlutil.count_images(html) >= 1:
        return "ok"
    return "empty"


class Source:
    """Base class. Subclasses override discover() and usually fetch_content()."""

    plugin_id: str = "generic"
    display_name: str = "Generic"
    # CSS selectors tried, in order, before falling back to density scoring.
    content_selectors: list[str] = []
    # How many article bodies to fetch at once. Kept modest by default: these
    # are someone else's servers, and the point is a complete archive rather
    # than a fast one.
    fetch_concurrency: int = 4
    # Same, for the metadata pass that discovery does over a static site.
    discover_concurrency: int = 5
    # Images matching these substrings are decoration, not content.
    image_blocklist: tuple[str, ...] = (
        "trans_1x1.gif", "spacer.gif", "pixel.gif", "1x1.png",
        "gravatar.com/avatar", "feedburner", "feedblitz", "/emoji/",
        "stats.wordpress.com", "pixel.wp.com", "scorecardresearch",
        "doubleclick", "googlesyndication",
    )

    def __init__(self, row, config: dict | None = None):
        self.row = row
        self.id = row["id"] if row is not None else 0
        self.name = row["name"] if row is not None else self.display_name
        self.homepage = (row["homepage"] if row is not None else "") or ""
        self.config = config or {}

    # -- discovery ---------------------------------------------------------

    def discover(self, ctx: Context) -> Iterable[Stub]:
        raise NotImplementedError

    # -- content -----------------------------------------------------------

    def fetch_content(self, ctx: Context, url: str, stub_html: str | None = None,
                      base_url: str | None = None, extra: dict | None = None) -> Content:
        """Default: fetch the page and run the shared extractor."""
        if stub_html:
            return self.clean(stub_html, base_url or url, source="feed")
        resp = net.fetch(url)
        return self.clean(resp.text(), resp.url, source="direct")

    def clean(self, raw_html: str, base_url: str, *, source: str = "direct") -> Content:
        soup = htmlutil.parse(raw_html)
        node = htmlutil.extract_main(soup, self.content_selectors)
        html = htmlutil.sanitise(node, base_url)
        html = self.drop_decorative_images(html)
        html = self.postprocess(html)
        return Content(html, status=assess(html), source=source)

    def drop_decorative_images(self, html: str) -> str:
        soup = htmlutil.parse(html)
        for img in list(soup.find_all("img")):
            src = (img.get("src") or "").lower()
            if any(bad in src for bad in self.image_blocklist):
                img.decompose()
                continue
            try:
                w, h = int(img.get("width") or 0), int(img.get("height") or 0)
            except (TypeError, ValueError):
                w = h = 0
            if 0 < w <= 12 or 0 < h <= 12:
                img.decompose()
        return soup.decode()

    def postprocess(self, html: str) -> str:
        return html

    # -- helpers -----------------------------------------------------------

    @property
    def path_prefix(self) -> str:
        return (self.config.get("path_prefix") or "").rstrip("/")

    def scope_by_membership(self, urls) -> None:
        """Let the section's own listing define the section.

        Some indexes list articles that do not live under the index's path: a
        WordPress category or tag page is the common case -- every post it
        lists sits at the site root. Scoping such a source by path prefix
        rejects every article the index found, and the source silently
        archives nothing.

        So when the index's links are elsewhere, the *set of links the index
        published* becomes the scope. That still cannot pull in the whole
        site: only what this section's own pages listed is ever accepted.
        """
        # Accumulated, not replaced: the index's later pages each call this,
        # and page 2 must not revoke what page 1 vouched for.
        members = getattr(self, "_members", None)
        if members is None:
            members = self._members = set()
        members.update(net.canonical_url(u) for u in urls if u)

    def in_scope(self, url: str) -> bool:
        """Is this article inside the section the user asked to follow?"""
        prefix = self.path_prefix
        if not prefix:
            return True
        from urllib.parse import urlparse
        path = urlparse(url or "").path.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
        # A section whose listing points off its own path vouches for the
        # articles it listed, and for nothing else.
        members = getattr(self, "_members", None)
        return bool(members) and net.canonical_url(url) in members

    @staticmethod
    def guid_for(url: str) -> str:
        return net.canonical_url(url)
