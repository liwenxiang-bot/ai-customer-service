"""Text chunking with overlap.

Splits on paragraph / sentence boundaries where possible, then packs into
~chunk_size-character windows with `overlap` carried between adjacent chunks so
context isn't severed mid-thought. Works for mixed Chinese/English text.
"""

from __future__ import annotations

import re

_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])|(?<=[.;])\s")


def _split_units(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    # Prefer paragraph breaks, fall back to sentence-ish boundaries.
    parts: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        for unit in _SENT_SPLIT.split(para):
            if unit and unit.strip():
                parts.append(unit.strip())
    return parts


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    units = _split_units(text)
    if not units:
        return []

    chunks: list[str] = []
    buf = ""
    for unit in units:
        # A single oversized unit gets hard-split.
        if len(unit) > chunk_size:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(unit), chunk_size - overlap):
                chunks.append(unit[i : i + chunk_size])
            continue

        if len(buf) + len(unit) + 1 <= chunk_size:
            buf = f"{buf} {unit}".strip()
        else:
            chunks.append(buf)
            # Carry the tail of the previous chunk as overlap.
            tail = buf[-overlap:] if overlap > 0 else ""
            buf = f"{tail} {unit}".strip()
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]
