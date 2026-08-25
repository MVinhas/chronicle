"""Blog-agnostic source for sites added by the user.

Tries the routes that yield a *complete* archive first (REST API, Ghost API,
sitemap) and only falls back to the feed — which is usually truncated — last.
Dates come from whichever signal a page actually provides, each carrying its
own confidence so unreliable ones stay visibly unreliable.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from .. import dates, htmlutil, net
from .base import Content, Context, Source, Stub

_FEED_HINTS = ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml",
               "/feed.xml", "/blog/feed")
_SITEMAP_HINTS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
                  "/sitemap-index.xml", "/sitemap-posts.xml")

_SKIP_PATH = re.compile(
    r"/(tag|tags|category|categories|author|authors|page|search|feed|about|"
    r"contact|privacy|terms|archive|archives|index|sitemap|login|subscribe)"
    r"(/|$)", re.I)


class GenericSource(Source):
    plugin_id = "generic"
    display_name = "Website"

    def discover(self, ctx: Context):
        strategy = self.config.get("strategy") or "auto"
        if strategy in ("auto", "sitemap"):
            got = yield from self._try_sitemap(ctx)
            if got:
                return
        yield from self._from_feed(ctx)

    # -- sitemap -----------------------------------------------------------

    def _try_sitemap(self, ctx: Context):
        base = (self.homepage or "").rstrip("/")
        urls: list[str] = []
        for hint in ([self.config["sitemap"]] if self.config.get("sitemap")
                     else _SITEMAP_HINTS):
            try:
                xml = net.fetch_text(base + hint if hint.startswith("/") else hint,
                                     timeout=60, max_bytes=40_000_000)
            except net.FetchError:
                continue
            urls = self._collect_sitemap(xml, base, ctx, depth=0)
            if urls:
                break
        if not urls:
            return False

        urls = [u for u in urls if not _SKIP_PATH.search(urlparse(u).path or "")]
        ctx.say(f"{self.name}: {len(urls)} pages in sitemap")
        count = 0
        for i, url in enumerate(urls):
            ctx.check()
            if i % 10 == 0:
                ctx.say(f"{self.name}: reading {i}/{len(urls)}", i / max(1, len(urls)))
            stub = self._probe(url, i)
            if stub is not None:
                count += 1
                yield stub
        return count > 0

    def _collect_sitemap(self, xml: str, base: str, ctx: Context, depth: int) -> list[str]:
        """Follow one level of sitemap index nesting."""
        locs = [m.strip() for m in re.findall(r"<loc>(.*?)</loc>", xml, re.S)]
        if "<sitemapindex" in xml and depth < 2:
            out: list[str] = []
            for child in locs[:25]:
                ctx.check()
                if not re.search(r"post|article|page|blog", child, re.I) and len(locs) > 3:
                    continue
                try:
                    out += self._collect_sitemap(
                        net.fetch_text(child, timeout=60), base, ctx, depth + 1)
                except net.FetchError:
                    continue
            return out
        return [u for u in locs if u.startswith("http")
                and not re.search(r"\.(xml|jpg|png|gif|pdf|webp|svg)$", u, re.I)]

    def _probe(self, url: str, order: int) -> Stub | None:
        try:
            resp = net.fetch(url)
        except net.FetchError:
            return None
        html = resp.text()
        soup = htmlutil.parse(html)
        date = extract_date(soup, url)
        if not date.known:
            return None
        return Stub(guid=net.canonical_url(url), url=url,
                    title=htmlutil.clean_title(htmlutil.page_title(soup), self.name),
                    date=date, source_order=order, raw_html=html,
                    base_url=resp.url, content_source="direct")

    # -- feed --------------------------------------------------------------

    def _from_feed(self, ctx: Context):
        feed_url = self.config.get("feed") or self._find_feed(ctx)
        if not feed_url:
            ctx.say(f"{self.name}: no feed or sitemap found")
            return
        ctx.say(f"{self.name}: reading feed…")
        try:
            xml = net.fetch_text(feed_url, timeout=60)
        except net.FetchError:
            return
        items = re.findall(r"<(?:item|entry)[ >](.*?)</(?:item|entry)>", xml, re.S)
        if not items:
            items = re.findall(r"<(?:item|entry)>(.*?)</(?:item|entry)>", xml, re.S)
        ctx.say(f"{self.name}: {len(items)} items in feed")
        for order, item in enumerate(items):
            ctx.check()
            link = _feed_link(item)
            if not link:
                continue
            raw_date = (_tag(item, "pubDate") or _tag(item, "published")
                        or _tag(item, "updated") or _tag(item, "dc:date") or "")
            date = dates.parse_rfc822(raw_date, confidence="exact", source="feed:date")
            if not date.known:
                date = dates.parse_iso(raw_date, confidence="exact", source="feed:date")
            if not date.known:
                date = dates.parse_from_url(link)
            body = _cdata(_tag(item, "content:encoded") or _tag(item, "content") or "")
            title = _cdata(_tag(item, "title") or "Untitled")
            import html as _h
            yield Stub(guid=net.canonical_url(link), url=link,
                       title=_h.unescape(re.sub(r"<[^>]+>", "", title)).strip() or "Untitled",
                       date=date, source_order=order,
                       raw_html=body or None, base_url=link,
                       content_source="feed" if body else "")

    def _find_feed(self, ctx: Context) -> str | None:
        base = (self.homepage or "").rstrip("/")
        try:
            html = net.fetch_text(base or self.homepage)
            soup = htmlutil.parse(html)
            link = soup.find("link", attrs={"type": re.compile(
                r"application/(rss|atom)\+xml")})
            if link and link.get("href"):
                return net.absolutise(self.homepage, link["href"])
        except net.FetchError:
            pass
        for hint in _FEED_HINTS:
            candidate = base + hint
            try:
                resp = net.fetch(candidate, timeout=20, retries=1)
                if b"<rss" in resp.body[:2000] or b"<feed" in resp.body[:2000]:
                    return candidate
            except net.FetchError:
                continue
        return None


# --------------------------------------------------------------------------
# shared date extraction for arbitrary pages
# --------------------------------------------------------------------------

_JSONLD_KEYS = ("datePublished", "dateCreated", "uploadDate")
_TIME_HINT = re.compile(r"published|pubdate|entry-date|post-date|dateline|byline", re.I)


def extract_date(soup, url: str) -> dates.PubDate:
    """Best available publication date for a page, most trustworthy first."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _iter_ld(data):
            for key in _JSONLD_KEYS:
                if isinstance(obj, dict) and obj.get(key):
                    d = dates.parse_iso(str(obj[key]), confidence="exact",
                                        source=f"jsonld:{key}")
                    if d.known:
                        return d

    meta = htmlutil.meta_content(
        soup, "article:published_time", "og:article:published_time",
        "datePublished", "dc.date.issued", "dcterms.issued",
        "citation_publication_date", "sailthru.date", "parsely-pub-date",
        "pubdate", "publishdate", "date")
    if meta:
        d = dates.parse_iso(meta, confidence="exact", source="meta:published")
        if d.known:
            return d

    for t in soup.find_all("time"):
        blob = " ".join((t.get("class") or [])) + " " + (t.get("id") or "")
        dt = t.get("datetime") or t.get("content")
        if dt and (_TIME_HINT.search(blob) or not blob.strip()):
            d = dates.parse_iso(dt, confidence="high", source="time:datetime")
            if d.known:
                return d

    d = dates.parse_from_url(url)
    if d.known:
        return d

    for node in soup.find_all(class_=_TIME_HINT, limit=6):
        d = dates.parse_freeform(node.get_text(" ", strip=True),
                                 confidence="medium", source="text:dateline")
        if d.known:
            return d
    return dates.UNKNOWN


def _iter_ld(data):
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld(item)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data:
                yield from _iter_ld(data[key])


def _tag(blob: str, name: str) -> str | None:
    m = re.search(rf"<{re.escape(name)}[^>]*>(.*?)</{re.escape(name)}>", blob, re.S)
    return m.group(1).strip() if m else None


def _cdata(text: str) -> str:
    m = re.match(r"\s*<!\[CDATA\[(.*?)\]\]>\s*$", text or "", re.S)
    return m.group(1) if m else (text or "")


def _feed_link(item: str) -> str | None:
    m = re.search(r'<link[^>]*rel="alternate"[^>]*href="([^"]+)"', item)
    if m:
        return m.group(1)
    m = re.search(r"<link[^>]*>(.*?)</link>", item, re.S)
    if m and m.group(1).strip().startswith("http"):
        return m.group(1).strip()
    m = re.search(r'<link[^>]*href="([^"]+)"', item)
    if m:
        return m.group(1)
    guid = _tag(item, "guid")
    return guid if guid and guid.startswith("http") else None
