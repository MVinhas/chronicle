"""Blog-agnostic source for sites added by the user.

Discovery is delegated to the evidence-merging engine in `discovery.py`: every
cheap route (feed, sitemaps, the site's own archive pages) is enumerated, the
results are merged into one candidate pool, and only candidates that still
need something are fetched. Dates come from whichever signal a page actually
provides, each carrying its own confidence so unreliable ones stay visibly
unreliable.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .. import dates, htmlutil, net
from .base import Content, Context, Source, Stub, probe_all
from . import discovery, wayback
from .discovery import Candidate, Report, extract_date  # noqa: F401 (re-export)


class GenericSource(Source):
    plugin_id = "generic"
    display_name = "Website"

    def fetch_content(self, ctx: Context, url: str, stub_html: str | None = None,
                      base_url: str | None = None, extra: dict | None = None) -> Content:
        snapshot = (extra or {}).get("snapshot")
        if snapshot:
            # The discovery probe already fetched and de-bannered this
            # snapshot; refetching it would double every request against the
            # slowest backend there is.
            if stub_html:
                return self.clean(stub_html, url, source="wayback")
            try:
                resp = wayback.fetch_snapshot(url, snapshot)
            except net.FetchError as exc:
                return Content("", status="error", source=f"wayback:{exc.status}")
            if resp is None:
                return Content("", status="error", source="wayback:none")
            soup = htmlutil.parse(resp.text())
            wayback.strip_banner(soup)
            return self.clean(soup.decode(), url, source="wayback")
        return super().fetch_content(ctx, url, stub_html, base_url, extra)

    # -- discovery ---------------------------------------------------------

    def discover(self, ctx: Context):
        if (self.config.get("strategy") or "auto") == "wayback":
            yield from self._try_wayback(ctx)
            return

        base = (self.homepage or "").rstrip("/")
        report = Report()

        # 1. Feed: one or two requests for exact dates and full bodies.
        feed_items: list[discovery.FeedItem] = []
        homepage_html = None
        try:
            homepage_html = net.fetch_text(base or self.homepage, retries=1)
        except net.FetchError:
            pass
        feed_url = discovery.find_feed_url(base, homepage_html, self.config, ctx)
        if feed_url:
            ctx.say(f"{self.name}: reading feed…")
            feed_items = discovery.read_feed(feed_url, ctx)
            report.feed_url, report.feed_count = feed_url, len(feed_items)
        # Remember where the feed lives — or, when the site was reachable,
        # that there is none — so later syncs skip the search.
        if ((feed_url or homepage_html)
                and self.config.get("feed") != (feed_url or "")):
            ctx.config_updates["feed"] = feed_url or ""

        # 2. Sitemap: the site's own enumeration of everything.
        sitemap_urls, sitemap_src = discovery.read_sitemaps(base, self.config, ctx)
        report.sitemap_url, report.sitemap_count = sitemap_src, len(sitemap_urls)
        if sitemap_urls:
            ctx.say(f"{self.name}: {len(sitemap_urls)} pages in sitemap")
        if sitemap_src and sitemap_src != self.config.get("sitemap"):
            ctx.config_updates["sitemap"] = sitemap_src

        # 3. Archive pages: crawled when they are the best route available, or
        #    when another route looks incomplete and needs corroborating.
        archive_urls: list[str] = []
        if self._should_crawl_archive(feed_items, sitemap_urls):
            archive_urls, index = discovery.read_archive(
                base, self.config, ctx, self.in_scope)
            report.archive_index, report.archive_count = index, len(archive_urls)
            if archive_urls:
                ctx.say(f"{self.name}: {len(archive_urls)} posts listed at {index}")
            if ((index or homepage_html)
                    and self.config.get("index") != (index or "")):
                ctx.config_updates["index"] = index or ""

        pool = discovery.merge(feed_items, sitemap_urls, archive_urls,
                               self.in_scope)
        report.candidates = len(pool)
        ctx.result_note = report.summary()

        if not pool:
            # Every direct route came up empty. The Internet Archive can often
            # still rebuild the history from here, but a full CDX crawl is
            # minutes of work, so it stays a deliberate, user-triggered
            # fallback (see sources_view._on_try_wayback).
            if self.path_prefix:
                ctx.say(f"{self.name}: nothing found under {self.path_prefix}/")
            else:
                ctx.say(f"{self.name}: no articles found directly")
            return

        # -- yield what is already complete, probe the rest ----------------
        yielded: set[str] = set()
        to_probe: list[Candidate] = []
        for cand in discovery.probe_order(pool):
            if cand.complete:
                # A feed item with a date and a body needs no page fetch.
                yielded.add(cand.guid)
                yield Stub(guid=cand.guid, url=cand.url, title=cand.title,
                           date=cand.date, source_order=cand.order,
                           raw_html=cand.feed_html, base_url=cand.url,
                           content_source="feed")
            elif ctx.no_direct(cand.guid) or (ctx.rejected and
                                              cand.guid in ctx.rejected):
                # Already archived (or already judged not to be an article):
                # a re-sync must not pay one request per page it has seen.
                report.skipped_known += 1
            else:
                to_probe.append(cand)

        if report.skipped_known:
            ctx.say(f"{self.name}: {report.skipped_known} already checked, "
                    f"{len(to_probe)} to examine")

        for stub in probe_all(ctx, to_probe, lambda c: self._probe(c, ctx),
                              workers=self.discover_concurrency,
                              label=f"{self.name}: reading",
                              total=len(to_probe)):
            if stub is None or stub.guid in yielded or ctx.settled(stub.guid):
                continue
            yielded.add(stub.guid)
            yield stub

    def _should_crawl_archive(self, feed_items, sitemap_urls) -> bool:
        """Crawl the site's own archive listing only when it can add something.

        With no sitemap it is the primary enumeration. With one, it is crawled
        only when the sitemap looks incomplete: too small to be the archive, or
        missing articles the feed proves exist.
        """
        if self.config.get("strategy") == "archive" or self.config.get("index"):
            return True
        if not sitemap_urls:
            return True
        if len(sitemap_urls) < 10:
            return True
        in_sitemap = {net.canonical_url(u) for u in sitemap_urls}
        return any(net.canonical_url(i.link) not in in_sitemap
                   for i in feed_items)

    # -- probing -----------------------------------------------------------

    def _probe(self, cand: Candidate, ctx: Context) -> Stub | None:
        """Fetch one candidate page and decide whether it is an article.

        The fetched HTML rides along on the stub, so the content pass never
        refetches a page discovery already paid for.
        """
        try:
            resp = net.fetch(cand.url)
        except net.FetchError:
            if cand.feed_html or cand.date.known:
                # The page is gone but the feed vouched for it; keep what the
                # feed gave rather than dropping a real post.
                return Stub(guid=cand.guid, url=cand.url,
                            title=cand.title or "Untitled", date=cand.date,
                            source_order=cand.order, raw_html=cand.feed_html,
                            base_url=cand.url,
                            content_source="feed" if cand.feed_html else "")
            return None
        html = resp.text()
        soup = htmlutil.parse(html)

        page_date = extract_date(soup, resp.url)
        date = page_date if discovery._rank(page_date) > discovery._rank(cand.date) \
            else cand.date

        if not date.known and not self._is_article(cand, soup):
            ctx.reject(cand.guid)
            return None

        # Redirects resolved: the guid comes from where the page actually
        # lives, so /post?id=1 and its pretty permalink become one article.
        guid = net.canonical_url(resp.url)
        title = cand.title or htmlutil.clean_title(
            htmlutil.page_title(soup), self.name)
        return Stub(guid=guid, url=resp.url, title=title, date=date,
                    source_order=cand.order, raw_html=cand.feed_html or html,
                    base_url=resp.url,
                    content_source="feed" if cand.feed_html else "direct")

    def _is_article(self, cand: Candidate, soup) -> bool:
        """Is an undated page still a post? Structured signals first, then —
        for pages a feed or archive listing vouched for — the shape of the
        page itself."""
        if discovery._looks_like_article(soup):
            return True
        if not (cand.evidence & {"feed", "archive"}):
            return False
        node = htmlutil.extract_main(soup, [])
        if node is None:
            return False
        if htmlutil._link_density(node) > 0.5:
            return False   # a listing, not an article
        return len(node.get_text(" ", strip=True).split()) >= 30

    # -- wayback -------------------------------------------------------------

    def _try_wayback(self, ctx: Context):
        """Rebuild the archive entirely from the Internet Archive.

        Used when the site itself does not answer at all, or when the user
        asks for it after the direct routes found nothing. Every URL the CDX
        index has ever crawled is a candidate; each is probed through its own
        archived snapshot, and the snapshot HTML is kept for the content pass.
        """
        host = urlparse(self.homepage or "").netloc
        ctx.say(f"{self.name}: searching the Internet Archive — this takes "
                f"a couple of minutes…")
        snapshots = wayback.list_snapshots(host)
        snapshots = [(u, ts) for u, ts in snapshots
                     if self.in_scope(u) and discovery.classify_url(u) == "maybe"
                     and not ctx.settled(net.canonical_url(u))]
        ctx.say(f"{self.name}: {len(snapshots)} pages to examine in the archive")

        items = list(enumerate(snapshots))
        for stub in probe_all(ctx, items,
                              lambda item: self._probe_wayback(*item[1], item[0]),
                              workers=self.discover_concurrency,
                              label=f"{self.name}: reading", total=len(items)):
            if stub is not None:
                yield stub

    def _probe_wayback(self, url: str, timestamp: str, order: int) -> Stub | None:
        try:
            resp = wayback.fetch_snapshot(url, timestamp)
        except net.FetchError:
            return None
        if resp is None:
            return None
        soup = htmlutil.parse(resp.text())
        wayback.strip_banner(soup)
        date = extract_date(soup, url)
        if not date.known:
            return None
        return Stub(guid=net.canonical_url(url), url=url,
                    title=htmlutil.clean_title(htmlutil.page_title(soup), self.name),
                    date=date, source_order=order, raw_html=soup.decode(),
                    base_url=url, extra={"snapshot": timestamp})
