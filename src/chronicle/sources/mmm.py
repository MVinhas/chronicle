"""mrmoneymustache.com — WordPress behind a Cloudflare bot challenge.

Nothing on the origin answers a plain HTTP client: the REST API, the sitemap
and even /feed/ all return 403. The author's FeedBurner mirror is reachable but
carries only the ~16 most recent posts, so it cannot build the archive.

So discovery runs off the Internet Archive, which has crawled the site since
April 2011. The permalink structure (/YYYY/MM/DD/slug/) means every discovered
URL carries its own publication date — no metadata guessing required.

Content is fetched from the best source available, in order:
  1. FeedBurner   — full-fidelity bodies for recent posts
  2. direct       — works from a residential IP once Cloudflare is satisfied
  3. browser      — the app's own WebKit view, which can solve the challenge
  4. Wayback      — always available, the historical backstop
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

from .. import dates, htmlutil, net
from .base import Content, Context, Source, Stub, assess

FEEDBURNER = "https://feeds.feedburner.com/mrmoneymustache"
CDX = ("http://web.archive.org/cdx/search/cdx?url={host}%2F{year}%2F*"
       "&fl=original&collapse=urlkey&filter=statuscode:200")
WAYBACK = "https://web.archive.org/web/{ts}id_/{url}"

_PERMALINK_RE = re.compile(r"^/(\d{4})/(\d{2})/(\d{2})/([^/]+)/?$")
_FIRST_YEAR = 2011

# Paths that look like posts but are not.
_NOT_A_POST = re.compile(
    r"/(feed|trackback|embed|amp|print|comment-page-\d+|page/\d+)/?$", re.I)


class MrMoneyMustacheSource(Source):
    plugin_id = "mrmoneymustache"
    display_name = "Mr. Money Mustache"
    content_selectors = [".entry-content", ".post-content", ".entry", ".post_content",
                         "article .content", "article", "main"]
    image_blocklist = Source.image_blocklist + (
        "feedburner", "feeds.wordpress.com", "/wp-content/plugins/",
        "web.archive.org/static/",
    )

    @property
    def host(self) -> str:
        return urlparse(self.homepage or "https://www.mrmoneymustache.com/").netloc \
            or "www.mrmoneymustache.com"

    # -- discovery ---------------------------------------------------------

    def discover(self, ctx: Context):
        seen: dict[str, Stub] = {}

        # 1. Recent posts, with full bodies, straight from the feed.
        ctx.say("Mr. Money Mustache: reading FeedBurner…")
        for stub in self._from_feed(ctx):
            seen[stub.guid] = stub
        ctx.say(f"Mr. Money Mustache: {len(seen)} recent posts from the feed")

        # 2. The historical archive, from the Internet Archive.
        this_year = datetime.now().year
        years = list(range(_FIRST_YEAR, this_year + 1))
        for i, year in enumerate(years):
            ctx.check()
            ctx.say(f"Mr. Money Mustache: searching archive for {year}…",
                    i / len(years))
            for url, d in self._cdx_year(year):
                guid = net.canonical_url(url)
                if guid in seen:
                    continue
                slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
                seen[guid] = Stub(
                    guid=guid, url=url,
                    title=slug.replace("-", " ").strip().capitalize(),
                    date=d, author="Mr. Money Mustache",
                    content_source="", source_order=0)
        ctx.say(f"Mr. Money Mustache: {len(seen)} posts discovered")

        ordered = sorted(seen.values(), key=lambda s: (s.date.iso or "9999", s.url))
        for i, stub in enumerate(ordered):
            stub.source_order = i
            yield stub

    def _from_feed(self, ctx: Context) -> list[Stub]:
        out: list[Stub] = []
        try:
            xml = net.fetch_text(FEEDBURNER, timeout=60)
        except net.FetchError:
            return out
        for order, item in enumerate(re.findall(r"<item>(.*?)</item>", xml, re.S)):
            link = _tag(item, "link") or _tag(item, "feedburner:origLink")
            if not link:
                continue
            link = _unescape(link)
            date = dates.parse_rfc822(_tag(item, "pubDate") or "",
                                      confidence="exact", source="feed:pubDate")
            if not date.known:
                date = dates.parse_from_url(link)
            body = _cdata(_tag(item, "content:encoded") or "")
            out.append(Stub(
                guid=net.canonical_url(link), url=link,
                title=_unescape(_cdata(_tag(item, "title") or "Untitled")).strip(),
                date=date, author="Mr. Money Mustache", source_order=order,
                raw_html=body or None, base_url=link, content_source="feed"))
        return out

    def _cdx_year(self, year: int):
        url = CDX.format(host=self.host.replace("www.", ""), year=year)
        try:
            text = net.fetch_text(url, timeout=150, retries=2, max_bytes=60_000_000)
        except net.FetchError:
            return
        seen_slugs = set()
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                path = urlparse(raw).path
            except ValueError:
                continue
            if _NOT_A_POST.search(path):
                continue
            m = _PERMALINK_RE.match(path)
            if not m:
                continue
            y, mo, d, slug = m.groups()
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            date = dates._mk(int(y), int(mo), int(d), precision=dates.PRECISION_DAY,
                             confidence="high", source="url:permalink")
            if not date.known:
                continue
            yield f"https://{self.host}/{y}/{mo}/{d}/{slug}/", date

    # -- content -----------------------------------------------------------

    def fetch_content(self, ctx: Context, url: str, stub_html=None, base_url=None) -> Content:
        if stub_html:
            html = htmlutil.sanitise(htmlutil.parse(f"<div>{stub_html}</div>").div,
                                     base_url or url)
            html = self.drop_decorative_images(html)
            html = self.postprocess(html)
            if assess(html) == "ok":
                return Content(html, status="ok", source="feed")

        try:                                   # direct — may pass from a real IP
            resp = net.fetch(url, retries=1)
            result = self.clean(resp.text(), resp.url, source="direct")
            if result.status == "ok":
                return result
        except net.FetchError:
            pass

        if ctx.browser_fetch is not None:      # the app's own WebKit view
            try:
                html = ctx.browser_fetch(url)
                if html:
                    result = self.clean(html, url, source="browser")
                    if result.status == "ok":
                        return result
            except Exception:
                pass

        return self._from_wayback(url)

    def _from_wayback(self, url: str) -> Content:
        ts = self._best_snapshot(url)
        if not ts:
            return Content("", status="error", source="wayback:none")
        try:
            resp = net.fetch(WAYBACK.format(ts=ts, url=url), timeout=90)
        except net.FetchError as exc:
            return Content("", status="error", source=f"wayback:{exc.status}")
        result = self.clean(resp.text(), url, source="wayback")
        return result

    @staticmethod
    def _best_snapshot(url: str) -> str | None:
        """Prefer an early snapshot: less accumulated site chrome, fewer dead images."""
        api = ("http://archive.org/wayback/available?url="
               + url.replace("https://", "").replace("http://", ""))
        try:
            data = net.fetch_json(api, timeout=45, retries=2)
        except Exception:
            return None
        snap = (data.get("archived_snapshots") or {}).get("closest") or {}
        return snap.get("timestamp") if snap.get("available") else None

    def postprocess(self, html: str) -> str:
        soup = htmlutil.parse(html)
        for sel in ("#comments", ".comments", ".comment-container", "#respond",
                    ".post-meta", ".postmeta", ".sharedaddy", ".adspace-widget",
                    ".widget", "#sidebar", ".bns-smf-feeds", ".wm-ipp"):
            for node in soup.select(sel):
                node.decompose()
        # Wayback injects its own banner markup into archived pages.
        for node in soup.select('[id^="wm-"]'):
            node.decompose()
        return soup.decode()


def _tag(blob: str, name: str) -> str | None:
    m = re.search(rf"<{re.escape(name)}[^>]*>(.*?)</{re.escape(name)}>", blob, re.S)
    return m.group(1).strip() if m else None


def _cdata(text: str) -> str:
    m = re.match(r"\s*<!\[CDATA\[(.*?)\]\]>\s*$", text, re.S)
    return m.group(1) if m else text


def _unescape(text: str) -> str:
    import html as _h
    return _h.unescape(text)
