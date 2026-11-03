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

from .. import htmlutil, net
from .base import Context, Source, Stub, probe_all
from . import discovery
from .discovery import Candidate, Report, extract_date  # noqa: F401 (re-export)


class GenericSource(Source):
    plugin_id = "generic"
    honours_limit = True
    display_name = "Website"

    # -- discovery ---------------------------------------------------------

    def discover(self, ctx: Context):
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
            feed_items = discovery.read_feed(feed_url, ctx, self.in_scope)
            report.feed_url, report.feed_count = feed_url, len(feed_items)
        # Remember where the feed lives — or, when the site was reachable,
        # that there is none — so later syncs skip the search.
        if ((feed_url or homepage_html)
                and self.config.get("feed") != (feed_url or "")):
            ctx.config_updates["feed"] = feed_url or ""

        # 2. Sitemap: the site's own enumeration of everything. This is the
        #    completeness route and also the expensive one -- a sitemap index
        #    can be dozens of child documents -- and it is ordered by nothing
        #    in particular, so it cannot be stopped early. "Fetch new posts"
        #    therefore skips it: the feed and the front of the archive index
        #    already carry anything published since the last update.
        sitemap_urls: list[str] = []
        sitemap_src = None
        # Under a "last N posts" limit a feed that already dated N posts has
        # named the newest N; the sitemap and the deep archive could only add
        # older ones, so they are not read.
        feed_covers_limit = bool(
            ctx.limit and sum(1 for it in feed_items
                              if it.date.known and self.in_scope(it.link))
            >= ctx.limit)
        if not ctx.newest_only and not feed_covers_limit:
            sitemap_urls, sitemap_src = discovery.read_sitemaps(
                base, self.config, ctx)
            report.sitemap_url, report.sitemap_count = sitemap_src, len(sitemap_urls)
            if sitemap_urls:
                ctx.say(f"{self.name}: {len(sitemap_urls)} pages in sitemap")
            if sitemap_src and sitemap_src != self.config.get("sitemap"):
                ctx.config_updates["sitemap"] = sitemap_src
            elif (not sitemap_src and homepage_html
                  and self.config.get("sitemap") != ""):
                # None found: remember it, as with the feed and the index, so
                # a full scan does not pay six requests to learn it again.
                ctx.config_updates["sitemap"] = ""

        # 3. Archive pages: always consulted — the site's own listing is both
        #    an enumeration and, on many blogs, the only place dates appear.
        #    Its pagination stops as soon as a whole page is already archived,
        #    so on a re-sync this costs one or two requests.
        def already_known(url: str) -> bool:
            guid = net.canonical_url(url)
            return (ctx.settled(guid)
                    or bool(ctx.rejected and guid in ctx.rejected))

        archive_entries, index = discovery.read_archive(
            base, self.config, ctx, self.in_scope, is_known=already_known,
            max_pages=(discovery.NEWEST_ARCHIVE_PAGES
                       if ctx.newest_only or feed_covers_limit else None),
            admit=self._admit())
        report.archive_index = index
        report.archive_count = len(archive_entries)
        if archive_entries:
            ctx.say(f"{self.name}: {len(archive_entries)} posts listed at {index}")
        if ((index or homepage_html)
                and self.config.get("index") != (index or "")):
            ctx.config_updates["index"] = index or ""

        pool = discovery.merge(feed_items, sitemap_urls, archive_entries,
                               self.in_scope, host=urlparse(base).netloc)
        report.candidates = len(pool)
        if ctx.limit and ctx.newest_only:
            pool = self._only_fresh(pool, ctx)
        elif ctx.limit and len(pool) > ctx.limit:
            pool = discovery.newest(pool, ctx.limit)
        ctx.result_note = report.summary()

        if not pool:
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
                # But cheap evidence gathered this sync can still *improve*
                # what is stored — an archive listing's date correcting a
                # wrong or missing one — at no request cost.
                report.skipped_known += 1
                if (cand.date.known and ctx.no_direct(cand.guid)
                        and discovery._rank(cand.date) >= ctx.known_rank(cand.guid)):
                    yielded.add(cand.guid)
                    yield Stub(guid=cand.guid, url=cand.url,
                               title=cand.title or "", date=cand.date,
                               source_order=cand.order)
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

    def _admit(self):
        """Membership scoping, only for a source whose configured index IS its
        section (a category or tag page whose posts live at the site root).
        A recipe that scopes by prefix but happens to have found an unrelated
        archive index must not have every link of that index admitted."""
        if not self.path_prefix:
            return None
        index = (self.config.get("index") or "").rstrip("/")
        return self.scope_by_membership if index == self.path_prefix else None

    @staticmethod
    def _only_fresh(pool, ctx):
        """Under a limit, "fetch new posts" keeps what is already held and
        what is not older than the archive; an old post that merely resurfaces
        (a feed listing its whole history) is not probed. Undated unknowns are
        capped at the limit."""
        out, undated = {}, 0
        for guid, cand in pool.items():
            if guid in (ctx.known or {}):
                out[guid] = cand
            elif cand.date.known:
                if not ctx.below_floor(cand.date):
                    out[guid] = cand
            else:
                undated += 1
                if undated <= ctx.limit:
                    out[guid] = cand
        return out

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
        # A teaser in the feed is not the article: the page is.
        use_feed = bool(cand.feed_html) and not cand.summary_only
        return Stub(guid=guid, url=resp.url, title=title, date=date,
                    source_order=cand.order,
                    raw_html=cand.feed_html if use_feed else html,
                    base_url=resp.url,
                    content_source="feed" if use_feed else "direct")

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
