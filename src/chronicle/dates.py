"""Publication-date parsing with explicit precision and confidence.

Core rule: a date is only ever recorded together with where it came from.
We never fall back to "now" — an article with no determinable date keeps
published_at = NULL and is shown in a separate 'undated' bucket rather than
being silently dropped into the timeline at the wrong point.

precision  : day | month | year | unknown   -- how much of the date is real
confidence : exact | high | medium | inferred | unknown
    exact    structured metadata from the publisher (API field, dc.date.issued)
    high     unambiguous machine-readable signal (date embedded in permalink)
    medium   parsed from a conventional position in the page (dateline)
    inferred derived from neighbours / ordering; genuinely uncertain
    unknown  no date could be determined
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# Hard-coded rather than calendar.month_name: that follows the process locale,
# which would render an English-language archive's dates in the user's locale
# and, worse, fail to parse English datelines on a non-English system.
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

MONTHS = {m.lower(): i for i, m in enumerate(MONTH_NAMES) if m}
MONTHS.update({m.lower()[:3]: i for i, m in enumerate(MONTH_NAMES) if m})
MONTHS["sept"] = 9
MONTH_RE = ("january|february|march|april|may|june|july|august|september|october|"
            "november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")

PRECISION_DAY, PRECISION_MONTH, PRECISION_YEAR, PRECISION_UNKNOWN = (
    "day", "month", "year", "unknown")


class PubDate(NamedTuple):
    """A publication date plus its provenance. `iso` is the START of the window."""
    iso: str | None
    precision: str
    confidence: str
    source: str

    @property
    def known(self) -> bool:
        return self.iso is not None

    def as_fields(self) -> dict:
        return {"published_at": self.iso, "date_precision": self.precision,
                "date_confidence": self.confidence, "date_source": self.source}

    def window_end(self) -> datetime | None:
        """Latest instant this date could actually refer to."""
        if not self.iso:
            return None
        d = datetime.fromisoformat(self.iso)
        if self.precision == PRECISION_DAY:
            return d + timedelta(days=1) - timedelta(seconds=1)
        if self.precision == PRECISION_MONTH:
            last = calendar.monthrange(d.year, d.month)[1]
            return d.replace(day=last, hour=23, minute=59, second=59)
        if self.precision == PRECISION_YEAR:
            return d.replace(month=12, day=31, hour=23, minute=59, second=59)
        return d


UNKNOWN = PubDate(None, PRECISION_UNKNOWN, "unknown", "")


def _mk(year: int, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0,
        second: int = 0, *, precision: str, confidence: str, source: str) -> PubDate:
    try:
        dt = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return UNKNOWN
    # Guard against nonsense: pre-web or far-future dates are not publication dates.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if dt.year < 1980 or dt > now + timedelta(days=2):
        return UNKNOWN
    return PubDate(dt.replace(microsecond=0).isoformat(), precision, confidence, source)


# --------------------------------------------------------------------------
# structured formats
# --------------------------------------------------------------------------

_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?)?")


def parse_iso(text: str, *, confidence: str = "exact", source: str = "") -> PubDate:
    """ISO-8601 / RFC-3339, with timezone normalised to UTC."""
    if not text:
        return UNKNOWN
    m = _ISO_RE.search(text.strip())
    if not m:
        return parse_freeform(text, confidence=confidence, source=source)
    y, mo, d, hh, mm, ss, tz = m.groups()
    has_time = hh is not None
    try:
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return UNKNOWN
    if tz and tz not in ("Z", "z") and has_time:
        sign = 1 if tz[0] == "+" else -1
        tz = tz[1:].replace(":", "")
        dt -= sign * timedelta(hours=int(tz[:2]), minutes=int(tz[2:4] or 0))
    return _mk(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
               precision=PRECISION_DAY, confidence=confidence, source=source)


_RFC822_RE = re.compile(
    rf"(\d{{1,2}})\s+({MONTH_RE})\w*\s+(\d{{4}})"
    r"(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?"
    r"(?:\s*([+-]\d{4}|GMT|UTC))?", re.I)


def parse_rfc822(text: str, *, confidence: str = "exact", source: str = "") -> PubDate:
    """RSS pubDate, e.g. 'Thu, 16 Apr 2026 17:25:38 +0000'."""
    if not text:
        return UNKNOWN
    m = _RFC822_RE.search(text)
    if not m:
        return parse_iso(text, confidence=confidence, source=source)
    d, mon, y, hh, mm, ss, tz = m.groups()
    month = MONTHS.get(mon.lower()[:3] if len(mon) > 3 else mon.lower())
    if not month:
        return UNKNOWN
    try:
        dt = datetime(int(y), month, int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return UNKNOWN
    if tz and tz[0] in "+-":
        sign = 1 if tz[0] == "+" else -1
        dt -= sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
    return _mk(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
               precision=PRECISION_DAY, confidence=confidence, source=source)


_MONTH_YEAR_RE = re.compile(rf"\b({MONTH_RE})\s+(\d{{4}})\b", re.I)
_DAY_MONTH_YEAR_RE = re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE})\w*,?\s+(\d{{4}})\b", re.I)
_MONTH_DAY_YEAR_RE = re.compile(rf"\b({MONTH_RE})\w*\s+(\d{{1,2}})(?:st|nd|rd|th)?,\s*(\d{{4}})\b", re.I)


def parse_freeform(text: str, *, confidence: str = "medium", source: str = "") -> PubDate:
    """Human-written datelines. Tries most-precise patterns first."""
    if not text:
        return UNKNOWN
    m = _MONTH_DAY_YEAR_RE.search(text)
    if m:
        mon, d, y = m.groups()
        month = MONTHS.get(mon.lower()) or MONTHS.get(mon.lower()[:3])
        if month:
            return _mk(int(y), month, int(d), precision=PRECISION_DAY,
                       confidence=confidence, source=source)
    m = _DAY_MONTH_YEAR_RE.search(text)
    if m:
        d, mon, y = m.groups()
        month = MONTHS.get(mon.lower()) or MONTHS.get(mon.lower()[:3])
        if month:
            return _mk(int(y), month, int(d), precision=PRECISION_DAY,
                       confidence=confidence, source=source)
    m = _MONTH_YEAR_RE.search(text)
    if m:
        mon, y = m.groups()
        month = MONTHS.get(mon.lower()) or MONTHS.get(mon.lower()[:3])
        if month:
            return _mk(int(y), month, 1, precision=PRECISION_MONTH,
                       confidence=confidence, source=source)
    m = re.search(r"\b(19[89]\d|20[0-4]\d)\b", text)
    if m:
        return _mk(int(m.group(1)), 1, 1, precision=PRECISION_YEAR,
                   confidence="inferred", source=source or "text:year-only")
    return UNKNOWN


_URL_DATE_RE = re.compile(r"/(\d{4})/(\d{1,2})(?:/(\d{1,2}))?(?:/|$)")


def parse_from_url(url: str, *, confidence: str = "high") -> PubDate:
    """Dates embedded in permalinks, e.g. /2011/04/06/slug/."""
    m = _URL_DATE_RE.search(url)
    if not m:
        return UNKNOWN
    y, mo, d = m.groups()
    if not (1 <= int(mo) <= 12):
        return UNKNOWN
    if d:
        return _mk(int(y), int(mo), int(d), precision=PRECISION_DAY,
                   confidence=confidence, source="url:permalink")
    return _mk(int(y), int(mo), 1, precision=PRECISION_MONTH,
               confidence=confidence, source="url:permalink")


def interpolate(older: str | None, newer: str | None, source: str) -> PubDate:
    """Bracket an undated item between two dated neighbours.

    Used only where an index page gives a reliable *relative* order but the
    item itself carries no date. Always flagged 'inferred'.
    """
    if older and newer:
        a, b = datetime.fromisoformat(older), datetime.fromisoformat(newer)
        if a > b:
            a, b = b, a
        mid = a + (b - a) / 2
        return PubDate(mid.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                       PRECISION_MONTH, "inferred", source)
    anchor = older or newer
    if anchor:
        return PubDate(anchor, PRECISION_YEAR, "inferred", source)
    return UNKNOWN


# --------------------------------------------------------------------------
# display
# --------------------------------------------------------------------------

def format_display(iso: str | None, precision: str, confidence: str = "exact") -> str:
    if not iso:
        return "Date unknown"
    d = datetime.fromisoformat(iso)
    if precision == PRECISION_YEAR:
        s = str(d.year)
    elif precision == PRECISION_MONTH:
        s = f"{MONTH_NAMES[d.month]} {d.year}"
    else:
        s = f"{d.day} {MONTH_NAMES[d.month]} {d.year}"
    if confidence == "inferred":
        s = "circa " + s
    return s


def format_short(iso: str | None, precision: str) -> str:
    if not iso:
        return "—"
    d = datetime.fromisoformat(iso)
    if precision == PRECISION_YEAR:
        return str(d.year)
    if precision == PRECISION_MONTH:
        return f"{d.year}-{d.month:02d}"
    return f"{d.year}-{d.month:02d}-{d.day:02d}"


CONFIDENCE_NOTE = {
    "exact": "Publisher-supplied date",
    "high": "Date from the article's permalink",
    "medium": "Date read from the article's dateline",
    "inferred": "Estimated from surrounding articles — uncertain",
    "unknown": "No publication date could be determined",
}
