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
from . import wayback

_FEED_HINTS = ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml",
               "/feed.xml", "/blog/feed")
_SITEMAP_HINTS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
                  "/sitemap-index.xml", "/sitemap-posts.xml")

# Where blogs conventionally list everything they have published.
_ARCHIVE_PATHS = ("/archive", "/archives", "/posts", "/all", "/blog",
                  "/writing", "/essays", "/articles", "/notes", "/")
_MAX_ARCHIVE_PAGES = 40

_SKIP_PATH = re.compile(
    r"/(tag|tags|category|categories|author|authors|page|search|feed|about|"
    r"contact|privacy|terms|archive|archives|index|sitemap|login|subscribe)"
    r"(/|$)", re.I)


class GenericSource(Source):
    plugin_id = "generic"
    display_name = "Website"

    def fetch_content(self, ctx: Context, url: str, stub_html: str | None = None,
                      base_url: str | None = None, extra: dict | None = None) -> Content:
        snapshot = (extra or {}).get("snapshot")
        if snapshot:
            try:
                resp = wayback.fetch_snapshot(url, snapshot)
            except net.FetchError as exc:
                return Content("", status="error", source=f"wayback:{exc.status}")
            if resp is None:
                return Content("", status="error", source="wayback:none")
            html = resp.text()
            soup = htmlutil.parse(html)
            wayback.strip_banner(soup)
            return self.clean(soup.decode(), url, source="wayback")
        return super().fetch_content(ctx, url, stub_html, base_url, extra)

    def discover(self, ctx: Context):
        strategy = self.config.get("strategy") or "auto"
        if strategy == "wayback":
            yield from self._try_wayback(ctx)
            return
        found = False
        if strategy in ("auto", "sitemap") and not self.path_prefix:
            got = yield from self._try_sitemap(ctx)
            found = found or got
            if got:
                return
        if strategy in ("auto", "sitemap", "archive"):
            got = yield from self._try_archive_pages(ctx)
            found = found or got
            if got:
                return
        if not self.path_prefix:
            got = yield from self._from_feed(ctx)
            found = found or got
            if got:
                return

        # Every direct route came up empty -- the page has no metadata Chronicle
        # can read a date from (a bare Apache index is a common case), the site
        # blocks scripted access, or the path really has nothing in it. Rather
        # than reporting an empty archive, see whether the Internet Archive has
        # crawled dated copies of the same URLs.
        if not found:
            ctx.say(f"{self.name}: nothing usable found directly; "
                    f"trying the Internet Archive…")
            yield from self._try_wayback(ctx)

    # -- archive pages -----------------------------------------------------

    def _index_paths(self) -> tuple[str, ...]:
        """Where to look for a list of everything, most specific first."""
        configured = self.config.get("index")
        return (configured,) + _ARCHIVE_PATHS if configured else _ARCHIVE_PATHS

    def _try_archive_pages(self, ctx: Context):
        """Crawl the blog's own index of everything it has published.

        Plenty of blogs have no sitemap but do keep an archive page. Following
        its pagination recovers the whole history where a feed would have given
        only the newest handful of posts -- and it also covers archive pages
        whose "load more" button is JavaScript, because the numbered pages it
        would fetch are usually still served as ordinary URLs.
        """
        base = (self.homepage or "").rstrip("/")
        links: dict[str, int] = {}

        for path in self._index_paths():
            ctx.check()
            found = self._links_from(base + path, base, ctx)
            found = [u for u in found if self.in_scope(u)]
            if len(found) < 3:
                continue
            ctx.say(f"{self.name}: reading the archive at {path}")
            for url in found:
                links.setdefault(url, len(links))

            # Follow numbered pagination for as long as it yields new posts.
            for page in range(2, _MAX_ARCHIVE_PAGES + 1):
                ctx.check()
                more = self._links_from(f"{base}{path.rstrip('/')}/page/{page}/",
                                        base, ctx)
                fresh = [u for u in more if u not in links and self.in_scope(u)]
                if not fresh:
                    break
                for url in fresh:
                    links.setdefault(url, len(links))
                ctx.say(f"{self.name}: archive page {page} — {len(links)} posts")
            break

        if len(links) < 5:
            return False

        ctx.say(f"{self.name}: {len(links)} posts found in the archive index")
        count = 0
        ordered = sorted(links, key=links.get)
        for i, url in enumerate(ordered):
            ctx.check()
            if i % 10 == 0:
                ctx.say(f"{self.name}: reading {i}/{len(ordered)}",
                        i / max(1, len(ordered)))
            stub = self._probe(url, i)
            if stub is not None:
                count += 1
                yield stub
        return count > 0

    def _links_from(self, page_url: str, base: str, ctx: Context) -> list[str]:
        """Same-site article links on a page, in document order."""
        try:
            html = net.fetch_text(page_url, timeout=30, retries=1)
        except net.FetchError:
            return []
        soup = htmlutil.parse(html)
        host = urlparse(base).netloc.replace("www.", "")
        out, seen = [], set()
        for a in soup.find_all("a", href=True):
            full = net.absolutise(page_url, a["href"]).split("#")[0]
            p = urlparse(full)
            if p.netloc.replace("www.", "") != host:
                continue
            if _SKIP_PATH.search(p.path or "") or (p.path or "/") == "/":
                continue
            if re.search(r"\.(jpg|png|gif|pdf|zip|xml|json|css|js)$", p.path, re.I):
                continue
            if full in seen:
                continue
            seen.add(full)
            out.append(full)
        return out

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

        urls = [u for u in urls if not _SKIP_PATH.search(urlparse(u).path or "")
                and self.in_scope(u)]
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

    # -- wayback -------------------------------------------------------------

    def _try_wayback(self, ctx: Context):
        """Rebuild the archive entirely from the Internet Archive.

        Used when the site itself does not answer at all -- see `is_dead()` in
        `detect()`. Every URL the CDX index has ever crawled is a candidate;
        each is probed through its own archived snapshot, since the origin
        cannot be asked directly for anything, metadata included.
        """
        host = urlparse(self.homepage or "").netloc
        ctx.say(f"{self.name}: searching the Internet Archive — this takes "
                f"a couple of minutes…")
        snapshots = wayback.list_snapshots(host)
        snapshots = [(u, ts) for u, ts in snapshots if self.in_scope(u)]
        ctx.say(f"{self.name}: {len(snapshots)} pages found in the archive")

        count = 0
        for i, (url, timestamp) in enumerate(snapshots):
            ctx.check()
            if i % 10 == 0:
                ctx.say(f"{self.name}: reading {i}/{len(snapshots)}",
                        i / max(1, len(snapshots)))
            stub = self._probe_wayback(url, timestamp, i)
            if stub is not None:
                count += 1
                yield stub
        return count > 0

    def _probe_wayback(self, url: str, timestamp: str, order: int) -> Stub | None:
        try:
            resp = wayback.fetch_snapshot(url, timestamp)
        except net.FetchError:
            return None
        if resp is None:
            return None
        html = resp.text()
        soup = htmlutil.parse(html)
        date = extract_date(soup, url)
        if not date.known:
            return None
        return Stub(guid=net.canonical_url(url), url=url,
                    title=htmlutil.clean_title(htmlutil.page_title(soup), self.name),
                    date=date, source_order=order,
                    extra={"snapshot": timestamp})

    # -- feed --------------------------------------------------------------

    def _from_feed(self, ctx: Context):
        feed_url = self.config.get("feed") or self._find_feed(ctx)
        if not feed_url:
            ctx.say(f"{self.name}: no feed or sitemap found")
            return False
        ctx.say(f"{self.name}: reading feed…")
        try:
            xml = net.fetch_text(feed_url, timeout=60)
        except net.FetchError:
            return False
        items = re.findall(r"<(?:item|entry)[ >](.*?)</(?:item|entry)>", xml, re.S)
        if not items:
            items = re.findall(r"<(?:item|entry)>(.*?)</(?:item|entry)>", xml, re.S)
        ctx.say(f"{self.name}: {len(items)} items in feed")
        count = 0
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
            count += 1
            yield Stub(guid=net.canonical_url(link), url=link,
                       title=_h.unescape(re.sub(r"<[^>]+>", "", title)).strip() or "Untitled",
                       date=date, source_order=order,
                       raw_html=body or None, base_url=link,
                       content_source="feed" if body else "")
        return count > 0

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

    # Some hand-written pages carry a dateline as plain text near the top --
    # "March 13, 2019" as its own heading, with no class or id to hint at it
    # (paulgraham.com's essays and incompleteideas.net's are both like this).
    # Restricted to the first few headings so a date mentioned in the body
    # later on is not mistaken for the publication date.
    for node in soup.find_all(("h1", "h2", "h3", "h4"), limit=6):
        text = node.get_text(" ", strip=True)
        if len(text) > 40:          # a real heading, not a dateline
            continue
        d = dates.parse_freeform(text, confidence="medium", source="text:heading")
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
