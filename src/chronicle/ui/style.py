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
import json as _json

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
  --marker:     rgba(233, 196, 106, 0.42);
  --marker-on:  rgba(233, 196, 106, 0.72);
  --marker-rule: #c9a227;
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
  /* A wash rather than a block: dark-theme prose is light on dark, and an
     opaque marker would invert the text it is meant to emphasise. */
  --marker:     rgba(201, 160, 119, 0.26);
  --marker-on:  rgba(201, 160, 119, 0.46);
  --marker-rule: #c9a077;
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

/* ---- highlights and notes ---------------------------------------------- */

/* Painted as a background wash on a plain <mark>, so the text itself keeps
   its own colour and weight and the prose reads exactly as it did before. */
mark.hl {
  background: var(--marker);
  color: inherit;
  border-radius: 2px;
  padding: 0.04em 0.02em;
  cursor: pointer;
  transition: background 120ms ease;
}
mark.hl:hover { background: var(--marker-on); }
mark.hl.has-note {
  box-shadow: inset 0 -0.14em 0 var(--marker-rule);
}

/* The floating control offered when text is selected, and when a highlight
   is clicked. Positioned by script, in document coordinates. */
#hl-pop {
  position: absolute;
  z-index: 40;
  display: none;
  font-family: var(--sans);
  font-size: 0.66rem;
  background: var(--paper);
  border: 1px solid var(--rule);
  border-radius: 6px;
  box-shadow: 0 3px 14px rgba(0, 0, 0, 0.16);
  padding: 3px;
  white-space: nowrap;
}
#hl-pop button {
  font: inherit;
  color: var(--ink);
  background: none;
  border: 0;
  border-radius: 4px;
  padding: 0.42em 0.7em;
  cursor: pointer;
}
#hl-pop button:hover { background: var(--code-bg); }

/* ---- the reader's own note, at the foot of the article ------------------ */

.notes {
  margin: 3.2rem 0 0;
  padding-top: 1.6rem;
  border-top: 1px solid var(--rule);
}

.notes h2 {
  font-family: var(--sans);
  font-size: 0.62rem;
  font-weight: 600;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--accent);
  margin: 0 0 1rem;
}

/* A real textarea rather than a contenteditable: plain text in, plain text
   out, with none of contenteditable's pasted-markup surprises. */
#note-body {
  display: block;
  width: 100%;
  min-height: 5.2rem;
  resize: vertical;
  font-family: var(--serif);
  font-size: 0.86rem;
  line-height: 1.6;
  color: var(--ink);
  background: var(--notice-bg);
  border: 1px solid var(--rule);
  border-radius: 5px;
  padding: 0.85em 1em;
}
#note-body:focus {
  outline: none;
  border-color: var(--marker-rule);
}
#note-body::placeholder { color: var(--ink-faint); }

.notes .status {
  font-family: var(--sans);
  font-size: 0.6rem;
  color: var(--ink-faint);
  margin: 0.5em 0 0;
  min-height: 1em;
}

/* Highlights collected under the note, each linking back into the prose. */
.marks { margin: 1.8rem 0 0; padding: 0; list-style: none; }
.marks li {
  margin: 0 0 0.9em;
  padding-left: 0.9em;
  border-left: 2px solid var(--marker-rule);
}
.marks blockquote {
  margin: 0;
  padding: 0;
  border: 0;
  font-style: normal;
  font-size: 0.84rem;
  color: var(--ink-soft);
  cursor: pointer;
}
.marks .mark-note {
  font-family: var(--sans);
  font-size: 0.64rem;
  color: var(--ink-faint);
  margin: 0.3em 0 0;
}
.marks li.orphan { border-left-style: dotted; opacity: 0.72; }
.marks li.orphan blockquote { cursor: default; }
.marks .orphan-tag {
  font-family: var(--sans);
  font-size: 0.58rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-faint);
  margin: 0.3em 0 0;
}

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
<section class="notes">
  <h2>Your notes</h2>
  <textarea id="note-body" placeholder="Write a note about this article…"
            spellcheck="false">{note}</textarea>
  <p class="status" id="note-status"></p>
  <ul class="marks" id="marks"></ul>
