"""The reading surface: stylesheet and document template.

Long-form reading is the whole point of the app, so the measure, leading and
scale here are the design, not decoration: ~66 characters per line, 1.62
leading, and a single serif optimised for continuous text.

Two palettes: a warm paper light theme and a low-contrast dark one. Neither
uses pure black or pure white -- both are fatiguing over a long read. The
active palette is chosen by the app from libadwaita's colour scheme rather
than by a CSS media query, because the WebView does not reliably inherit the
desktop preference.
"""
from __future__ import annotations

import html as _html

from .. import dates

ASSET_SCHEME = "chronicle-asset"
IMAGE_SCHEME = "chronicle-img"

WORDS_PER_MINUTE = 230

FONT_FACES = """
@font-face {
  font-family: 'Source Serif 4';
  font-style: normal; font-weight: 200 900; font-display: block;
  src: url('%(a)s://fonts/SourceSerif4-roman-latin.woff2') format('woff2');
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA,
                 U+02DC, U+2000-206F, U+2074, U+20AC, U+2122, U+2191, U+2193,
                 U+2212, U+2215, U+FEFF, U+FFFD;
}
@font-face {
  font-family: 'Source Serif 4';
  font-style: normal; font-weight: 200 900; font-display: block;
  src: url('%(a)s://fonts/SourceSerif4-roman-latinext.woff2') format('woff2');
  unicode-range: U+0100-02AF, U+0304, U+0308, U+0329, U+1E00-1E9F, U+1EF2-1EFF,
                 U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF;
}
@font-face {
  font-family: 'Source Serif 4';
  font-style: italic; font-weight: 200 900; font-display: block;
  src: url('%(a)s://fonts/SourceSerif4-italic-latin.woff2') format('woff2');
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA,
                 U+02DC, U+2000-206F, U+2074, U+20AC, U+2122, U+2191, U+2193,
                 U+2212, U+2215, U+FEFF, U+FFFD;
}
@font-face {
  font-family: 'Source Serif 4';
  font-style: italic; font-weight: 200 900; font-display: block;
  src: url('%(a)s://fonts/SourceSerif4-italic-latinext.woff2') format('woff2');
  unicode-range: U+0100-02AF, U+0304, U+0308, U+0329, U+1E00-1E9F, U+1EF2-1EFF,
                 U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF;
}
""" % {"a": ASSET_SCHEME}

