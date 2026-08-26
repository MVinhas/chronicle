"""General-purpose blog discovery: gather evidence from every route, then merge.

Older versions picked ONE route per site — sitemap OR archive page OR feed —
and lost what the other routes knew: a sitemap enumerates the whole archive
but carries no dates, while the feed has exact dates and full bodies for the
newest posts. Treating discovery as an evidence problem fixes that:

1. Every cheap enumerator runs: feeds (with pagination), sitemaps (found via
   robots.txt and convention), and the site's own archive/index pages.
2. Everything lands in one candidate pool keyed by canonical URL, so an
   article seen through three routes is one candidate carrying the union of
   what those routes knew about it.
3. Candidates are classified *before* any per-page fetch: obvious non-posts
   (assets, taxonomy listings, date archives, pagination) are dropped free of
   charge, and the rest are ordered so the most promising are probed first.
4. A candidate whose evidence is already complete (a feed item with a date
   and a body) is never fetched at all; one already settled in the library is
   skipped, which is what makes a re-sync cost a handful of requests instead
   of one per article.

The output is deliberately dumb: a list of `Candidate`s plus a `Report` saying
what each route contributed, so the caller can both build the archive and say
how confident it is that the archive is complete.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from .. import dates, htmlutil, net

# Where blogs conventionally list everything they have published.
ARCHIVE_PATHS = ("/archive", "/archives", "/posts", "/all", "/blog",
                 "/writing", "/essays", "/articles", "/notes", "/")
MAX_ARCHIVE_PAGES = 40
MAX_FEED_PAGES = 25
MAX_SITEMAP_CHILDREN = 50

FEED_HINTS = ("/feed", "/feed/", "/rss", "/rss.xml", "/atom.xml", "/index.xml",
              "/feed.xml", "/blog/feed")
SITEMAP_HINTS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
                 "/sitemap-index.xml", "/sitemap-posts.xml")

# Path segments that mean "a listing or utility page, not an article".
_SKIP_SEGMENTS = re.compile(
    r"/(tag|tags|category|categories|author|authors|page|search|feed|about|"
    r"contact|privacy|terms|archive|archives|index|sitemap|login|subscribe|"
    r"wp-content|wp-admin|wp-includes|cdn-cgi|cart|checkout|shop|products)"
    r"(/|$)", re.I)
_ASSET_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|pdf|zip|gz|mp[34]|mov|avi|xml|json|css|js|"
    r"txt|woff2?|ttf|eot)$", re.I)
# A path that is ONLY a date is a date-archive listing, not a post.
_DATE_ARCHIVE_RE = re.compile(r"^/\d{4}(/\d{1,2})?(/\d{1,2})?/?$")

# Sitemap-index children that cannot contain posts.
_SITEMAP_CHILD_SKIP = re.compile(
    r"(image|video|news|category|categories|tag|author|term|user|product|"
    r"attachment|misc|local)[-_]?sitemap|sitemap[-_]?(image|video|category|"
    r"tag|author|misc|pt-)|[-_](category|tag|author|attachment|product)[-_]?\d*\.xml",
    re.I)


@dataclass
class Candidate:
    """One URL that might be an article, with everything every route knew."""
    url: str
    guid: str
    evidence: set[str] = field(default_factory=set)   # feed | sitemap | archive
    title: str | None = None
    date: dates.PubDate = dates.UNKNOWN
    feed_html: str | None = None
    order: int = 0

    @property
    def complete(self) -> bool:
        """Nothing a page fetch could add that we need for discovery."""
        return bool(self.feed_html and self.date.known and self.title)


@dataclass
class Report:
    """What each route contributed — the basis for a completeness statement."""
    feed_count: int = 0
    feed_url: str | None = None
    sitemap_count: int = 0
    sitemap_url: str | None = None
    archive_count: int = 0
    archive_index: str | None = None
    candidates: int = 0
    skipped_known: int = 0

    def routes(self) -> list[str]:
        out = []
        if self.sitemap_count:
            out.append(f"sitemap {self.sitemap_count}")
        if self.archive_count:
            out.append(f"archive {self.archive_count}")
        if self.feed_count:
            out.append(f"feed {self.feed_count}")
        return out

    def summary(self) -> str:
        """One line an end user can read: what was consulted, how it agreed."""
        routes = self.routes()
        if not routes:
            return "no article listing found"
        blurb = ", ".join(routes)
        if len(routes) >= 2:
            blurb += f" — {self.candidates} unique"
        if self.feed_count and len(routes) == 1:
            blurb += " (feed only — old posts may be missing)"
        return blurb

    @property
    def feed_only(self) -> bool:
        return bool(self.feed_count) and not (self.sitemap_count or self.archive_count)


# --------------------------------------------------------------------------
# URL classification
# --------------------------------------------------------------------------

def classify_url(url: str) -> str:
    """'skip' (cannot be a post), or 'maybe' (worth considering)."""
    p = urlparse(url)
    path = p.path or "/"
    if path == "/" and not p.query:
        return "skip"
    if _ASSET_RE.search(path):
        return "skip"
    if _DATE_ARCHIVE_RE.match(path):
        return "skip"
    if _SKIP_SEGMENTS.search(path):
        # A dated permalink under a skip-listed prefix is still a post:
        # /archive/2020/05/06/slug is content, /archive/2020/05/ is not.
        last = path.rstrip("/").rsplit("/", 1)[-1]
        if dates.parse_from_url(url).known and not last.isdigit():
            return "maybe"
        return "skip"
    return "maybe"


def _looks_like_article(soup, extracted_html: str | None = None) -> bool:
    """Generic article-ness signals for a page with no extractable date."""
    og = htmlutil.meta_content(soup, "og:type")
    if og and og.strip().lower() == "article":
        return True
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _iter_ld(data):
            t = obj.get("@type")
            names = {t} if isinstance(t, str) else set(t or [])
            if names & {"Article", "BlogPosting", "NewsArticle", "TechArticle"}:
                return True
    return False


# --------------------------------------------------------------------------
# enumerators
# --------------------------------------------------------------------------

def find_feed_url(base: str, homepage_html: str | None, config: dict,
                  ctx) -> str | None:
    # An empty stored hint means "looked for one before, there is none" —
    # recorded so every sync does not re-probe eight conventional paths.
    if "feed" in config:
        return config["feed"] or None
    if homepage_html:
        soup = htmlutil.parse(homepage_html)
        link = soup.find("link", attrs={"type": re.compile(
            r"application/(rss|atom)\+xml")})
        if link and link.get("href"):
            return net.absolutise(base + "/", link["href"])
    for hint in FEED_HINTS:
        ctx.check()
        try:
            resp = net.fetch(base + hint, timeout=20, retries=1)
        except net.FetchError:
            continue
        if b"<rss" in resp.body[:2000] or b"<feed" in resp.body[:2000]:
            return base + hint
    return None


@dataclass
class FeedItem:
    link: str
    title: str
    date: dates.PubDate
    html: str | None


def read_feed(feed_url: str, ctx) -> list[FeedItem]:
    """Read a feed and, where the site supports it, its older pages too.

    WordPress serves the archive through ``/feed/?paged=N``; some Atom feeds
    link the next page with ``rel="next"``. Feeds that support neither simply
    return the same items again, which yields nothing new and stops the loop
    after a single extra request.
    """
    items: list[FeedItem] = []
    seen: set[str] = set()
    url: str | None = feed_url
    first_page_size = 0

    for page in range(1, MAX_FEED_PAGES + 1):
        if url is None:
            break
        ctx.check()
        try:
            xml = net.fetch_text(url, timeout=60)
        except net.FetchError:
            break
        page_items, next_url = _parse_feed_page(xml)
        if page == 1:
            first_page_size = len(page_items)
        fresh = 0
        for it in page_items:
            key = net.canonical_url(it.link)
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
            fresh += 1
        if not fresh:
            break
        if next_url:
            url = net.absolutise(url, next_url)
        elif first_page_size >= 10:
            # Worth asking for an older page in the WordPress style.
            url = _with_query_param(feed_url, "paged", str(page + 1))
        else:
            break
    return items


def _with_query_param(url: str, key: str, value: str) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != key] + [(key, value)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


_NEXT_LINK_RE = re.compile(
    r'<(?:atom:)?link[^>]*rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']', re.I)


def _parse_feed_page(xml: str) -> tuple[list[FeedItem], str | None]:
    raw_items = re.findall(r"<(?:item|entry)[ >](.*?)</(?:item|entry)>", xml, re.S)
    if not raw_items:
        raw_items = re.findall(r"<(?:item|entry)>(.*?)</(?:item|entry)>", xml, re.S)
    head = xml.split("<item")[0].split("<entry")[0]
    m = _NEXT_LINK_RE.search(head)
    next_url = m.group(1) if m else None

    out = []
    import html as _h
    for blob in raw_items:
        link = _feed_link(blob)
        if not link:
            continue
        raw_date = (_tag(blob, "pubDate") or _tag(blob, "published")
                    or _tag(blob, "updated") or _tag(blob, "dc:date") or "")
        date = dates.parse_rfc822(raw_date, confidence="exact", source="feed:date")
        if not date.known:
            date = dates.parse_iso(raw_date, confidence="exact", source="feed:date")
        body = _cdata(_tag(blob, "content:encoded") or _tag(blob, "content") or "")
        title = _cdata(_tag(blob, "title") or "")
        title = _h.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        out.append(FeedItem(link=link, title=title or "Untitled", date=date,
                            html=body or None))
    return out, next_url


def read_sitemaps(base: str, config: dict, ctx) -> tuple[list[str], str | None]:
    """URLs from the best available sitemap, and which sitemap that was."""
    def hints():
        # Lazily: when the stored hint still works, nothing else is fetched.
        if config.get("sitemap"):
            yield config["sitemap"]
        yield from robots_sitemaps(base)
        yield from SITEMAP_HINTS

    tried: set[str] = set()
    for hint in hints():
        url = hint if hint.startswith("http") else base + hint
        if url in tried:
            continue
        tried.add(url)
        ctx.check()
        try:
            xml = net.fetch_text(url, timeout=60, max_bytes=40_000_000)
        except net.FetchError:
            continue
        if "<urlset" not in xml[:2000] and "<sitemapindex" not in xml[:2000]:
            continue
        urls = _collect_sitemap(xml, ctx, depth=0)
        if urls:
            return urls, hint
    return [], None


def robots_sitemaps(base: str) -> list[str]:
    try:
        text = net.fetch_text(base + "/robots.txt", timeout=15, retries=1)
    except net.FetchError:
        return []
    out = []
    for line in text.splitlines()[:200]:
        if line.lower().startswith("sitemap:"):
            loc = line.split(":", 1)[1].strip()
            if loc.startswith("http"):
                out.append(loc)
    return out


def _collect_sitemap(xml: str, ctx, depth: int) -> list[str]:
    locs = [m.strip() for m in re.findall(r"<loc>(.*?)</loc>", xml, re.S)]
    if "<sitemapindex" in xml and depth < 2:
        out: list[str] = []
        for child in locs[:MAX_SITEMAP_CHILDREN]:
            ctx.check()
            # Skip only children that clearly cannot hold posts. Anonymous or
            # numbered children (sitemap-1.xml) must be followed: plenty of
            # generators name their content sitemaps that way.
            if _SITEMAP_CHILD_SKIP.search(child):
                continue
            try:
                out += _collect_sitemap(
                    net.fetch_text(child, timeout=60), ctx, depth + 1)
            except net.FetchError:
                continue
        return out
    return [u for u in locs if u.startswith("http") and not _ASSET_RE.search(
        urlparse(u).path or "")]


def read_archive(base: str, config: dict, ctx, in_scope) -> tuple[list[str], str | None]:
    """Article links from the site's own archive/index pages and pagination."""
    if config.get("index") == "":
        return [], None   # looked before: this site keeps no archive index
    paths = ((config.get("index"),) if config.get("index") else ()) + ARCHIVE_PATHS
    for path in paths:
        ctx.check()
        links = _links_from(base + path, base)
        links = [u for u in links if in_scope(u)]
        if len(links) < 3:
            continue
        seen = dict.fromkeys(links)
        # Numbered pagination first (also covers JS "load more" buttons whose
        # numbered pages are still served), then any rel=next chain.
        for page in range(2, MAX_ARCHIVE_PAGES + 1):
            ctx.check()
            more = _links_from(f"{base}{path.rstrip('/')}/page/{page}/", base)
            if not more:
                more = _links_from(
                    _with_query_param(base + path, "paged", str(page)), base)
            fresh = [u for u in more if u not in seen and in_scope(u)]
            if not fresh:
                break
            for u in fresh:
                seen[u] = None
        return list(seen), path
    return [], None


