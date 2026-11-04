"""Structure-aware Markdown chunking with sentence-level overlap.

Knowledge content is Markdown (editor, imports, converted documents). Chunks are what
gets embedded and cited verbatim, so chunking must keep the document's structure
intact instead of flattening it into one line of prose:

- fenced code blocks and tables are atomic: never cut mid-block (an oversized one is
  split at LINE boundaries, so a long table still breaks between rows);
- list items and other line breaks are preserved — joining units with a space turned
  "- a\n- b" into "- a - b" and broke every Markdown list;
- headings never end a chunk; when a heading arrives into a mostly-full chunk the
  chunk is emitted first so the heading leads the next one, and every chunk carries
  the heading path in effect where it starts (`Chunk.heading`, "父标题 > 子标题"). The
  ingest side joins that path into the embedding/FTS input (not the stored content),
  so a chunk deep in a manual is still found by its section name;
- prose is packed sentence by sentence; overlap between adjacent chunks is carried as
  whole sentences (never a mid-sentence slice) and never across a heading boundary or
  out of an atomic block;
- tail fragments (mostly-overlap or tiny remainders) are folded back into their
  neighbour rather than emitted — a 20-char chunk has no retrievable meaning but
  still takes a candidate slot.

`chunk_text` keeps the historical list[str] contract; `chunk_markdown` adds the
heading path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SENT_SPLIT = re.compile(r"(?<=[。！？；!?])|(?<=[.;])\s")
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


@dataclass
class Chunk:
    text: str
    heading: str = ""  # "A > B" heading path in effect at the chunk's first novel unit


@dataclass
class _Unit:
    text: str
    sep: str = " "  # separator to place BEFORE this unit when it is not first in a chunk
    kind: str = "prose"  # prose | heading | atomic
    heading: str = ""  # heading path in effect for this unit (for prose/atomic)
    level: int = 0  # heading level (kind == heading)


# ----------------------------------------------------------------- tokenising


def _units(text: str) -> list[_Unit]:
    """Markdown → ordered units: headings, atomic blocks (code/table), sentences.

    Separators encode the original layout: "\\n\\n" before a new block, "\\n" before a
    new line inside a block (list items, hard-wrapped prose), " " between sentences on
    one line."""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    lines = text.split("\n")
    out: list[_Unit] = []
    path: dict[int, str] = {}  # level → heading text

    def cur_path() -> str:
        return " > ".join(t for _, t in sorted(path.items()))

    i = 0
    first_in_block = True  # whether the next unit starts a new block (blank line seen)
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            first_in_block = True
            i += 1
            continue
        block_sep = "\n\n" if first_in_block else "\n"

        m = _FENCE.match(line)
        if m:
            fence = m.group(1)
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(fence):
                j += 1
            block = "\n".join(lines[i : min(j + 1, len(lines))])
            out.append(_Unit(block, block_sep, "atomic", cur_path()))
            i = j + 1
            first_in_block = True  # a fence is its own block either way
            continue

        if _TABLE_LINE.match(line):
            j = i
            while j < len(lines) and _TABLE_LINE.match(lines[j]):
                j += 1
            out.append(_Unit("\n".join(lines[i:j]), block_sep, "atomic", cur_path()))
            i = j
            first_in_block = True
            continue

        h = _HEADING.match(line)
        if h:
            level = len(h.group(1))
            path = {lv: t for lv, t in path.items() if lv < level}
            path[level] = h.group(2).strip()
            out.append(_Unit(line.strip(), "\n\n", "heading", cur_path(), level))
            i += 1
            first_in_block = True
            continue

        # Prose line: split into sentences. The first sentence takes the block/line
        # separator; later ones re-join exactly as written — no space after CJK
        # punctuation (none was there), one space after a Latin ".;" (the split ate it).
        sents = [u.strip() for u in _SENT_SPLIT.split(s) if u and u.strip()] or [s]
        for k, sent in enumerate(sents):
            if k == 0:
                sep = block_sep
            else:
                sep = " " if sents[k - 1][-1] in ".;" else ""
            out.append(_Unit(sent, sep, "prose", cur_path()))
        i += 1
        first_in_block = False
    return out


# ----------------------------------------------------------------- packing


def _hard_split(unit: _Unit, chunk_size: int, overlap: int, min_tail: int) -> list[_Unit]:
    """Split a single oversized unit. Atomic blocks split at line boundaries (rows /
    code lines); a long sentence falls back to character slices with overlap."""
    if unit.kind == "atomic" and "\n" in unit.text:
        pieces: list[str] = []
        buf: list[str] = []
        size = 0
        for ln in unit.text.split("\n"):
            if buf and size + len(ln) + 1 > chunk_size:
                pieces.append("\n".join(buf))
                buf, size = [], 0
            buf.append(ln)
            size += len(ln) + 1
        if buf:
            if pieces and size < min_tail:
                pieces[-1] += "\n" + "\n".join(buf)
            else:
                pieces.append("\n".join(buf))
        return [_Unit(p, unit.sep if n == 0 else "\n\n", "atomic", unit.heading) for n, p in enumerate(pieces)]

    step = max(chunk_size - overlap, 1)
    t = unit.text
    slices = [t[i : i + chunk_size] for i in range(0, len(t), step)]
    if len(slices) > 1 and len(slices[-1]) <= overlap:
        slices.pop()
    elif len(slices) > 1 and len(slices[-1]) < min_tail:
        tail = slices.pop()
        slices[-1] += tail[overlap:]
    return [_Unit(sl, unit.sep if n == 0 else "\n\n", unit.kind, unit.heading) for n, sl in enumerate(slices)]


def _render(units: list[_Unit]) -> str:
    parts: list[str] = []
    for n, u in enumerate(units):
        if n:
            parts.append(u.sep)
        parts.append(u.text)
    return "".join(parts).strip()


def _size(units: list[_Unit]) -> int:
    return sum(len(u.text) + (len(u.sep) if n else 0) for n, u in enumerate(units))


def chunk_markdown(text: str, chunk_size: int = 600, overlap: int = 100) -> list[Chunk]:
    units = _units(text)
    if not units:
        return []
    min_tail = max(overlap, chunk_size // 6)

    chunks: list[Chunk] = []
    buf: list[_Unit] = []
    carried = 0  # leading units of `buf` carried as overlap from the previous chunk

    def emit(next_is_heading: bool = False) -> None:
        """Emit `buf` as a chunk, then reset `buf` to the overlap tail.

        A trailing heading is not emitted — it moves to the next chunk's head."""
        nonlocal buf, carried
        held: list[_Unit] = []
        while buf and buf[-1].kind == "heading" and len(buf) > carried:
            held.insert(0, buf.pop())
        if len(buf) > carried:
            lead = buf[carried] if carried < len(buf) else buf[0]
            chunks.append(Chunk(_render(buf), lead.heading))
        # Overlap: whole trailing prose sentences within budget, never atomic blocks or
        # headings, and nothing when the next chunk opens with a heading (a section
        # start should not be prefixed with the previous section's tail).
        tail: list[_Unit] = []
        if not held and not next_is_heading:
            budget = overlap
            for u in reversed(buf):
                if u.kind != "prose" or len(u.text) > budget:
                    break
                tail.insert(0, u)
                budget -= len(u.text) + 1
        buf = tail + held
        carried = len(tail)

    for unit in units:
        if len(unit.text) > chunk_size:
            if len(buf) > carried:
                emit()
            else:
                buf, carried = [u for u in buf if u.kind == "heading"], 0
            for piece in _hard_split(unit, chunk_size, overlap, min_tail):
                if len(buf) > carried and _size(buf) + len(piece.text) + len(piece.sep) > chunk_size:
                    emit()
                buf.append(piece)
                if len(piece.text) >= chunk_size - min_tail:
                    emit()
            continue

        projected = _size(buf) + len(unit.text) + len(unit.sep)
        is_heading = unit.kind == "heading"
        if buf and len(buf) > carried and projected > chunk_size:
            emit(next_is_heading=is_heading)
        elif is_heading and len(buf) > carried and _size(buf) > chunk_size * 0.6:
            emit(next_is_heading=True)  # let the heading lead the next chunk, not dangle at the end
        buf.append(unit)

    # Final buffer: nothing to emit if it holds only carried overlap (pure duplicate).
    if len(buf) > carried:
        novel = buf[carried:]
        novel_text = _render(novel)
        last_only_headings = all(u.kind == "heading" for u in novel)
        if chunks and (
            last_only_headings
            or (len(novel_text) < min_tail and len(chunks[-1].text) + len(novel_text) + 2 <= chunk_size + min_tail)
        ):
            prev = chunks[-1]
            chunks[-1] = Chunk(prev.text + novel[0].sep + novel_text, prev.heading)
        else:
            chunks.append(Chunk(_render(buf), novel[0].heading))

    return [c for c in chunks if c.text.strip()]


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    return [c.text for c in chunk_markdown(text, chunk_size, overlap)]
