"""Word lookup for the reader: a definition without leaving the article.

Definitions come from Wiktionary, through the REST endpoint the Wikimedia
Foundation runs for it. Every answer is written into the library — including
"there is no such word", which is a real answer and not a failure — so a word
looked up once stays available with the network off, the way the articles
themselves do. Only a genuine network failure goes uncached, since that says
nothing about the word.

Wiktionary rather than one of the friendlier dictionary APIs because those are
hobby services in front of this same data, and go down: the one this was first
written against answered 522 for twenty seconds at a stretch. A reader waiting
on a word cannot afford that, and Wikimedia's own endpoint is the thing the
others are wrapping anyway.

Deliberately not routed through `net`: that module carries a cancel flag and a
retry-and-disk-cache policy built for archive builds, where a stalled fetch
costs nothing. A reader waiting on a word wants one quick attempt or an honest
shrug.
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, db

log = logging.getLogger("chronicle.dictionary")

ENDPOINT = "https://en.wiktionary.org/api/rest_v1/page/definition/{word}"
SOURCE = "Wiktionary"
TIMEOUT = 8.0

# Wikimedia asks that clients identify themselves and say how to be reached.
USER_AGENT = (f"Chronicle/{__version__} (https://github.com/MVinhas/chronicle; "
              "personal blog reader)")

# How much of an entry the card shows. Wiktionary will happily return two dozen
# senses; the reader wants the handful that answer the question.
MAX_SENSES = 4
MAX_PER_MEANING = 2

# What counts as a word worth looking up. Letters, with the punctuation that
# lives *inside* English words (Wiktionary has an entry for "ne'er-do-well").
_WORD = re.compile(r"^[a-z][a-z'\-]{0,40}$")
_TRIM = re.compile(r"^[^\w]+|[^\w]+$")

# Wiktionary's definitions arrive as fragments of the rendered page: wikilinks
# around half the words, and an empty span carrying the sense's usage label.
_TAGS = re.compile(r"<[^>]+>")


def normalise(text: str) -> str | None:
    """The dictionary headword for a selection, or None if there isn't one.

    A selection is only a lookup when it is a single word: "quixotic" has an
    entry, "the most quixotic of" does not, and offering to define a sentence
    would be offering something that always fails.
    """
    word = _TRIM.sub("", (text or "").strip().replace("’", "'"))
    if not word or " " in word or "\n" in word:
        return None
    word = word.casefold()
    # Possessives are not headwords; the word underneath one is.
    if word.endswith("'s"):
        word = word[:-2]
    return word if _WORD.match(word) else None


def cached(conn, word: str) -> dict | None:
    """A previously stored answer for this word, or None if never looked up."""
    payload = db.get_definition(conn, word)
    if payload is None:
        return None
    try:
        entry = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    return entry if isinstance(entry, dict) else None


def remember(conn, word: str, entry: dict) -> None:
    """Keep an answer, so the word is defined again with the network off."""
    db.set_definition(conn, word, json.dumps(entry))


# -- the network ------------------------------------------------------------

def fetch(word: str) -> dict:
    """Ask Wiktionary. Raises OSError when it cannot be reached."""
    url = ENDPOINT.format(word=urllib.parse.quote(word, safe=""))
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(600_000)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Not a failure: Wiktionary looked, and there is no such page.
            return {"word": word, "senses": [], "source": SOURCE}
        raise OSError(f"the dictionary returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OSError(str(exc)) from exc
    try:
        return parse(word, json.loads(raw.decode("utf-8", "replace")))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise OSError("the dictionary sent something unreadable") from exc


def parse(word: str, payload) -> dict:
    """Fold Wiktionary's English entry into one card's worth.

    The response is keyed by language — a word spelled the same in Italian and
    Latin brings those back too — and only English is wanted here.

    No pronunciation: this endpoint does not carry IPA, and digging it out of
    the rendered entry would cost a second request per word for something the
    card can do without. `phonetic` is kept in the entry, empty, because it
    belongs to the shape of a dictionary entry and a later source may fill it.
    """
    meanings = payload.get("en") if isinstance(payload, dict) else None
    senses: list[dict] = []
    seen: set[str] = set()

    for meaning in meanings or []:
        if not isinstance(meaning, dict):
            continue
        pos = _clean(meaning.get("partOfSpeech")).casefold()
        taken = 0
        for definition in meaning.get("definitions") or []:
            if not isinstance(definition, dict):
                continue
            body = _clean(definition.get("definition"))
            if not body or body.casefold() in seen:
                continue
            seen.add(body.casefold())
            senses.append({"pos": pos, "definition": body,
                           "example": _example(definition)})
            taken += 1
            if taken >= MAX_PER_MEANING or len(senses) >= MAX_SENSES:
                break
        if len(senses) >= MAX_SENSES:
            break

    return {"word": word, "phonetic": "", "senses": senses, "source": SOURCE}


def _example(definition: dict) -> str:
    for candidate in definition.get("examples") or []:
        text = _clean(candidate)
        if text:
            return text
    return ""


def _clean(value) -> str:
    """A definition fragment as plain text: no markup, no doubled spaces.

    Tags come out rather than becoming spaces: they are all inline, and the
    words around them already carry whatever spacing they need. Substituting
    a space instead pushes the full stop off the end of the sentence.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(_TAGS.sub("", value)).split())