def _links_from(page_url: str, base: str) -> list[str]:
    """Same-site candidate links on a page, in document order."""
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
        if classify_url(full) != "maybe":
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------

def merge(feed_items: list[FeedItem], sitemap_urls: list[str],
          archive_urls: list[str], in_scope) -> dict[str, Candidate]:
    """One candidate per canonical URL, holding the union of the evidence."""
    pool: dict[str, Candidate] = {}
    order = 0

    def get(url: str) -> Candidate | None:
        nonlocal order
        if not in_scope(url):
            return None
        guid = net.canonical_url(url)
        cand = pool.get(guid)
        if cand is None:
            cand = Candidate(url=url, guid=guid, order=order)
            order += 1
            pool[guid] = cand
        return cand

    # Archive order first: it is the site's own ordering, which several blogs
    # without dates rely on for tie-breaking.
    for url in archive_urls:
        cand = get(url)
        if cand is not None:
            cand.evidence.add("archive")
    for url in sitemap_urls:
        if classify_url(url) != "maybe":
            continue
        cand = get(url)
        if cand is not None:
            cand.evidence.add("sitemap")
    for item in feed_items:
        cand = get(item.link)
        if cand is None:
            continue
        cand.evidence.add("feed")
        if item.title and not cand.title:
            cand.title = item.title
        if item.date.known and _rank(item.date) > _rank(cand.date):
            cand.date = item.date
        if item.html and not cand.feed_html:
            cand.feed_html = item.html
    return pool


