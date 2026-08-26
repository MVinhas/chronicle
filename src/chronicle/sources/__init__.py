"""Source registry and auto-detection for newly added blogs."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .. import net
from . import discovery, wayback
from .base import Cancelled, Content, Context, Source, Stub
from .discovery import extract_date
from .generic import GenericSource
from .ghost import GhostSource
from .gwern import GwernSource
from .mmm import MrMoneyMustacheSource
from .paulgraham import PaulGrahamSource
from .wordpress import WordPressSource

REGISTRY: dict[str, type[Source]] = {
    cls.plugin_id: cls for cls in (
        GwernSource, PaulGrahamSource, WordPressSource, GhostSource,
        MrMoneyMustacheSource, GenericSource,
    )
}


def build(row, config: dict | None = None) -> Source:
    cls = REGISTRY.get(row["plugin"], GenericSource)
    return cls(row, config)


# Chronicle ships with no blogs configured — the library starts empty and you
# add what you read. It does ship *recipes*: adapters for sites whose archives
# cannot be recovered by generic means. A recipe is applied automatically when
# you add a matching site, and is otherwise inert.
#
# Want a recipe for a site that generic detection handles badly? Open an issue:
# https://github.com/MVinhas/chronicle/issues/new?labels=adapter-request
RECIPES = [
    dict(plugin="gwern", name="Gwern", hosts=("gwern.net",),
         note="Dublin Core metadata gives exact original publication dates, "
              "kept separate from the revision dates this site also publishes."),
    dict(plugin="paulgraham", name="Paul Graham", hosts=("paulgraham.com",),
         note="No date metadata exists; dates are read from each essay's "
              "dateline, giving month precision."),
    dict(plugin="mrmoneymustache", name="Mr. Money Mustache",
         hosts=("mrmoneymustache.com",),
         note="Cloudflare-protected; the archive is rebuilt from the Internet "
              "Archive and dated from each permalink."),
]


def recipe_for(host: str) -> dict | None:
    host = (host or "").lower().replace("www.", "")
    for recipe in RECIPES:
        if any(host == h or host.endswith("." + h) for h in recipe["hosts"]):
            return recipe
    return None


# WordPress answers on one of these even when pretty permalinks are off.
_WP_PATHS = ("/wp-json/wp/v2/posts?per_page=1",
             "/?rest_route=/wp/v2/posts&per_page=1")

_GHOST_KEY_RE = re.compile(
    r'(?:key|apiKey)["\']?\s*[:=]\s*["\']([0-9a-f]{26})["\']', re.I)

# Paths that conventionally mean "everything I've published", not a section
# of the site scoped to that path -- see the path-handling comment in detect().
_LISTING_PATHS = {p.rstrip("/") for p in discovery.ARCHIVE_PATHS
                  if p not in ("", "/")}


def detect(url: str) -> dict:
    """Work out how to ingest a site the user just added.

    An API is preferred when one exists — it enumerates every post with the
    publisher's own timestamps. Otherwise the generic source takes over and
    merges every route it can find (feed, sitemaps, archive pages), so
    detection only needs to record useful hints, not choose a single winner.
    """
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url

    # The user may have pasted the feed itself (a FeedBurner mirror, a bare
    # /feed.xml) rather than the site. Detecting off the feed's own host would
    # misfire -- a mirror host has no WordPress API or sitemap of its own, and
    # is not what any recipe is keyed on. Re-run detection against the site
    # the feed says it belongs to, keeping the feed as a config hint: it is
    # sometimes the only reachable route into a Cloudflare-blocked origin.
    home_link = _feed_home_link(url)
    if home_link and urlparse(home_link).netloc.lower() != urlparse(url).netloc.lower():
        result = detect(home_link)
        result.setdefault("config", {}).setdefault("feed", url)
        return result

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    base = f"{parsed.scheme}://{parsed.netloc}"

    recipe = recipe_for(host)
    if recipe is not None:
        return {"plugin": recipe["plugin"], "name": recipe["name"],
                "homepage": base, "config": {}, "partial": False,
                "detected": "built-in recipe", "note": recipe["note"]}

    # One homepage fetch serves everything below: the site title, the Ghost
    # key, the feed link, and the is-it-alive check.
    home_html: str | None = None
    home_dead = False
    try:
        home_html = net.fetch_text(base, timeout=25, retries=2)
    except net.FetchError as exc:
        # `status is None` means the request never got a response at all --
        # DNS failure or connection refused. A 403/404 is a live site being
        # fussy, not a dead one.
        home_dead = exc.status is None

    # Title comes from the URL as given, so a section index names itself after
    # the section rather than the site. The bare hostname is the honest
    # fallback: truncating at the first dot turns nav.al into "Nav", which is
    # simply wrong.
    name = None
    if parsed.path.rstrip("/"):
        name = _site_title_from(_fetch_quiet(url))
    if not name:
        name = _site_title_from(home_html)
    name = name or host.replace("www.", "")

    # The site does not answer at all. There is no API or sitemap to probe on
    # a server that is not there, so the only route left is whatever the
    # Internet Archive has crawled.
    if home_dead and wayback.is_dead(base):
        return {"plugin": "generic", "name": name, "homepage": base,
                "config": {"strategy": "wayback"}, "partial": False,
                "detected": "site unreachable — rebuilding the archive from "
                            "the Internet Archive"}

    config: dict = {}
    feed_link = _feed_link_from(base, home_html)
    if feed_link:
        config["feed"] = feed_link

    # A path means the user asked for one section, not the whole site -- unless
    # the path itself is a conventional "everything I've published" listing
    # (ribbonfarm.com/archive/ and the like). Those enumerate posts that live
    # all over the site, not under that path, so scoping to it would reject
    # every post the index actually finds.
    section = parsed.path.rstrip("/")
    if section and section not in ("", "/"):
        cfg = dict(config, strategy="archive", index=section + "/")
        if section.lower() in _LISTING_PATHS:
            return {"plugin": "generic", "name": name, "homepage": base,
                    "config": cfg, "partial": False,
                    "detected": f"archive index at {section}/, following its pagination"}
        cfg["path_prefix"] = section
        return {"plugin": "generic", "name": name, "homepage": base,
                "config": cfg, "partial": False,
                "detected": f"section index at {section}/, following its pagination"}

    # 1. WordPress REST API -- every post, publisher timestamps, full bodies.
    for path in _WP_PATHS:
        try:
            resp = net.fetch(base + path, headers={"Accept": "application/json"},
                             timeout=25, retries=2)
        except net.FetchError:
            continue
        if resp.status == 200 and resp.body.strip().startswith(b"["):
            total = resp.headers.get("x-wp-total")
            root = (f"{base}/wp-json/wp/v2" if path.startswith("/wp-json")
                    else f"{base}/?rest_route=/wp/v2")
            return {"plugin": "wordpress", "name": name, "homepage": base,
                    "config": {"api_root": root}, "partial": False,
                    "detected": f"WordPress REST API"
                                f"{f' ({total} posts)' if total else ''}"}

    # 2. Ghost -- the front end embeds its own public content key.
    if home_html:
        m = _GHOST_KEY_RE.search(home_html)
        if m and ("ghost" in home_html.lower() or "/ghost/api/" in home_html):
            return {"plugin": "ghost", "name": name, "homepage": base,
                    "config": {"content_key": m.group(1)}, "partial": False,
                    "detected": "Ghost Content API"}

    # 3. No API. The generic source merges every remaining route — sitemap,
    #    archive pages, feed — so detection just records where a sitemap is
    #    (robots.txt first, then convention) to save the first sync a probe.
    sitemap = _find_sitemap(base)
    if sitemap:
        config["sitemap"] = sitemap
        return {"plugin": "generic", "name": name, "homepage": base,
                "config": config, "partial": False,
                "detected": f"sitemap ({sitemap}), merged with the blog's "
                            f"feed and archive pages"}

    return {"plugin": "generic", "name": name, "homepage": base,
            "config": config, "partial": True,
            "detected": "no API or sitemap — will merge the blog's archive "
                        "pages and feed"}


def _fetch_quiet(url: str) -> str | None:
    try:
        return net.fetch_text(url, timeout=20, retries=1)
    except net.FetchError:
        return None


def _site_title_from(html: str | None) -> str | None:
    if not html:
        return None
    from .. import htmlutil
    soup = htmlutil.parse(html)
    for getter in (lambda: htmlutil.meta_content(soup, "og:site_name"),
                   lambda: soup.title.get_text(strip=True) if soup.title else None):
        val = getter()
        if val:
            val = re.split(r"\s*[|–—·]\s*", val.strip())[0].strip()
            if 2 <= len(val) <= 48:
                return val
    return None


def _feed_link_from(base: str, html: str | None) -> str | None:
    if not html:
        return None
    from .. import htmlutil
    soup = htmlutil.parse(html)
    link = soup.find("link", attrs={"type": re.compile(
        r"application/(rss|atom)\+xml")})
    if link and link.get("href"):
        return net.absolutise(base + "/", link["href"])
    return None


def _find_sitemap(base: str) -> str | None:
    """Where the site's sitemap lives, if anywhere — existence check only."""
    hints = discovery.robots_sitemaps(base) + list(discovery.SITEMAP_HINTS)
    for hint in hints:
        target = hint if hint.startswith("http") else base + hint
        try:
            # use_cache=False: a truncated probe body must never be cached as
            # if it were the whole sitemap.
            resp = net.fetch(target, timeout=20, retries=1, max_bytes=4000,
                             use_cache=False)
        except net.FetchError:
            continue
        head = resp.body[:2000]
        if b"<urlset" in head or b"<sitemapindex" in head:
            return hint
    return None


_RSS_LINK_RE = re.compile(r"<link>\s*(https?://[^<\s]+)\s*</link>")
_ATOM_LINK_RE = re.compile(
    r'<link[^>]*rel=["\']alternate["\'][^>]*href=["\'](https?://[^"\']+)["\']')


def _feed_home_link(url: str) -> str | None:
    """If `url` is itself a feed, the site homepage it says it belongs to.

    A feed mirror (FeedBurner and the like) or a bare feed path has no
    WordPress API, sitemap or Ghost key of its own to probe -- those all live
    on the origin. RSS's `<channel><link>` and Atom's `rel="alternate"` link
    both name that origin directly.
    """
    try:
        resp = net.fetch(url, timeout=20, retries=1)
    except net.FetchError:
        return None
    head = resp.body[:4000]
    if b"<rss" not in head and b"<feed" not in head:
        return None
    text = resp.text()[:4000]
    m = _RSS_LINK_RE.search(text) or _ATOM_LINK_RE.search(text)
    return m.group(1) if m else None


__all__ = ["REGISTRY", "RECIPES", "recipe_for", "build", "detect", "Source",
           "Stub", "Content", "Context", "Cancelled", "extract_date"]
