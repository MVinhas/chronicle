"""WordPress sites exposing the REST API.

Where it is reachable the REST API is the ideal route: it enumerates every
post ever published, with the publisher's own timestamps and the full rendered
body including images — no scraping, and none of the truncation that makes a
feed useless for building an archive.
"""
from __future__ import annotations

import json

from .. import dates, htmlutil, net
from .base import Content, Context, Source, Stub, assess

FIELDS = "id,date_gmt,modified_gmt,link,title,content,excerpt,slug,status,type"
PAGE_SIZE = 50


class WordPressSource(Source):
    plugin_id = "wordpress"
    display_name = "WordPress site"
    content_selectors = [".entry-content", ".post-content", "article .content",
                         ".wp-block-post-content", "article", "main"]
    # Deliberately narrow: on illustration-heavy blogs the pictures *are* the
    # article, so only tracking pixels and avatars are discarded.
    image_blocklist = ("pixel.wp.com", "stats.wordpress.com", "scorecardresearch",
                       "gravatar.com/avatar", "/emoji/")

    @property
    def api_root(self) -> str:
        return self.config.get("api_root") or f"{self.homepage.rstrip('/')}/wp-json/wp/v2"

    def discover(self, ctx: Context):
        # The REST API can do the filtering itself: `after` is the whole of
        # "fetch new posts" in one parameter, so a routine update reads one
        # page of nothing instead of paginating the entire history.
        since = ctx.since()
        window = f"&after={since}" if since else ""
        ctx.say(f"Querying {self.name} REST API"
                f"{' for posts since ' + since[:10] if since else ''}…")
        page, order, total = 1, 0, None
        while True:
            ctx.check()
            url = (f"{self.api_root}/posts?per_page={PAGE_SIZE}&page={page}"
                   f"&orderby=date&order=asc&_fields={FIELDS}{window}")
            try:
                resp = net.fetch(url, headers={"Accept": "application/json"})
                posts = json.loads(resp.text())
            except (net.FetchError, json.JSONDecodeError) as exc:
                if page == 1:
                    raise
                break
            if not posts:
                break
            if total is None:
                total = int(resp.headers.get("x-wp-total", 0)) or None
                ctx.say(f"{self.name}: {total or 'unknown'} posts to "
                        f"{'check' if since else 'import'}")

            for post in posts:
                if post.get("status") not in (None, "publish"):
                    continue
                yield self._stub(post, order)
                order += 1
            ctx.say(f"{self.name}: {order} posts",
                    order / total if total else None)
            if len(posts) < PAGE_SIZE:
                break
            page += 1

    def _stub(self, post: dict, order: int) -> Stub:
        link = post.get("link") or ""
        raw_title = (post.get("title") or {}).get("rendered") or "Untitled"
        title = htmlutil.parse(raw_title).get_text(" ", strip=True) or "Untitled"
        date = dates.parse_iso((post.get("date_gmt") or "") + "Z",
                               confidence="exact", source="wp:date_gmt")
        if not date.known:
            date = dates.parse_from_url(link)
        body = (post.get("content") or {}).get("rendered") or ""
        return Stub(
            guid=net.canonical_url(link) or f"wp:{post.get('id')}",
            url=link, title=title, date=date, source_order=order,
            raw_html=body or None, base_url=link, content_source="api",
        )

    def fetch_content(self, ctx: Context, url: str, stub_html=None, base_url=None,
                      extra: dict | None = None) -> Content:
        if stub_html:
            # API bodies are already just the post content; sanitise, don't extract.
            html = htmlutil.sanitise(htmlutil.parse(f"<div>{stub_html}</div>").div,
                                     base_url or url)
            html = self.drop_decorative_images(html)
            html = self.postprocess(html)
            status = assess(html)
            if status == "ok":
                return Content(html, status=status, source="api")
            # An empty API body means the post lives only on the page itself.
            try:
                resp = net.fetch(url)
                fallback = self.clean(resp.text(), resp.url, source="direct")
                if fallback.status == "ok":
                    return fallback
            except net.FetchError:
                pass
            return Content(html, status=status, source="api")
        resp = net.fetch(url)
        return self.clean(resp.text(), resp.url, source="direct")

    def postprocess(self, html: str) -> str:
        """Strip the furniture WordPress themes bolt onto post bodies."""
        soup = htmlutil.parse(html)
        for sel in (".sharedaddy", ".jp-relatedposts", ".wpcnt", ".author-box",
                    ".post-navigation", ".entry-footer", ".entry-meta",
                    ".mailing-list-signup", ".newsletter-signup",
                    ".wp-block-post-comments", "#comments", "#respond"):
            for node in soup.select(sel):
                node.decompose()
        return soup.decode()