_CONF_RANK = {"unknown": 0, "inferred": 1, "medium": 2, "high": 3, "exact": 4}


def _rank(d: dates.PubDate) -> int:
    return _CONF_RANK.get(d.confidence, 0) if d.known else -1


def probe_order(pool: dict[str, Candidate]) -> list[Candidate]:
    """Most promising first: corroborated by several routes, then site order."""
    return sorted(pool.values(),
                  key=lambda c: (-len(c.evidence), c.order))


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

    # <time datetime> is a strong signal whatever the theme happened to name
    # its class. A hinted one wins outright; otherwise a page whose time
    # elements all agree is unambiguous. Times in navigation/sidebars and
    # ones explicitly marked as modification dates are ignored.
    time_candidates: list[dates.PubDate] = []
    for t in soup.find_all("time"):
        dt = t.get("datetime") or t.get("content")
        if not dt:
            continue
        parsed = dates.parse_iso(dt, confidence="high", source="time:datetime")
        if not parsed.known:
            continue
        blob = " ".join((t.get("class") or [])) + " " + (t.get("id") or "")
        if _TIME_HINT.search(blob):
            return parsed
        if re.search(r"updated|modified", blob, re.I):
            continue
        if t.find_parent(("nav", "aside", "footer")):
            continue
        time_candidates.append(parsed)
    if len({c.iso for c in time_candidates}) == 1:
        return time_candidates[0]

    d = dates.parse_from_url(url)
    if d.known:
        return d

    # Several distinct <time> values with no hint: the one nearest the top of
    # the document is conventionally the dateline. Weaker, so 'medium'.
    if time_candidates:
        first = time_candidates[0]
        return dates.PubDate(first.iso, first.precision, "medium",
                             "time:datetime")

    for node in soup.find_all(class_=_TIME_HINT, limit=6):
        d = dates.parse_freeform(node.get_text(" ", strip=True),
                                 confidence="medium", source="text:dateline")
        if d.known:
            return d

    # Some hand-written pages carry a dateline as plain text near the top --
    # "March 13, 2019" as its own heading, with no class or id to hint at it
    # (paulgraham.com's essays and incompleteideas.net's are both like this).
    # Restricted to the first few headings, and required to actually name a
    # month, so a section heading that merely contains a bare year ("Lawyers
    # in 1991") is not mistaken for a dateline -- parse_freeform's bare-year
    # fallback is far too weak a signal here, unlike in a context that is
    # already known to be a dateline (e.g. a class-hinted node, above).
    for node in soup.find_all(("h1", "h2", "h3", "h4"), limit=6):
        text = node.get_text(" ", strip=True)
        if len(text) > 40 or not re.search(dates.MONTH_RE, text, re.I):
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


# --------------------------------------------------------------------------
# small feed-parsing helpers
# --------------------------------------------------------------------------

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