READER_CSS = FONT_FACES + """
:root {
  --ink:        #1c1b19;
  --ink-soft:   #55524d;
  --ink-faint:  #8a857e;
  --paper:      #fdfcfa;
  --rule:       #e3ded6;
  --accent:     #7a4b2a;
  --quote-bar:  #d8d0c4;
  --code-bg:    #f4f1ec;
  --link-rule:  rgba(122, 75, 42, 0.40);
  --notice-bg:  #f7f3ec;
  --select:     #e8dcc8;
  --img-fade:   none;
  --measure:    34rem;
  --serif: 'Source Serif 4', 'Noto Serif', 'Liberation Serif', Georgia, serif;
  --sans:  'Adwaita Sans', Cantarell, 'Noto Sans', system-ui, sans-serif;
  --mono:  'Source Code Pro', 'Adwaita Mono', 'DejaVu Sans Mono', monospace;
}

html.dark {
  --ink:        #ddd6ca;
  --ink-soft:   #a49c90;
  --ink-faint:  #7d766c;
  --paper:      #191817;
  --rule:       #35322d;
  --accent:     #c9a077;
  --quote-bar:  #45403a;
  --code-bg:    #221f1d;
  --link-rule:  rgba(201, 160, 119, 0.45);
  --notice-bg:  #232019;
  --select:     #4a3f2c;
  /* Take the glare off pure-white diagrams and screenshots. */
  --img-fade:   brightness(0.88) contrast(1.02);
}

* { box-sizing: border-box; }

html {
  font-size: 20px;
  -webkit-text-size-adjust: 100%;
}

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--serif);
  font-size: 1rem;
  line-height: 1.62;
  font-kerning: normal;
  font-variant-numeric: oldstyle-num proportional-nums;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}

.page {
  max-width: var(--measure);
  margin: 0 auto;
  padding: 4.5rem 1.6rem 8rem;
}

/* ---- masthead ---------------------------------------------------------- */

.masthead { margin-bottom: 3rem; }

.masthead .kicker {
  font-family: var(--sans);
  font-size: 0.62rem;
  font-weight: 600;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--accent);
  margin: 0 0 0.9rem;
}

.masthead h1 {
  font-size: 2.15rem;
  line-height: 1.16;
  font-weight: 600;
  letter-spacing: -0.012em;
  margin: 0 0 1rem;
  text-wrap: balance;
}

.masthead .byline {
  font-family: var(--sans);
  font-size: 0.68rem;
  color: var(--ink-faint);
  margin: 0;
  letter-spacing: 0.01em;
}

.masthead .byline .sep { margin: 0 0.5em; opacity: 0.5; }

.masthead .uncertain {
  border-bottom: 1px dotted var(--ink-faint);
  cursor: help;
}

.masthead::after {
  content: "";
  display: block;
  width: 3.5rem;
  height: 2px;
  background: var(--rule);
  margin-top: 2.4rem;
}

/* ---- prose ------------------------------------------------------------- */

.prose > *:first-child { margin-top: 0; }

p {
  margin: 0 0 1.35em;
  /* Ragged-right at a 60-65 character measure reads better unhyphenated;
     break only where a word would otherwise overflow the column. */
  overflow-wrap: break-word;
}

h2, h3, h4, h5, h6 {
  font-weight: 600;
  line-height: 1.25;
  margin: 2.6em 0 0.75em;
  letter-spacing: -0.006em;
  text-wrap: balance;
}
h2 { font-size: 1.42rem; }
h3 { font-size: 1.17rem; }
h4 { font-size: 1.02rem; }
h5, h6 { font-size: 0.94rem; color: var(--ink-soft); }

a {
  color: inherit;
  text-decoration: underline;
  text-decoration-color: var(--link-rule);
  text-underline-offset: 0.16em;
  text-decoration-thickness: 0.055em;
}
a:hover { text-decoration-color: var(--accent); }

strong, b { font-weight: 600; }
em, i { font-style: italic; }
small { font-size: 0.84em; color: var(--ink-soft); }
sup, sub { font-size: 0.68em; line-height: 0; }

ul, ol { margin: 0 0 1.35em; padding-left: 1.5em; }
li { margin-bottom: 0.42em; }
li > ul, li > ol { margin: 0.42em 0 0.2em; }

dl { margin: 0 0 1.35em; }
dt { font-weight: 600; margin-top: 0.9em; }
dd { margin: 0.2em 0 0 1.4em; color: var(--ink-soft); }

blockquote {
  margin: 1.9em 0;
  padding: 0.1em 0 0.1em 1.4em;
  border-left: 2px solid var(--quote-bar);
  color: var(--ink-soft);
  font-style: italic;
}
blockquote p:last-child { margin-bottom: 0; }
blockquote cite { font-style: normal; font-size: 0.86em; color: var(--ink-faint); }

hr {
  border: 0;
  height: 1px;
  background: var(--rule);
  margin: 3em auto;
  width: 40%;
}

/* ---- code -------------------------------------------------------------- */

code, kbd, samp, var {
  font-family: var(--mono);
  font-size: 0.83em;
  font-style: normal;
  background: var(--code-bg);
  padding: 0.12em 0.34em;
  border-radius: 3px;
}

pre {
  font-family: var(--mono);
  font-size: 0.78rem;
  line-height: 1.5;
  background: var(--code-bg);
  border: 1px solid var(--rule);
  border-radius: 5px;
  padding: 1em 1.15em;
  margin: 1.8em 0;
  overflow-x: auto;
  white-space: pre;
  -webkit-hyphens: none;
  hyphens: none;
}
pre code { background: none; padding: 0; font-size: inherit; }

/* ---- figures and images ------------------------------------------------ */

img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 1.9em auto;
  border-radius: 3px;
  filter: var(--img-fade);
}

figure {
  margin: 2.2em 0;
}
figure img { margin: 0 auto 0.75em; }

figcaption {
  font-family: var(--sans);
  font-size: 0.66rem;
  line-height: 1.5;
  color: var(--ink-faint);
  text-align: center;
  margin: 0 auto;
  max-width: 90%;
}

/* An image we could not cache: show the gap honestly rather than a broken icon. */
img[data-remote] { outline: none; }
.image-missing {
  font-family: var(--sans);
  font-size: 0.66rem;
  color: var(--ink-faint);
  text-align: center;
  border: 1px dashed var(--rule);
  border-radius: 4px;
  padding: 1.4em;
  margin: 1.9em 0;
}

/* ---- tables ------------------------------------------------------------ */

.table-scroll { overflow-x: auto; margin: 1.9em 0; }
table {
  border-collapse: collapse;
  font-size: 0.82rem;
  font-family: var(--sans);
  width: 100%;
}
th, td {
  border-bottom: 1px solid var(--rule);
  padding: 0.5em 0.75em;
  text-align: left;
  vertical-align: top;
}
th { font-weight: 600; border-bottom-width: 2px; }
caption {
  font-size: 0.68rem;
  color: var(--ink-faint);
  text-align: left;
  padding-bottom: 0.6em;
}

/* ---- end matter -------------------------------------------------------- */

.endmark {
  margin: 4rem 0 0;
  text-align: center;
  color: var(--ink-faint);
  font-size: 0.8rem;
  letter-spacing: 0.4em;
}

.provenance {
  margin-top: 2.5rem;
  padding-top: 1.4rem;
  border-top: 1px solid var(--rule);
  font-family: var(--sans);
  font-size: 0.63rem;
  line-height: 1.7;
  color: var(--ink-faint);
}
.provenance a { text-decoration-color: var(--link-rule); }

.notice {
  font-family: var(--sans);
  font-size: 0.7rem;
  line-height: 1.6;
  color: var(--ink-soft);
  background: var(--notice-bg);
  border: 1px solid var(--rule);
  border-radius: 5px;
  padding: 0.9em 1.1em;
  margin: 0 0 2rem;
}

::selection { background: var(--select); }

@media (max-width: 640px) {
  html { font-size: 18px; }
  .page { padding: 3rem 1.2rem 6rem; }
  .masthead h1 { font-size: 1.75rem; }
}
"""

