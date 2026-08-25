"""Source registry and auto-detection for newly added blogs."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .. import net
from .base import Cancelled, Content, Context, Source, Stub
from .generic import GenericSource, extract_date
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

# Ordered most complete first. A post-specific sitemap beats a generic index.
_SITEMAP_PATHS = ("/post-sitemap.xml", "/sitemap-posts.xml",
                  "/wp-sitemap-posts-post-1.xml", "/sitemap_index.xml",
                  "/wp-sitemap.xml", "/sitemap.xml", "/sitemap-index.xml")

_GHOST_KEY_RE = re.compile(
    r'(?:key|apiKey)["\']?\s*[:=]\s*["\']([0-9a-f]{26})["\']', re.I)


def detect(url: str) -> dict:
    """Work out how to ingest a site the user just added.

    Routes are tried most-complete first, and each probe is given real retries.
    A feed is the last resort rather than an early exit: it usually carries only
    the newest handful of posts, so silently settling for one would build a
    stunted archive and look like success. When that is all there is, the result
    says so via `partial`.
    """
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    base = f"{parsed.scheme}://{parsed.netloc}"

    recipe = recipe_for(host)
    if recipe is not None:
        return {"plugin": recipe["plugin"], "name": recipe["name"],
                "homepage": base, "config": {}, "partial": False,
                "detected": "built-in recipe", "note": recipe["note"]}

    # Title comes from the URL as given, so a section index names itself after
    # the section rather than the site. The bare hostname is the honest
    # fallback: truncating at the first dot turns nav.al into "Nav", which is
    # simply wrong.
    name = _site_title(url) or _site_title(base) or host.replace("www.", "")

    # A path means the user asked for one section, not the whole site. Crawl
    # that index and follow its pagination; a site-wide API would return
    # everything else too, and on some sites the section is not in the API at
    # all.
    section = parsed.path.rstrip("/")
    if section and section not in ("", "/"):
        return {"plugin": "generic", "name": name, "homepage": base,
                "config": {"strategy": "archive", "index": section + "/",
                           "path_prefix": section},
                "partial": False,
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
    try:
        html = net.fetch_text(base, timeout=25, retries=2)
        m = _GHOST_KEY_RE.search(html)
        if m and ("ghost" in html.lower() or "/ghost/api/" in html):
            return {"plugin": "ghost", "name": name, "homepage": base,
                    "config": {"content_key": m.group(1)}, "partial": False,
                    "detected": "Ghost Content API"}
    except net.FetchError:
        pass

    # 3. A sitemap still enumerates the whole archive.
    for path in _SITEMAP_PATHS:
        try:
            resp = net.fetch(base + path, timeout=20, retries=2)
        except net.FetchError:
            continue
        head = resp.body[:2000]
        if b"<urlset" in head or b"<sitemapindex" in head:
            return {"plugin": "generic", "name": name, "homepage": base,
                    "config": {"strategy": "sitemap", "sitemap": path},
                    "partial": False, "detected": f"sitemap ({path})"}

    # 4. Nothing machine-readable. The generic source will still try the
    #    blog's own archive index (and its pagination) before falling back to
    #    a feed, so leave the strategy open rather than pinning it to "feed".
    return {"plugin": "generic", "name": name, "homepage": base,
            "config": {"strategy": "auto"}, "partial": True,
            "detected": "no API or sitemap — will crawl the blog's archive, "
                        "then fall back to its feed"}


def _site_title(base: str) -> str | None:
    from .. import htmlutil
    try:
        soup = htmlutil.parse(net.fetch_text(base, timeout=20, retries=1))
    except net.FetchError:
        return None
    for getter in (lambda: htmlutil.meta_content(soup, "og:site_name"),
                   lambda: soup.title.get_text(strip=True) if soup.title else None):
        val = getter()
        if val:
            val = re.split(r"\s*[|–—·]\s*", val.strip())[0].strip()
            if 2 <= len(val) <= 48:
                return val
    return None


__all__ = ["REGISTRY", "RECIPES", "recipe_for", "build", "detect", "Source",
           "Stub", "Content", "Context", "Cancelled", "extract_date"]
