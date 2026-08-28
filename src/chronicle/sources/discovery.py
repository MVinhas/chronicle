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
# How far into an archive index "Fetch new posts" is willing to page.
# Listings are newest-first, so anything published since the last update
# is on the first page or two; paging deeper is the full scan's job.
NEWEST_ARCHIVE_PAGES = 3
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


def read_archive(base: str, config: dict, ctx, in_scope,
                 is_known=lambda url: False, max_pages: int | None = None,
                 admit=None,
                 ) -> tuple[list[tuple[str, dates.PubDate]], str | None]:
    """Article links from the site's own archive/index pages and pagination.

    Each link comes back with whatever date the listing printed next to it —
    for many hand-built blogs the index is the *only* place dates exist at
    all. Pagination is incremental: once a whole page's links are already in
    the library, the older pages cannot contain anything new, so the crawl
    stops there instead of re-walking the entire history every sync.

    `max_pages` caps that pagination regardless of what it finds, which is how
    "Fetch new posts" keeps a routine update to a handful of requests even on
    a site whose listing carries no dates for the incremental stop to use.

    `admit` is called with the links a *configured* section index published
    before they are scope-filtered. It lets a section whose articles live
    off its own path (a category or tag listing, whose posts sit at the site
    root) widen the scope to exactly what it listed -- without which every
    such source discovers nothing at all.
    """
    if config.get("index") == "":
        return [], None   # looked before: this site keeps no archive index
    configured = config.get("index") or None
    paths = ((configured,) if configured else ()) + ARCHIVE_PATHS
    for path in paths:
        ctx.check()
        links = _links_from(base + path, base)
        # The section the user actually asked for gets to say what belongs to
        # it. Done before scope filtering, because on a category or tag index
        # the filter would otherwise reject every link on the page.
        if admit is not None and path == configured:
            admit([u for u, _ in links])
        links = [(u, h) for u, h in links if in_scope(u)]
        if len(links) < 3:
            continue
        seen: dict[str, str] = {}
        for u, h in links:
            seen.setdefault(u, h)
        # Numbered pagination first (also covers JS "load more" buttons whose
        # numbered pages are still served), then the ?paged= fallback.
        limit = MAX_ARCHIVE_PAGES if max_pages is None else min(
            MAX_ARCHIVE_PAGES, max(1, max_pages))
        for page in range(2, limit + 1):
            ctx.check()
            more = _links_from(f"{base}{path.rstrip('/')}/page/{page}/", base)
            if not more:
                more = _links_from(
                    _with_query_param(base + path, "paged", str(page)), base)
            if admit is not None and path == configured:
                admit([u for u, _ in more])
            more = [(u, h) for u, h in more if in_scope(u)]
            fresh = [(u, h) for u, h in more if u not in seen]
            if not fresh:
                break
            for u, h in fresh:
                seen.setdefault(u, h)
            if all(is_known(u) for u, _ in more):
                break   # everything older is already archived
        return resolve_listing_dates(list(seen.items())), path
    return [], None


def _links_from(page_url: str, base: str) -> list[tuple[str, str]]:
    """Same-site candidate links on a page, in document order, each with the
    listing text printed alongside it (a date, on many archive pages)."""
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
        out.append((full, _link_hint(a)))
    return out


_HINT_CONTAINERS = ("li", "dd", "td", "p", "article",
                    "h2", "h3", "h4", "h5", "h6")


def _link_hint(a) -> str:
    """The text a listing prints beside a link — its dateline, very often.

    danluu.com writes `<li><d>08/26</d><a …>`, WordPress archives put a
    <time> in each card; both reduce to "the text of the link's own list
    item, minus the link itself". A container shared by several links
    describes none of them, so it yields nothing.
    """
    container = a.find_parent(_HINT_CONTAINERS)
    if container is None:
        return ""
    anchors = container.find_all("a")
    if len(anchors) != 1:
        return ""
    t = container.find("time")
    if t is not None and t.get("datetime"):
        return str(t["datetime"])
    text = container.get_text(" ", strip=True)
    link_text = a.get_text(" ", strip=True)
    if link_text:
        text = text.replace(link_text, " ", 1)
    return " ".join(text.split())[:120]


