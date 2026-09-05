"""Ghost-powered sites.

Ghost's Content API needs a key, but every Ghost front-end embeds its own
public key in the page so the theme can call the API — so we read it from the
homepage rather than asking for it. The API then returns every post ever
published, with exact published_at timestamps and full bodies.

Ghost sites often paywall part of their archive. Members-only posts come back
with a truncated body; we store what is public and mark the article
'paywalled' instead of pretending it is complete.
"""
from __future__ import annotations

import json
import urllib.parse
import re

from .. import dates, htmlutil, net
from .base import Content, Context, Source, Stub, assess

_KEY_RE = re.compile(r'(?:key|apiKey)["\']?\s*[:=]\s*["\']([0-9a-f]{26})["\']', re.I)
PAGE_SIZE = 50


class GhostSource(Source):
    plugin_id = "ghost"
    display_name = "Ghost site"
    content_selectors = [".gh-content", ".post-content", ".article-content",
                         "article", "main"]

    def _api_key(self, ctx: Context) -> str | None:
        key = self.config.get("content_key")
        if key:
            return key
        for path in ("", "/"):
            try:
                html = net.fetch_text(self.homepage.rstrip("/") + path)
            except net.FetchError:
                continue
            m = _KEY_RE.search(html)
            if m:
                return m.group(1)
        return None

    def discover(self, ctx: Context):
        key = self._api_key(ctx)
        if not key:
            ctx.say(f"{self.name}: no Ghost API key found; falling back to sitemap")
            yield from self._discover_sitemap(ctx)
            return

        base = self.homepage.rstrip("/")
        # Ghost filters server-side too, so a routine update asks only for what
        # was published after what we hold rather than paging the archive.
        since = ctx.since()
        window = (f"&filter={urllib.parse.quote(f'published_at:>{since}')}"
                  if since else "")
        page, order, total = 1, 0, None
        while True:
            ctx.check()
            url = (f"{base}/ghost/api/content/posts/?key={key}&limit={PAGE_SIZE}"
                   f"&page={page}&order=published_at%20asc"
                   f"&fields=id,title,url,published_at,updated_at,excerpt,visibility,slug"
                   f"&formats=html{window}")
            try:
                data = net.fetch_json(url)
            except (net.FetchError, json.JSONDecodeError):
                if page == 1:
                    yield from self._discover_sitemap(ctx)
                    return
                break
            posts = data.get("posts") or []
            if not posts:
                break
            if total is None:
                total = (data.get("meta", {}).get("pagination", {}) or {}).get("total")
                ctx.say(f"{self.name}: {total or '?'} posts to import")
            for post in posts:
                yield self._stub(post, order)
                order += 1
            ctx.say(f"{self.name}: {order} posts", order / total if total else None)
            pag = data.get("meta", {}).get("pagination", {}) or {}
            if not pag.get("next"):
                break
            page = pag["next"]

    def _stub(self, post: dict, order: int) -> Stub:
        url = post.get("url") or ""
        date = dates.parse_iso(post.get("published_at") or "",
                               confidence="exact", source="ghost:published_at")
        body = post.get("html") or ""
        visibility = post.get("visibility") or "public"
        hint = None if visibility == "public" else "paywalled"
        return Stub(
            guid=net.canonical_url(url) or f"ghost:{post.get('id')}",
            url=url, title=(post.get("title") or "Untitled").strip(),
            date=date, source_order=order, raw_html=body or None,
            base_url=url, content_source="api", status_hint=hint,
        )

    def _discover_sitemap(self, ctx: Context):
        """Ghost publishes sitemap-posts.xml with a lastmod per post."""
        base = self.homepage.rstrip("/")
        try:
            xml = net.fetch_text(f"{base}/sitemap-posts.xml", timeout=60)
        except net.FetchError:
            ctx.say(f"{self.name}: no sitemap available")
            return
        entries = re.findall(r"<url>(.*?)</url>", xml, re.S)
        ctx.say(f"{self.name}: {len(entries)} posts in sitemap")
        for order, entry in enumerate(entries):
            ctx.check()
            loc = re.search(r"<loc>(.*?)</loc>", entry)
            if not loc:
                continue
            url = loc.group(1).strip()
            # lastmod is a modification time, not a publication date: only a hint.
            yield Stub(guid=net.canonical_url(url), url=url,
                       title=url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title(),
                       date=dates.UNKNOWN, source_order=order)

    def fetch_content(self, ctx: Context, url: str, stub_html=None, base_url=None,
                      extra: dict | None = None) -> Content:
        if stub_html:
            html = htmlutil.sanitise(htmlutil.parse(f"<div>{stub_html}</div>").div,
                                     base_url or url)
            html = self.drop_decorative_images(html)
            html = self.postprocess(html)
            return Content(html, status=assess(html), source="api")
        resp = net.fetch(url)
        return self.clean(resp.text(), resp.url, source="direct")

    def postprocess(self, html: str) -> str:
        soup = htmlutil.parse(html)
        for sel in (".gh-post-upgrade-cta", ".kg-card.kg-cta-card", ".members-cta",
                    ".gh-signup", ".subscribe-form", ".portal-trigger"):
            for node in soup.select(sel):
                node.decompose()
        return soup.decode()
