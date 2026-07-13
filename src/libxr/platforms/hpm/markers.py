"""Safe marker-block replacement helpers for generated HPM board code."""

from typing import Optional, Tuple

MARKER_BEGIN = "/* HPM Peripheral Config Begin */"
MARKER_END = "/* HPM Peripheral Config End */"


class MarkerError(ValueError):
    """Base class for unsafe or malformed generated marker blocks."""


class MarkerConflictError(MarkerError):
    """Raised when an existing marker region cannot be identified uniquely."""


def detect_newline(source: str) -> str:
    """Return the first newline convention used by a source file."""

    index = source.find("\n")
    if index > 0 and source[index - 1] == "\r":
        return "\r\n"
    return "\n"


def _marker_span(source: str) -> Optional[Tuple[int, int]]:
    begin_count = source.count(MARKER_BEGIN)
    end_count = source.count(MARKER_END)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise MarkerConflictError(
            "expected one HPM Peripheral Config Begin/End marker pair"
        )
    begin = source.index(MARKER_BEGIN)
    end = source.index(MARKER_END)
    if end < begin:
        raise MarkerConflictError(
            "HPM Peripheral Config End marker appears before Begin marker"
        )
    return begin, end + len(MARKER_END)


def has_marker_block(source: str) -> bool:
    """Return whether a unique, ordered marker pair exists."""

    return _marker_span(source) is not None


def _with_newline(content: str, newline: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def marker_block(content: str, newline: str = "\n") -> str:
    """Render one complete marker block using the requested newline."""

    normalized = _with_newline(content, newline).rstrip(" \t\r\n")
    if normalized:
        return newline.join((MARKER_BEGIN, normalized, MARKER_END))
    return newline.join((MARKER_BEGIN, MARKER_END))


def replace_marker_block(source: str, content: str, before_hint: str = "") -> str:
    """Replace, insert, or append the unique HPM generated marker block."""

    newline = detect_newline(source)
    block = marker_block(content, newline)
    span = _marker_span(source)
    if span is not None:
        return source[: span[0]] + block + source[span[1] :]
    if before_hint and before_hint in source:
        return source.replace(before_hint, block + newline * 2 + before_hint, 1)
    prefix = source.rstrip(" \t\r\n")
    if not prefix:
        return block + newline
    return prefix + newline * 2 + block + newline


__all__ = [
    "MARKER_BEGIN",
    "MARKER_END",
    "MarkerConflictError",
    "MarkerError",
    "detect_newline",
    "has_marker_block",
    "marker_block",
    "replace_marker_block",
]