DOCUMENT = """<!DOCTYPE html>
<html lang="en" class="{theme}"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head><body>
<article class="page">
<header class="masthead">
  <p class="kicker">{source}</p>
  <h1>{title}</h1>
  <p class="byline">{byline}</p>
</header>
{notice}
<div class="prose">{content}</div>
<p class="endmark">* * *</p>
<footer class="provenance">{provenance}</footer>
</article>
<script>{script}</script>
</body></html>"""

SCRIPT = """
(function () {
  // Wide tables get their own scroll container so the page never scrolls sideways.
  document.querySelectorAll('table').forEach(function (t) {
    if (t.closest('.table-scroll')) return;
    var w = document.createElement('div');
    w.className = 'table-scroll';
    t.parentNode.insertBefore(w, t);
    w.appendChild(t);
  });
  // Replace images that failed to load with an honest placeholder.
  document.querySelectorAll('img').forEach(function (img) {
    img.addEventListener('error', function () {
      var d = document.createElement('div');
      d.className = 'image-missing';
      d.textContent = img.alt ? ('Image unavailable — ' + img.alt) : 'Image unavailable';
      if (img.parentNode) img.parentNode.replaceChild(d, img);
    });
  });
  // Report scroll position so reading progress survives closing the app.
  var ticking = false;
  window.addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      ticking = false;
      var max = document.body.scrollHeight - window.innerHeight;
      var frac = max > 0 ? window.scrollY / max : 0;
      if (window.webkit && window.webkit.messageHandlers &&
          window.webkit.messageHandlers.chronicle) {
        window.webkit.messageHandlers.chronicle.postMessage(
          JSON.stringify({ type: 'scroll', value: frac }));
      }
    });
  });
  window.chronicleScrollTo = function (frac) {
    var max = document.body.scrollHeight - window.innerHeight;
    if (max > 0 && frac > 0) window.scrollTo(0, max * frac);
  };
})();
"""