</section>
<footer class="provenance">{provenance}</footer>
</article>
<div id="hl-pop"></div>
<script>window.chronicleHighlights = {highlights};</script>
<script>{script}</script>
</body></html>"""

SCRIPT = r"""
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

  // ---- annotations ------------------------------------------------------
  //
  // Highlights are stored as a quote plus the offset it was last found at,
  // both measured against the article's PLAIN TEXT -- the concatenation of
  // every text node in .prose. Working in that space rather than in the HTML
  // means an anchor survives any change that does not alter the words, and
  // the quote itself means it usually survives changes that do.

  var prose = document.querySelector('.prose');
  var pop = document.getElementById('hl-pop');
  var marksList = document.getElementById('marks');
  var noteBox = document.getElementById('note-body');
  var noteStatus = document.getElementById('note-status');
  if (!prose) return;

  function send(payload) {
    if (window.webkit && window.webkit.messageHandlers &&
        window.webkit.messageHandlers.chronicle) {
      window.webkit.messageHandlers.chronicle.postMessage(
        JSON.stringify(payload));
    }
  }

  // A flat index of the prose's text nodes: each entry records where that
  // node's text begins in the whole-article string. Rebuilt after every
  // change to the DOM, because painting a highlight splits text nodes.
  var nodes = [], text = '';

  function reindex() {
    nodes = [];
    text = '';
    var walker = document.createTreeWalker(prose, NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = walker.nextNode())) {
      // Skip text inside elements that carry no prose of their own.
      if (n.parentNode && /^(SCRIPT|STYLE)$/.test(n.parentNode.nodeName)) continue;
      nodes.push({ node: n, start: text.length });
      text += n.nodeValue;
    }
  }

  // Whitespace in HTML is not what a reader sees: a newline in the source and
  // a space on screen are the same word boundary. Comparisons therefore run
  // over a normalised copy, with a map back to real offsets so a match found
  // in normalised space can still be painted in the real document.
  var norm = '', normMap = [];

  function renormalise() {
    norm = '';
    normMap = [];
    var prevSpace = false;
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (/\s/.test(ch)) {
        if (prevSpace) continue;
        prevSpace = true;
        norm += ' ';
      } else {
        prevSpace = false;
        norm += ch;
      }
      normMap.push(i);
    }
  }

  function normalise(s) {
    return (s || '').replace(/\s+/g, ' ').trim();
  }

  // Locate a stored anchor in the current text. The quote is authoritative;
  // the offset only decides between equally good matches, and the
  // prefix/suffix break the remaining ties. Returns real (non-normalised)
  // start/end offsets, or null when the words are simply not there any more.
  function locate(h) {
    var needle = normalise(h.quote);
    if (!needle) return null;
    var hits = [];
    var from = 0, at;
    while ((at = norm.indexOf(needle, from)) !== -1) {
      hits.push(at);
      from = at + 1;
      if (hits.length > 200) break;   // pathological; the best is already in
    }
    if (!hits.length) return null;

    var best = hits[0], bestScore = -Infinity;
    var wantPrefix = normalise(h.prefix).slice(-32);
    var wantSuffix = normalise(h.suffix).slice(0, 32);
    for (var i = 0; i < hits.length; i++) {
      var pos = hits[i];
      var score = 0;
      if (wantPrefix) {
        var before = norm.slice(Math.max(0, pos - wantPrefix.length), pos);
        if (before === wantPrefix) score += 1000;
      }
      if (wantSuffix) {
        var after = norm.slice(pos + needle.length,
                               pos + needle.length + wantSuffix.length);
        if (after === wantSuffix) score += 1000;
      }
      // Nearness to where it was last seen, as the tie-breaker.
      var realPos = normMap[pos] === undefined ? pos : normMap[pos];
      score -= Math.abs(realPos - (h.start_offset || 0)) / 10000;
      if (score > bestScore) { bestScore = score; best = pos; }
    }
    var startReal = normMap[best];
    var endIdx = best + needle.length - 1;
    var endReal = normMap[endIdx];
    if (startReal === undefined || endReal === undefined) return null;
    return { start: startReal, end: endReal + 1 };
  }

  // Turn a pair of plain-text offsets back into a live DOM Range.
  function rangeFor(start, end) {
    var startNode = null, startOff = 0, endNode = null, endOff = 0;
    for (var i = 0; i < nodes.length; i++) {
      var e = nodes[i];
      var len = e.node.nodeValue.length;
      if (startNode === null && start < e.start + len) {
        startNode = e.node;
        startOff = Math.max(0, start - e.start);
      }
      if (end <= e.start + len) {
        endNode = e.node;
        endOff = Math.max(0, end - e.start);
        break;
      }
    }
    if (!startNode || !endNode) return null;
    var r = document.createRange();
    try {
      r.setStart(startNode, Math.min(startOff, startNode.nodeValue.length));
      r.setEnd(endNode, Math.min(endOff, endNode.nodeValue.length));
    } catch (err) { return null; }
    return r;
  }

  // Wrap a range in <mark> elements. A range spanning several block elements
  // cannot be wrapped in one node without restructuring the document, so it
  // is painted per text node instead -- which also keeps the original markup
  // (links, emphasis, code) intact inside the highlight.
  function paint(range, h) {
    var affected = [];
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i].node;
      if (range.intersectsNode && range.intersectsNode(node)) affected.push(node);
    }
    if (!affected.length) return false;
    var painted = 0;
    for (var j = 0; j < affected.length; j++) {
      var node = affected[j];
      if (!node.parentNode) continue;
      var from = (node === range.startContainer) ? range.startOffset : 0;
      var to = (node === range.endContainer) ? range.endOffset
                                             : node.nodeValue.length;
      if (to <= from) continue;
      var target = node;
      if (to < node.nodeValue.length) target.splitText(to);
      if (from > 0) target = target.splitText(from);
      var mark = document.createElement('mark');
      mark.className = 'hl' + (h.note ? ' has-note' : '');
      mark.dataset.hl = h.id;
      if (h.note) mark.title = h.note;
      target.parentNode.replaceChild(mark, target);
      mark.appendChild(target);
      painted++;
    }
    return painted > 0;
  }

  var highlights = (window.chronicleHighlights || []).slice();

  function render() {
    // Start from clean prose so a re-render never nests marks inside marks.
    document.querySelectorAll('mark.hl').forEach(function (m) {
      var parent = m.parentNode;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize();
    });
    reindex();
    renormalise();

    var resolved = [];
    highlights.forEach(function (h) {
      var found = locate(h);
      var ok = false;
      if (found) {
        var r = rangeFor(found.start, found.end);
        if (r) {
          ok = paint(r, h);
          if (ok) {
            h.resolved = found.start;
            // Painting split text nodes, so every later lookup must run
            // against the new tree.
            reindex();
            renormalise();
          }
        }
      }
      if (!ok) h.resolved = null;
      resolved.push({ id: h.id, offset: h.resolved });
    });

    renderList();
    // Tell the app where each highlight ended up, so an anchor that drifted
    // is corrected in the database and one that vanished is marked orphaned
    // rather than being silently dropped.
    send({ type: 'anchors', anchors: resolved });
  }

  function renderList() {
    if (!marksList) return;
    marksList.innerHTML = '';
    highlights.forEach(function (h) {
      var li = document.createElement('li');
      if (h.resolved === null || h.resolved === undefined) {
        li.className = 'orphan';
      }
      var q = document.createElement('blockquote');
      q.textContent = '“' + h.quote + '”';
      li.appendChild(q);
      if (h.note) {
        var n = document.createElement('p');
        n.className = 'mark-note';
        n.textContent = h.note;
        li.appendChild(n);
      }
      if (li.className === 'orphan') {
        var tag = document.createElement('p');
        tag.className = 'orphan-tag';
        tag.textContent = 'no longer in this article';
        li.appendChild(tag);
      } else {
        q.addEventListener('click', function () {
          var mark = document.querySelector('mark.hl[data-hl="' + h.id + '"]');
          if (mark) mark.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
      }
      marksList.appendChild(li);
    });
  }

  // ---- the popup --------------------------------------------------------

  function hidePop() { pop.style.display = 'none'; pop.innerHTML = ''; }

  function showPop(rect, buttons) {
    pop.innerHTML = '';
    buttons.forEach(function (b) {
      var el = document.createElement('button');
      el.textContent = b.label;
      el.addEventListener('mousedown', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        hidePop();
        b.run();
      });
      pop.appendChild(el);
    });
    pop.style.display = 'block';
    var top = rect.top + window.scrollY - pop.offsetHeight - 8;
    if (top < window.scrollY + 4) top = rect.bottom + window.scrollY + 8;
    var left = rect.left + window.scrollX + (rect.width / 2) - (pop.offsetWidth / 2);
    left = Math.max(6, Math.min(left, document.documentElement.clientWidth -
                                      pop.offsetWidth - 6));
    pop.style.top = top + 'px';
    pop.style.left = left + 'px';
  }

  document.addEventListener('mouseup', function (ev) {
    if (pop.contains(ev.target)) return;
    setTimeout(function () {
      var sel = window.getSelection();
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) { hidePop(); return; }
      var range = sel.getRangeAt(0);
      if (!prose.contains(range.commonAncestorContainer)) { hidePop(); return; }
      var quote = normalise(sel.toString());
      if (quote.length < 2) { hidePop(); return; }

      // Where the selection begins, in plain-text coordinates.
      reindex();
      var start = 0;
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i].node === range.startContainer) {
          start = nodes[i].start + range.startOffset;
          break;
        }
      }
      var raw = text;
      var prefix = raw.slice(Math.max(0, start - 40), start);
      var suffix = raw.slice(start + sel.toString().length,
                             start + sel.toString().length + 40);

      showPop(range.getBoundingClientRect(), [{
        label: 'Highlight',
        run: function () {
          send({ type: 'highlight-add', quote: quote, prefix: prefix,
                 suffix: suffix, start_offset: start });
          sel.removeAllRanges();
        }
      }]);
    }, 0);
  });

  document.addEventListener('mousedown', function (ev) {
    if (pop.contains(ev.target)) return;
    var mark = ev.target.closest && ev.target.closest('mark.hl');
    if (!mark) { hidePop(); return; }
    var id = parseInt(mark.dataset.hl, 10);
    var h = highlights.filter(function (x) { return x.id === id; })[0];
    ev.preventDefault();
    showPop(mark.getBoundingClientRect(), [
      { label: h && h.note ? 'Edit note' : 'Add note',
        run: function () { send({ type: 'highlight-note', id: id }); } },
      { label: 'Remove',
        run: function () { send({ type: 'highlight-remove', id: id }); } }
    ]);
  });

  window.addEventListener('scroll', hidePop, { passive: true });

  // Applied by the app after the database has accepted a change, so what is
  // on screen is always what was actually stored.
  window.chronicleSetHighlights = function (list) {
    highlights = (list || []).slice();
    render();
  };

  // ---- the note ---------------------------------------------------------

  if (noteBox) {
    var saveTimer = null;
    var lastSent = noteBox.value;

    function flushNote() {
      if (noteBox.value === lastSent) return;
      lastSent = noteBox.value;
      send({ type: 'note', body: noteBox.value });
      if (noteStatus) {
        noteStatus.textContent = 'Saved';
        setTimeout(function () { noteStatus.textContent = ''; }, 1600);
      }
    }

    noteBox.addEventListener('input', function () {
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(flushNote, 700);
    });

    // The reader's single-key shortcuts (n, j, f, r…) are *window*
    // accelerators: GTK matches them before the key ever reaches the
    // WebView, so no amount of preventDefault in here can hold on to a
    // plain "n". The app has to stand them down while this field has
    // focus, and the app cannot see that by itself -- from GTK's side the
    // focused widget is the WebView, not the textarea inside it. So the
    // page says so.
    noteBox.addEventListener('focus', function () {
      send({ type: 'editing', value: true });
    });
    // Leaving the field, or the page, must not lose what was typed.
    noteBox.addEventListener('blur', function () {
      flushNote();
      send({ type: 'editing', value: false });
    });
    window.addEventListener('pagehide', flushNote);
    window.chronicleFlushNote = flushNote;

    noteBox.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { noteBox.blur(); }
    });
  }

  render();
})();
"""


# Long enough to see where a link goes, short enough not to push the reader's
# buttons around as the pointer moves.
URL_MAX = 96


def elide_url(uri: str) -> str:
    """A URL trimmed for a one-line status, keeping the part that identifies it.

    The host and the start of the path say where a link goes; a long tail of
    slug and query string does not, so that is what gets dropped.
    """
    if len(uri) <= URL_MAX:
        return uri
    cut = uri[:URL_MAX - 1]
    # Do not leave half of a percent-escape behind ("%E2" cut to "%E"), which
    # would render as mojibake in the tooltip.
    if "%" in cut[-2:]:
        cut = cut[:cut.rfind("%")]
    return cut + "…"


# A note line long enough to recognise the thought, short enough to keep the
# queue scannable. Beyond this the reader is better served by opening it.
NOTE_PREVIEW = 120


def note_line(row) -> str:
    """The one line of the reader's own writing to show under a queue row.

    Their note about the article says the most, so it wins. Failing that,
    something they marked -- a note on a highlight, else the highlighted words
    -- so a marked-up article is never a blank row in the Notes list.
    """
    for value, quoted in ((row["note_body"], False), (row["first_mark"], True)):
        text = " ".join((value or "").split())
        if not text:
            continue
        if len(text) > NOTE_PREVIEW:
            text = text[:NOTE_PREVIEW - 1].rstrip() + "…"
        return f"“{text}”" if quoted else text
    return ""


def reading_minutes(words: int) -> int:
    return max(1, round((words or 0) / WORDS_PER_MINUTE))


# Below this much of an article read, "where you left off" is the top anyway.
STARTED_THRESHOLD = 0.02


def resume_scroll(stored: float | None, remember: bool = True) -> float:
    """Where reopening an article should put the reader.

    The position saved on the way out is the whole point of storing one, so
    it is restored rather than discarded. Opening at the top instead does not
    merely lose the place: the reader then reports that fresh near-zero
    scroll, which is flushed back over the stored value on the way out, so
    each launch erases the position it was meant to restore.
    """
    if not remember or not stored:
        return 0.0
    return max(0.0, min(1.0, stored))


def shows_resume_hint(stored: float | None) -> bool:
    """Whether reopening is worth remarking on in the bottom bar."""
    return bool(stored) and stored > STARTED_THRESHOLD


def time_remaining(total_minutes: int, fraction: float) -> str:
    """The reading time to show in the position line.

    Before you have started, how long the article takes is the useful figure;
    once you are into it, how much is left replaces it, so the bar carries one
    number rather than two competing ones.
    """
    if not total_minutes or fraction <= STARTED_THRESHOLD:
        return f"{total_minutes} min"
    left = round(total_minutes * (1.0 - fraction))
    if left <= 0:
        # Rounding reaches zero before the last screen does; "under a minute"
        # is true there, "0 min left" is not.
        return "under a minute left"
    return f"{left} min left"


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


def highlights_json(rows) -> str:
    """The reader's highlights, as the JSON the page's script expects.

    Only the fields the page actually needs: it re-locates each highlight by
    its own quote, so the stored offset travels as a hint rather than as
    something to be trusted.
    """
    out = []
    for r in rows or []:
        out.append({"id": r["id"], "quote": r["quote"], "prefix": r["prefix"],
                    "suffix": r["suffix"], "start_offset": r["start_offset"],
                    "note": r["note"]})
    # </script> inside a string literal would end the block early.
    return _json.dumps(out).replace("</", "<\\/")


def build_document(article, dark: bool = False, note: str = "",
                   highlights=None) -> str:
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
        note=_esc(note),
        highlights=highlights_json(highlights),
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
