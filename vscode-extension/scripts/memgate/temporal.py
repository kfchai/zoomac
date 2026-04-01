"""TemporalExtractor — date parsing, temporal query detection, proximity scoring."""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass


@dataclass
class TemporalRef:
    """A parsed temporal reference."""

    timestamp: float | None  # Unix timestamp (midpoint for ranges)
    start_ts: float | None  # Range start
    end_ts: float | None  # Range end
    precision: str  # "exact", "day", "month", "year", "relative"
    raw_text: str  # Original matched text


# Month name -> number mappings
_MONTH_MAP: dict[str, int] = {}
for _i, _name in enumerate(calendar.month_name):
    if _name:
        _MONTH_MAP[_name.lower()] = _i
for _i, _name in enumerate(calendar.month_abbr):
    if _name:
        _MONTH_MAP[_name.lower()] = _i

# LoCoMo format: "1:56 pm on 8 May, 2023" or "1:56 pm on 18 May, 2023"
_LOCOMO_DATE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(am|pm)\s+on\s+(\d{1,2})\s+(\w+),?\s+(\d{4})",
    re.IGNORECASE,
)

# LongMemEval format: "2023/05/20 (Sat) 02:21"
_LONGMEMEVAL_DATE_RE = re.compile(
    r"(\d{4})/(\d{2})/(\d{2})\s*\(\w+\)\s*(\d{2}):(\d{2})",
)

# Session header: "[Session N, <date>]"
_SESSION_HEADER_RE = re.compile(r"\[Session\s+\d+,\s*(.+?)\]")

# Natural date patterns
_DAY_MONTH_YEAR_RE = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_MONTH_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# Relative time patterns
_RELATIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(yesterday)\b", re.I), "yesterday"),
    (re.compile(r"\b(last\s+week)\b", re.I), "last_week"),
    (re.compile(r"\b(last\s+month)\b", re.I), "last_month"),
    (re.compile(r"\b(last\s+year)\b", re.I), "last_year"),
]

# Temporal query keywords
_TEMPORAL_QUERY_RE = re.compile(
    r"\b(when did|what time|what date|how long ago|how recently|since when|at what point)\b",
    re.IGNORECASE,
)


def _make_timestamp(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> float:
    """Create a Unix timestamp from date components using calendar.timegm (UTC)."""
    import time
    try:
        return float(calendar.timegm((year, month, day, hour, minute, 0, 0, 0, -1)))
    except (ValueError, OverflowError):
        return 0.0


def _parse_locomo_date(text: str) -> TemporalRef | None:
    """Parse LoCoMo-style date: '1:56 pm on 8 May, 2023'."""
    m = _LOCOMO_DATE_RE.search(text)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3).lower()
    day = int(m.group(4))
    month_name = m.group(5).lower()
    year = int(m.group(6))

    month = _MONTH_MAP.get(month_name)
    if month is None:
        return None

    # Convert 12-hour to 24-hour
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    ts = _make_timestamp(year, month, day, hour, minute)
    if ts == 0.0:
        return None

    return TemporalRef(
        timestamp=ts,
        start_ts=ts,
        end_ts=ts,
        precision="exact",
        raw_text=m.group(0),
    )


def _parse_longmemeval_date(text: str) -> TemporalRef | None:
    """Parse LongMemEval-style date: '2023/05/20 (Sat) 02:21'."""
    m = _LONGMEMEVAL_DATE_RE.search(text)
    if not m:
        return None

    year = int(m.group(1))
    month = int(m.group(2))
    day = int(m.group(3))
    hour = int(m.group(4))
    minute = int(m.group(5))

    ts = _make_timestamp(year, month, day, hour, minute)
    if ts == 0.0:
        return None

    return TemporalRef(
        timestamp=ts,
        start_ts=ts,
        end_ts=ts,
        precision="exact",
        raw_text=m.group(0),
    )


