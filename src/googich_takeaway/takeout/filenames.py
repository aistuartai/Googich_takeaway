"""Capture dates encoded in common camera and app filenames.

Used only when a file has no embedded date and no Takeout sidecar.
"""

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FilenameDate:
    """A date parsed from a filename.

    ``value`` is naive. ``is_utc`` says whether it is a UTC time (Pixel names) or local
    wall-clock time. ``has_time`` is False when the name only carries a date.
    """

    value: datetime
    is_utc: bool
    has_time: bool


# Order matters: more specific patterns first.
_PATTERNS: list[tuple[re.Pattern[str], bool, bool]] = [
    # Google Pixel: PXL_20190704_001500123.jpg — UTC.
    (re.compile(r"^PXL_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"), True, True),
    # WhatsApp: IMG-20190704-WA0001.jpg — date only.
    (re.compile(r"^(?:IMG|VID|AUD)-(\d{4})(\d{2})(\d{2})-WA\d+"), False, False),
    # Android cameras: IMG_20190704_101500.jpg, VID_20190704_101500.mp4.
    (re.compile(r"^(?:IMG|VID|MVIMG)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"), False, True),
    # Screenshots: Screenshot_20190704-101500.png, Screenshot_2019-07-04-10-15-00.png.
    (
        re.compile(r"^Screenshot_(\d{4})-?(\d{2})-?(\d{2})[-_](\d{2})-?(\d{2})-?(\d{2})"),
        False,
        True,
    ),
    # Generic: 2019-07-04 10.15.00.jpg, 20190704_101500.jpg.
    (re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})[ _-](\d{2})[.:-]?(\d{2})[.:-]?(\d{2})"), False, True),
]


def parse_filename_date(filename: str) -> FilenameDate | None:
    """Return the date encoded in ``filename``, or None if it has no recognised pattern."""
    for pattern, is_utc, has_time in _PATTERNS:
        match = pattern.match(filename)
        if not match:
            continue
        parts = [int(p) for p in match.groups()]
        if not has_time:
            parts += [12, 0, 0]  # date only: noon keeps the day stable across offsets
        try:
            value = datetime(*parts)  # type: ignore[arg-type]
        except ValueError:
            continue  # digits that are not a real date, e.g. month 13
        return FilenameDate(value=value, is_utc=is_utc, has_time=has_time)
    return None
