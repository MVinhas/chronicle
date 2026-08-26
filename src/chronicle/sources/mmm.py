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
from . import wayback

FEEDBURNER = "https://feeds.feedburner.com/mrmoneymustache"

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
    # The Internet Archive answers a page in 10-15 seconds. Fetched one at a
    # time that is hours for a large blog, so overlap more than usual.
    fetch_concurrency = 6

    def __init__(self, row, config=None):
        super().__init__(row, config)
        self._origin_blocked = False

    @property
    def host(self) -> str:
        return urlparse(self.homepage or "https://www.mrmoneymustache.com/").netloc \
            or "www.mrmoneymustache.com"

    # -- discovery ---------------------------------------------------------

    def discover(self, ctx: Context, since_year: int | None = None):
        seen: dict[str, Stub] = {}

        # 1. Recent posts, with full bodies, straight from the feed. Yield these
        #    first so an interrupted run still leaves something readable.
        ctx.say(f"{self.name}: reading the feed…")
        feed = self._from_feed(ctx)
        for stub in feed:
            seen[stub.guid] = stub
        ctx.say(f"{self.name}: {len(feed)} recent posts from the feed")
        for stub in sorted(feed, key=lambda s: s.date.iso or ""):
            yield stub

        # 2. The historical archive, from the Internet Archive. The origin
        #    answers 403 to every client, so this is the only route to it.
        ctx.check()
        ctx.say(f"{self.name}: searching the Internet Archive — this takes "
                f"a couple of minutes…")
        found = list(self._cdx(ctx, since_year))
        ctx.say(f"{self.name}: {len(found)} posts found in the archive")

        order = 0
        for url, date, timestamp in sorted(found, key=lambda r: r[1].iso or ""):
            ctx.check()
            guid = net.canonical_url(url)
            order += 1
            if guid in seen:
                continue
            slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
            yield Stub(
                guid=guid, url=url,
                title=slug.replace("-", " ").strip().capitalize(),
                date=date, author=self.name, source_order=order,
                extra={"snapshot": timestamp})

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

    def _cdx(self, ctx: Context, since_year: int | None = None):
        """Every archived permalink, with the snapshot id to fetch it from."""
        snapshots = wayback.list_snapshots(self.host, since_year=since_year)
        if not snapshots:
            ctx.say(f"{self.name}: the Internet Archive did not answer")
            return

        best: dict[str, tuple[str, object, str]] = {}
        for raw, timestamp in snapshots:
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
            date = dates._mk(int(y), int(mo), int(d), precision=dates.PRECISION_DAY,
                             confidence="high", source="url:permalink")
            if not date.known:
                continue
            # Prefer the latest snapshot: earlier ones are often partial crawls.
            if slug not in best or timestamp > best[slug][2]:
                best[slug] = (f"https://{self.host}/{y}/{mo}/{d}/{slug}/",
                              date, timestamp)
        yield from best.values()

    # -- content -----------------------------------------------------------

    def fetch_content(self, ctx: Context, url: str, stub_html=None, base_url=None,
                      extra: dict | None = None) -> Content:
        extra = extra or {}
        if stub_html:
            html = htmlutil.sanitise(htmlutil.parse(f"<div>{stub_html}</div>").div,
                                     base_url or url)
            html = self.drop_decorative_images(html)
            html = self.postprocess(html)
            if assess(html) == "ok":
                return Content(html, status="ok", source="feed")

        # The origin is behind a bot challenge. Try it once per run; once it
        # has refused, stop paying the timeout on every remaining article.
        if not self._origin_blocked:
            try:
                resp = net.fetch(url, retries=1)
                result = self.clean(resp.text(), resp.url, source="direct")
                if result.status == "ok":
                    return result
            except net.FetchError as exc:
                if exc.status in (403, 401, 429):
                    self._origin_blocked = True
                    ctx.say(f"{self.name}: the site is blocking direct access; "
                            f"using the Internet Archive")

        if ctx.browser_fetch is not None:      # the app's own WebKit view
            try:
                html = ctx.browser_fetch(url)
                if html:
                    result = self.clean(html, url, source="browser")
                    if result.status == "ok":
                        return result
            except Exception:
                pass

        return self._from_wayback(url, extra.get("snapshot"))

    def _from_wayback(self, url: str, snapshot: str | None = None) -> Content:
        # Discovery already recorded a snapshot id, so the availability lookup
        # is only needed for articles carried over from an interrupted run.
        try:
            resp = wayback.fetch_snapshot(url, snapshot)
        except net.FetchError as exc:
            return Content("", status="error", source=f"wayback:{exc.status}")
        if resp is None:
            return Content("", status="error", source="wayback:none")
        return self.clean(resp.text(), url, source="wayback")

    def postprocess(self, html: str) -> str:
        soup = htmlutil.parse(html)
        for sel in ("#comments", ".comments", ".comment-container", "#respond",
                    ".post-meta", ".postmeta", ".sharedaddy", ".adspace-widget",
                    ".widget", "#sidebar", ".bns-smf-feeds", ".wm-ipp"):
            for node in soup.select(sel):
                node.decompose()
        wayback.strip_banner(soup)
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