_NUM_MY_RE = re.compile(r"(\d{1,2})\s*[/.\-]\s*(\d{2}|\d{4})$")
_NUM_YM_RE = re.compile(r"(\d{4})\s*[/.\-]\s*(\d{1,2})$")
_BARE_YEAR_RE = re.compile(r"\(?\s*((?:19|20)\d{2})\s*\)?$")


def resolve_listing_dates(entries: list[tuple[str, str]],
                          ) -> list[tuple[str, dates.PubDate]]:
    """Turn per-link listing text into dates, conservatively.

    Explicit forms (ISO, named months) are taken per entry. Bare numeric
    forms like danluu.com's "08/26" are ambiguous alone, so they are read as
    an *ensemble*: an interpretation (month/2-digit-year, month/full-year,
    year/month) is accepted only when it makes every entry a valid date in a
    plausible range AND the whole listing comes out in chronological order —
    which is what an archive index is. Anything unresolved stays unknown.
    """
    out: dict[str, dates.PubDate] = {}
    numeric: list[tuple[str, int, int]] = []   # (url, first, second)
    for url, hint in entries:
        hint = (hint or "").strip()
        if not hint:
            continue
        # parse_iso cascades to parse_freeform, so this covers ISO datetimes
        # and named-month datelines alike. The bare-year fallback is too
        # eager for arbitrary listing text ("4 min read · issue 2021"), so a
        # year-only reading is trusted only when the hint IS just a year.
        d = dates.parse_iso(hint, confidence="medium", source="archive:index")
        if d.known and d.precision == "year" and not _BARE_YEAR_RE.fullmatch(hint):
            d = dates.UNKNOWN
        if d.known:
            out[url] = d
            continue
        m = _NUM_MY_RE.fullmatch(hint) or _NUM_YM_RE.fullmatch(hint)
        if m:
            numeric.append((url, int(m.group(1)), int(m.group(2))))

    for url, d in _resolve_numeric_ensemble(numeric).items():
        out.setdefault(url, d)
    return [(url, out.get(url, dates.UNKNOWN)) for url, _ in entries]


def _resolve_numeric_ensemble(numeric: list[tuple[str, int, int]],
                              ) -> dict[str, dates.PubDate]:
    if len(numeric) < 3:
        return {}
    from datetime import datetime, timezone
    this_year = datetime.now(timezone.utc).year

    def expand(two: int) -> int:
        century = 2000 if two <= (this_year % 100) + 1 else 1900
        return century + two

    def interpret(a: int, b: int, form: str) -> tuple[int, int] | None:
        if form == "my":
            month, year = a, (expand(b) if b < 100 else b)
        else:
            year, month = a, b
        if not (1 <= month <= 12 and 1990 <= year <= this_year + 1):
            return None
        return year, month

    for form in ("ym", "my"):
        pairs = [interpret(a, b, form) for _, a, b in numeric]
        if any(p is None for p in pairs):
            continue
        forward = all(x <= y for x, y in zip(pairs, pairs[1:]))
        backward = all(x >= y for x, y in zip(pairs, pairs[1:]))
        if not (forward or backward):
            continue
        return {
            url: dates.PubDate(f"{y:04d}-{m:02d}-01T00:00:00", "month",
                               "medium", "archive:index")
            for (url, _, _), (y, m) in zip(numeric, pairs)
        }
    return {}


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------

def merge(feed_items: list[FeedItem], sitemap_urls: list[str],
          archive_entries: list[tuple[str, dates.PubDate]],
          in_scope) -> dict[str, Candidate]:
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
    # without dates rely on for tie-breaking — and its listings often carry
    # the only dates the site publishes at all.
    for url, listed_date in archive_entries:
        cand = get(url)
        if cand is not None:
            cand.evidence.add("archive")
            if listed_date.known and _rank(listed_date) > _rank(cand.date):
                cand.date = listed_date
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