def reading_minutes(words: int) -> int:
    return max(1, round((words or 0) / WORDS_PER_MINUTE))


def _esc(text) -> str:
    return _html.escape(str(text or ""), quote=True)


def build_byline(article) -> str:
    """Date, reading time and — when the date is not certain — why."""
    precision = article["date_precision"]
    confidence = article["date_confidence"]
    label = dates.format_display(article["published_at"], precision, confidence)
    note = dates.CONFIDENCE_NOTE.get(confidence, "")

    if confidence in ("exact", "high"):
        date_html = f'<time>{_esc(label)}</time>'
    else:
        date_html = (f'<time class="uncertain" title="{_esc(note)}">'
                     f'{_esc(label)}</time>')

    parts = [date_html]
    if article["word_count"]:
        parts.append(f"{reading_minutes(article['word_count'])} min read")
    return '<span class="sep">·</span>'.join(parts)


def build_notice(article) -> str:
    status = article["content_status"]
    if status == "paywalled":
        return ('<p class="notice">Only the public part of this article is '
                'available — the rest is behind the publisher\'s paywall.</p>')
    if status == "partial":
        return ('<p class="notice">This article was recovered from a partial '
                'source and may be incomplete.</p>')
    return ""


_SOURCE_LABEL = {
    "api": "publisher API", "feed": "publisher feed", "direct": "the original page",
    "wayback": "the Internet Archive", "browser": "the original page",
}


def build_provenance(article) -> str:
    bits = []
    url = article["url"]
    if url:
        bits.append(f'Original: <a href="{_esc(url)}">{_esc(url)}</a>')

    origin = _SOURCE_LABEL.get(article["content_source"], article["content_source"])
    if origin:
        bits.append(f"Retrieved from {_esc(origin)}")

    conf = article["date_confidence"]
    src = article["date_source"]
    note = dates.CONFIDENCE_NOTE.get(conf, "")
    if note:
        bits.append(f"{_esc(note)}{f' ({_esc(src)})' if src else ''}")
    return "<br>".join(bits)


def build_document(article, dark: bool = False) -> str:
    """Assemble the full reader page for one article."""
    return DOCUMENT.format(
        theme="dark" if dark else "light",
        title=_esc(article["title"]),
        source=_esc(article["source_name"]),
        byline=build_byline(article),
        notice=build_notice(article),
        content=article["content_html"] or
                '<p class="notice">This article has no stored content yet.</p>',
        provenance=build_provenance(article),
        css=READER_CSS,
        script=SCRIPT,
    )


PLACEHOLDER = """<!DOCTYPE html><html class="%s"><head><meta charset="utf-8"><style>%s
.empty { max-width: 26rem; margin: 22vh auto; text-align: center;
         font-family: var(--sans); color: var(--ink-soft); }
.empty h1 { font-family: var(--serif); font-size: 1.5rem; font-weight: 600;
            margin: 0 0 0.7rem; color: var(--ink); }
.empty p { font-size: 0.72rem; line-height: 1.7; margin: 0; }
</style></head><body><div class="empty"><h1>%s</h1><p>%s</p></div></body></html>"""


def placeholder(heading: str, body: str, dark: bool = False) -> str:
    return PLACEHOLDER % ("dark" if dark else "light", READER_CSS,
                          _esc(heading), body)


# Painted behind the page so a theme switch never flashes the wrong ground.
BACKGROUND = {"light": "#fdfcfa", "dark": "#191817"}