class TemporalExtractor:
    """Extracts temporal references from text and detects temporal queries."""

    def extract_session_date(self, text: str) -> TemporalRef | None:
        """Parse session date from [Session N, <date>] headers.

        Supports both LoCoMo and LongMemEval date formats.
        """
        m = _SESSION_HEADER_RE.search(text)
        if not m:
            # Try parsing the whole text as a date
            return _parse_locomo_date(text) or _parse_longmemeval_date(text)

        date_str = m.group(1)
        return _parse_locomo_date(date_str) or _parse_longmemeval_date(date_str)

    def extract_from_text(self, text: str, reference_time: float | None = None) -> list[TemporalRef]:
        """Extract all temporal references from text.

        Tries session header first, then natural date patterns, then relative refs.
        """
        refs: list[TemporalRef] = []

        # Session header (highest priority, most specific)
        session_ref = self.extract_session_date(text)
        if session_ref:
            refs.append(session_ref)

        # ISO dates: 2023-05-08
        for m in _ISO_DATE_RE.finditer(text):
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                ts = _make_timestamp(year, month, day)
                if ts > 0:
                    refs.append(TemporalRef(
                        timestamp=ts,
                        start_ts=_make_timestamp(year, month, day, 0, 0),
                        end_ts=_make_timestamp(year, month, day, 23, 59),
                        precision="day",
                        raw_text=m.group(0),
                    ))
            except (ValueError, OverflowError):
                pass

        # "8 May, 2023" or "8 May 2023"
        for m in _DAY_MONTH_YEAR_RE.finditer(text):
            day = int(m.group(1))
            month = _MONTH_MAP.get(m.group(2).lower())
            year = int(m.group(3))
            if month and 1 <= day <= 31:
                ts = _make_timestamp(year, month, day)
                if ts > 0:
                    refs.append(TemporalRef(
                        timestamp=ts,
                        start_ts=_make_timestamp(year, month, day, 0, 0),
                        end_ts=_make_timestamp(year, month, day, 23, 59),
                        precision="day",
                        raw_text=m.group(0),
                    ))

        # "May 8, 2023" or "May 8 2023"
        for m in _MONTH_DAY_YEAR_RE.finditer(text):
            month = _MONTH_MAP.get(m.group(1).lower())
            day = int(m.group(2))
            year = int(m.group(3))
            if month and 1 <= day <= 31:
                ts = _make_timestamp(year, month, day)
                if ts > 0:
                    refs.append(TemporalRef(
                        timestamp=ts,
                        start_ts=_make_timestamp(year, month, day, 0, 0),
                        end_ts=_make_timestamp(year, month, day, 23, 59),
                        precision="day",
                        raw_text=m.group(0),
                    ))

        # "May 2023"
        for m in _MONTH_YEAR_RE.finditer(text):
            month = _MONTH_MAP.get(m.group(1).lower())
            year = int(m.group(2))
            if month:
                ts = _make_timestamp(year, month, 15)  # midpoint
                start = _make_timestamp(year, month, 1, 0, 0)
                # End of month
                last_day = calendar.monthrange(year, month)[1]
                end = _make_timestamp(year, month, last_day, 23, 59)
                refs.append(TemporalRef(
                    timestamp=ts,
                    start_ts=start,
                    end_ts=end,
                    precision="month",
                    raw_text=m.group(0),
                ))

        # Relative patterns (only if reference_time provided)
        if reference_time is not None:
            for pattern, label in _RELATIVE_PATTERNS:
                m_rel = pattern.search(text)
                if m_rel:
                    ref = self._resolve_relative(label, reference_time, m_rel.group(0))
                    if ref:
                        refs.append(ref)

        # Deduplicate by raw_text
        seen: set[str] = set()
        unique: list[TemporalRef] = []
        for r in refs:
            if r.raw_text not in seen:
                seen.add(r.raw_text)
                unique.append(r)

        return unique

    def detect_temporal_query(self, query: str) -> tuple[bool, TemporalRef | None]:
        """Detect if a query is temporal and extract any date reference.

        Returns (is_temporal, temporal_ref_or_none).
        """
        is_temporal = bool(_TEMPORAL_QUERY_RE.search(query))

        # Extract any date from the query
        refs = self.extract_from_text(query)
        ref = refs[0] if refs else None

        # Also check for year-only matches in query context
        if ref is None and is_temporal:
            for m in _YEAR_RE.finditer(query):
                year = int(m.group(1))
                if 1900 <= year <= 2100:
                    ts = _make_timestamp(year, 6, 15)  # midpoint of year
                    ref = TemporalRef(
                        timestamp=ts,
                        start_ts=_make_timestamp(year, 1, 1, 0, 0),
                        end_ts=_make_timestamp(year, 12, 31, 23, 59),
                        precision="year",
                        raw_text=m.group(0),
                    )
                    break

        return is_temporal, ref

    @staticmethod
    def temporal_proximity(ts1: float, ts2: float, precision: str = "day") -> float:
        """Gaussian proximity score between two timestamps.

        Width varies by precision: tighter for day-level, wider for year-level.
        Returns value in [0, 1].
        """
        if ts1 == 0 or ts2 == 0:
            return 0.0

        width_map = {
            "exact": 3600.0,  # 1 hour
            "day": 86400.0,  # 1 day
            "month": 30.0 * 86400,  # 30 days
            "year": 365.0 * 86400,  # 365 days
            "relative": 7.0 * 86400,  # 1 week
        }
        width = width_map.get(precision, 86400.0)

        delta = abs(ts1 - ts2)
        return math.exp(-0.5 * (delta / width) ** 2)

    @staticmethod
    def _resolve_relative(label: str, reference_time: float, raw_text: str) -> TemporalRef | None:
        """Resolve a relative time reference against a reference timestamp."""
        offsets = {
            "yesterday": 86400,
            "last_week": 7 * 86400,
            "last_month": 30 * 86400,
            "last_year": 365 * 86400,
        }
        offset = offsets.get(label)
        if offset is None:
            return None

        ts = reference_time - offset
        return TemporalRef(
            timestamp=ts,
            start_ts=ts - offset,
            end_ts=reference_time,
            precision="relative",
            raw_text=raw_text,
        )
