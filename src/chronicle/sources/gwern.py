"""gwern.net — not a blog, but a versioned wiki of long-form essays.

Every page carries Dublin Core metadata: dc.date.issued is the original
publication date and dcterms.modified is the last revision. Gwern revises
essays for years, so using the modified date (as a feed reader would) puts
2009 essays in 2026. We read issued and keep modified separately.

Newsletter pages are the exception: their issued date is a template artefact,
so for those the date comes from the URL.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .. import dates, htmlutil, net
from .base import Content, Context, Source, Stub, probe_all

SITEMAP = "https://gwern.net/sitemap.xml"
INDEX = "https://gwern.net/index"

# Utility pages that aren't reading material.
DENY_PREFIXES = ("/doc/", "/static/", "/metadata/", "/lorem")
DENY_EXACT = {
    "/index", "/404", "/static", "/sitemap", "/atom", "/rss",
    "/design-graveyard-test", "/lorem-code", "/lorem-halfmark",
    "/lorem-figure", "/lorem-link", "/lorem-block", "/lorem-inline",
    "/lorem-multicolumn", "/lorem-table", "/lorem-math",
}
_EXT_RE = re.compile(r"\.[a-z0-9]{1,5}$", re.I)
_NEWSLETTER_RE = re.compile(r"^/newsletter/(\d{4})/(\d{1,2}|\d{2})$")


class GwernSource(Source):
    plugin_id = "gwern"
    display_name = "Gwern"
    content_selectors = ["#markdownBody", "#markdown-body", "article", "main"]
    discover_concurrency = 5
    image_blocklist = Source.image_blocklist + ("/static/img/icon/", "logo.svg")

    def discover(self, ctx: Context):
        ctx.say("Reading gwern.net sitemap…")
        urls = self._candidate_urls(ctx)
        ordered = sorted(urls)

        # gwern.net has no feed and its sitemap carries no <lastmod>, so there
        # is no date to enumerate by: the only way to learn when a page was
        # published is to fetch the page and read its dc.date.issued. That is
        # 669 requests, and at the polite rate this costs about seven minutes
        # -- every sync, including "fetch new posts".
        #
        # So the enumeration is by identity instead of by date. A page already
        # archived with its content, or already judged not to be an article,
        # can tell us nothing we do not have; what is left is what the site
        # has published since. On a built archive that is a handful of pages,
        # and the sync takes seconds.
        todo = [(i, url) for i, url in enumerate(ordered)
                if not self._resolved(ctx, url)]
        ctx.say(f"gwern.net: {len(ordered)} candidate pages, "
                f"{len(todo)} not yet resolved")
        ctx.result_note = f"sitemap {len(ordered)}, examined {len(todo)}"

        for stub in probe_all(ctx, todo,
                              lambda item: self._probe(ctx, item[1], item[0]),
                              workers=self.discover_concurrency,
                              label="gwern.net: reading metadata"):
            if stub is not None:
                yield stub

    @staticmethod
    def _resolved(ctx: Context, url: str) -> bool:
        """Whether an earlier sync already settled what this page is.

        Either it is archived with a date and a body, or it was fetched and
        found not to be an article at all. Both verdicts survive in the
        library, and re-deriving either costs a request.
        """
        guid = net.canonical_url(url)
        return ctx.no_direct(guid) or bool(ctx.rejected and guid in ctx.rejected)

    # -- discovery helpers -------------------------------------------------

    def _candidate_urls(self, ctx: Context) -> set[str]:
        urls: set[str] = set()
        try:
            xml = net.fetch_text(SITEMAP, timeout=90, max_bytes=40_000_000)
            for loc in re.findall(r"<loc>(.*?)</loc>", xml):
                if self._is_candidate(loc):
                    urls.add(loc.strip())
        except net.FetchError as exc:
            ctx.say(f"gwern.net sitemap unavailable ({exc.status}); using index")

        # The index page is the curated essay list — a useful second opinion,
        # and the fallback if the sitemap fails.
        try:
            html = net.fetch_text(INDEX)
            soup = htmlutil.parse(html)
            for a in soup.find_all("a", href=True):
                full = net.absolutise(INDEX, a["href"])
                if self._is_candidate(full):
                    urls.add(full.split("#")[0])
        except net.FetchError:
            pass
        return urls

    @staticmethod
    def _is_candidate(url: str) -> bool:
        try:
            p = urlparse(url)
        except ValueError:
            return False
        if p.netloc not in ("gwern.net", "www.gwern.net"):
            return False
        path = p.path.rstrip("/") or "/"
        if path in DENY_EXACT or path == "/":
            return False
        if any(path.startswith(pre) for pre in DENY_PREFIXES):
            return False
        if path.endswith("/index"):
            return False
        if _EXT_RE.search(path):
            return False
        return True

    def _probe(self, ctx: Context, url: str, order: int) -> Stub | None:
        """Fetch a page just far enough to read its metadata."""
        try:
            resp = net.fetch(url)
        except net.FetchError:
            return None
        html = resp.text()
        soup = htmlutil.parse(html)

        issued = htmlutil.meta_content(
            soup, "dc.date.issued", "citation_publication_date", "dcterms.issued")
        modified = htmlutil.meta_content(soup, "dcterms.modified", "dc.date.modified")

        path = urlparse(url).path.rstrip("/")
        nl = _NEWSLETTER_RE.match(path)
        if nl:
            year, month = int(nl.group(1)), int(nl.group(2))
            date = dates._mk(year, month, 1, precision=dates.PRECISION_MONTH,
                             confidence="high", source="url:newsletter")
        elif issued:
            date = dates.parse_iso(issued, confidence="exact",
                                   source="meta:dc.date.issued")
        else:
            date = dates.UNKNOWN

        if not date.known:
            # Recorded, not just skipped: a page with no dc.date.issued is
            # site furniture or an index, and without the verdict every sync
            # from here on pays a request to reach the same conclusion.
            ctx.reject(net.canonical_url(url))
            return None  # without a date it cannot join the chronology

        title = htmlutil.clean_title(htmlutil.page_title(soup), "Gwern.net", "Gwern")
        mod = dates.parse_iso(modified, source="meta:dcterms.modified") if modified else None

        return Stub(
            guid=net.canonical_url(url), url=url, title=title, date=date,
            author="Gwern Branwen", source_order=order,
            raw_html=html, base_url=resp.url, content_source="direct",
        )

    def fetch_content(self, ctx: Context, url: str, stub_html=None, base_url=None,
                      extra: dict | None = None) -> Content:
        if stub_html:
            return self.clean(stub_html, base_url or url, source="direct")
        resp = net.fetch(url)
        return self.clean(resp.text(), resp.url, source="direct")

    def postprocess(self, html: str) -> str:
        """Strip gwern's interactive furniture, keep the prose."""
        soup = htmlutil.parse(html)
        for sel in ("#page-metadata", ".page-metadata", "#TOC", ".TOC",
                    ".footnote-back", ".link-bibliography", "#link-bibliography",
                    ".backlinks", "#backlinks", ".similars", "#similars",
                    ".aux-links-append", ".collapse-toggle"):
            for node in soup.select(sel):
                node.decompose()
        # Inline annotation popups are duplicated content in a reading view.
        for node in soup.select(".include-annotation, .aux-links-container"):
            node.decompose()
        return soup.decode()
