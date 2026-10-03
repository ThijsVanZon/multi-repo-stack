"""YY.RELEASE.PATCH lines (not SemVer) and the next-line calendar rule."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_CANONICAL = re.compile(r"(\d{2})\.([1-9]\d*)\.(0|[1-9]\d*)")


class VersionError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Version:
    year: int
    release: int
    patch: int

    def __str__(self) -> str:
        return f"{self.year:02d}.{self.release}.{self.patch}"


def parse(text: str) -> Version:
    match = _CANONICAL.fullmatch(text)
    if not match:
        raise VersionError(f"not a canonical YY.RELEASE.PATCH version: {text!r}")
    return Version(int(match[1]), int(match[2]), int(match[3]))


def is_version_shaped(name: str) -> bool:
    return _CANONICAL.fullmatch(name) is not None


def parse_file(data: bytes) -> Version:
    """Root VERSION bytes: the canonical value and exactly one final newline."""
    if not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise VersionError("VERSION must contain one canonical value followed by a single newline")
    try:
        return parse(data[:-1].decode("ascii"))
    except UnicodeDecodeError:
        raise VersionError("VERSION is not ASCII") from None


def file_bytes(version: Version) -> bytes:
    return f"{version}\n".encode("ascii")


def next_line(current: Version, opened: date) -> Version:
    """Line opened by the release of `current` on UTC date `opened`."""
    if current.patch != 0:
        raise VersionError(f"patch releases are unsupported: {current}")
    if not 2000 <= opened.year <= 2099:
        raise VersionError(f"opening year {opened.year} is outside the two-digit range")
    year = opened.year % 100
    if year < current.year:
        raise VersionError(f"opening date {opened} precedes line {current}; explain the clock before continuing")
    if year == current.year:
        return Version(year, current.release + 1, 0)
    return Version(year, 1, 0)
