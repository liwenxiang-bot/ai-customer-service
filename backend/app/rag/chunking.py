"""Text chunking with overlap.

Splits on paragraph / sentence boundaries where possible, then packs into
~chunk_size-character windows. Overlap between adjacent chunks is carried as whole
sentence units — never a mid-sentence byte slice — so the carried tail stays readable
and doesn't pollute the next chunk's embedding with a truncated half-thought.
Tail fragments (a chunk that is mostly or entirely overlap, or a tiny hard-split
remainder) are folded back into their neighbour instead of being emitted: a
20-character chunk carries almost no retrievable meaning but still occupies a
candidate slot. Works for mixed Chinese/English text.
"""

from __future__ import annotations

import re

_SENT_SPLIT = re.compile(r"(?<=[。！？；!?\n])|(?<=[.;])\s")


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


def _hard_split(unit: str, chunk_size: int, overlap: int, min_tail: int) -> list[str]:
    """Length-split a single oversized unit (no sentence boundaries to respect).

    The stride re-covers `overlap` chars, so a final slice no longer than the overlap
    is pure duplicate and is dropped; a short-but-novel tail is folded into the
    previous slice rather than emitted as a fragment chunk."""
    step = max(chunk_size - overlap, 1)
    slices = [unit[i : i + chunk_size] for i in range(0, len(unit), step)]
    if len(slices) > 1 and len(slices[-1]) <= overlap:
        slices.pop()
    elif len(slices) > 1 and len(slices[-1]) < min_tail:
        tail = slices.pop()
        slices[-1] += tail[overlap:]
    return slices


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    units = _split_units(text)
    if not units:
        return []

    min_tail = max(overlap, chunk_size // 6)

    chunks: list[str] = []
    buf: list[str] = []  # units in the chunk being packed
    carried = 0  # leading units of `buf` that are overlap carried from the previous chunk

    def emit_and_carry() -> None:
        nonlocal buf, carried
        chunks.append(" ".join(buf))
        # Carry whole trailing units within the overlap budget. If even the last unit
        # alone exceeds it, carry nothing — overlap is best-effort at sentence
        # granularity, not worth re-introducing a mid-sentence slice.
        tail: list[str] = []
        budget = overlap
        for u in reversed(buf):
            if len(u) > budget:
                break
            tail.insert(0, u)
            budget -= len(u) + 1
        buf = tail
        carried = len(tail)

    for unit in units:
        if len(unit) > chunk_size:
            if len(buf) > carried:
                emit_and_carry()
            buf, carried = [], 0
            chunks.extend(_hard_split(unit, chunk_size, overlap, min_tail))
            continue

        packed = sum(len(u) + 1 for u in buf)
        if buf and packed + len(unit) > chunk_size and len(buf) > carried:
            emit_and_carry()
        buf.append(unit)

    # Final buffer: nothing to emit if it holds only carried overlap (pure duplicate).
    if len(buf) > carried:
        novel = " ".join(buf[carried:])
        if chunks and len(novel) < min_tail and len(chunks[-1]) + len(novel) + 1 <= chunk_size + min_tail:
            chunks[-1] = f"{chunks[-1]} {novel}"
        else:
            chunks.append(" ".join(buf))

    return [c.strip() for c in chunks if c.strip()]
