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

    def check(self) -> None:
        if self.should_stop():
            raise Cancelled()

    def say(self, msg: str, frac: float | None = None) -> None:
        self.progress(msg, frac)


class Cancelled(Exception):
    pass


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

    @staticmethod
    def guid_for(url: str) -> str:
        return net.canonical_url(url)
