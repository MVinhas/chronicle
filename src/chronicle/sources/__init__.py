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


def detect(url: str) -> dict:
    """Work out how to ingest a site the user just added.

    Returns {plugin, name, homepage, config, note}. Probing is best-effort:
    a site we cannot classify still works through the generic source.
    """
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    base = f"{parsed.scheme}://{parsed.netloc}"
    name = host.replace("www.", "").split(".")[0].replace("-", " ").title()

    recipe = recipe_for(host)
    if recipe is not None:
        return {"plugin": recipe["plugin"], "name": recipe["name"],
                "homepage": base, "config": {},
                "detected": "built-in recipe", "note": recipe["note"]}

    title = _site_title(base)
    if title:
        name = title

    # WordPress REST API — the best generic route when present.
    try:
        resp = net.fetch(f"{base}/wp-json/wp/v2/posts?per_page=1",
                         headers={"Accept": "application/json"}, timeout=20, retries=1)
        if resp.status == 200 and resp.body.strip().startswith(b"["):
            total = resp.headers.get("x-wp-total")
            return {"plugin": "wordpress", "name": name, "homepage": base,
                    "config": {"api_root": f"{base}/wp-json/wp/v2"},
                    "detected": f"WordPress REST API ({total or '?'} posts)"}
    except net.FetchError:
        pass

    # Ghost — the front-end embeds its own public content key.
    try:
        html = net.fetch_text(base, timeout=20, retries=1)
        m = re.search(r'(?:key|apiKey)["\']?\s*[:=]\s*["\']([0-9a-f]{26})["\']', html)
        if m and ("ghost" in html.lower() or "/ghost/api/" in html):
            return {"plugin": "ghost", "name": name, "homepage": base,
                    "config": {"content_key": m.group(1)},
                    "detected": "Ghost Content API"}
    except net.FetchError:
        pass

    for hint in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
                 "/sitemap-posts.xml"):
        try:
            resp = net.fetch(base + hint, timeout=15, retries=1)
            if b"<urlset" in resp.body[:2000] or b"<sitemapindex" in resp.body[:2000]:
                return {"plugin": "generic", "name": name, "homepage": base,
                        "config": {"strategy": "sitemap", "sitemap": hint},
                        "detected": "sitemap"}
        except net.FetchError:
            continue

    return {"plugin": "generic", "name": name, "homepage": base,
            "config": {"strategy": "feed"}, "detected": "RSS/Atom feed (may be partial)"}


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
