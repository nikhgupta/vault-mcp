"""Heading-aware markdown chunking with token limits."""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

# cl100k_base matches OpenAI embedding models
_enc = tiktoken.get_encoding("cl100k_base")

MAX_CHUNK_TOKENS = 512
MIN_CHUNK_TOKENS = 100
OVERLAP_TOKENS = 50


@dataclass
class Chunk:
    text: str
    heading_path: str  # e.g. "# Strategy > ## Vision > ### North Star"
    token_count: int


def count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def _last_sentences(text: str, max_tokens: int = OVERLAP_TOKENS) -> str:
    """Extract last 1-2 sentences for overlap, up to max_tokens."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    overlap = ""
    for s in reversed(sentences):
        candidate = s if not overlap else f"{s} {overlap}"
        if count_tokens(candidate) > max_tokens:
            break
        overlap = candidate
    return overlap


# ── Markdown heading-aware chunking ──────────────────────────


@dataclass
class _Section:
    """A section of markdown under a specific heading hierarchy."""
    heading_path: str  # breadcrumb like "# Strategy > ## Vision"
    level: int         # heading level (1-6), 0 for preamble
    body: str          # text content (without the heading line itself)


def _parse_sections(text: str) -> list[_Section]:
    """Split markdown into sections by headings, tracking the breadcrumb path."""
    lines = text.split("\n")
    sections: list[_Section] = []
    # Track current heading at each level
    heading_stack: dict[int, str] = {}
    current_body_lines: list[str] = []
    current_level = 0
    current_path = ""

    def _flush():
        body = "\n".join(current_body_lines).strip()
        if body or current_path:
            sections.append(_Section(
                heading_path=current_path,
                level=current_level,
                body=body,
            ))

    for line in lines:
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            _flush()
            current_body_lines = []
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            heading_tag = f"{'#' * level} {title}"

            # Clear deeper headings when we encounter a same-or-higher level
            for lvl in list(heading_stack.keys()):
                if lvl >= level:
                    del heading_stack[lvl]

            heading_stack[level] = heading_tag
            current_level = level
            current_path = " > ".join(
                heading_stack[k] for k in sorted(heading_stack)
            )
        else:
            current_body_lines.append(line)

    _flush()
    return sections


def _split_by_paragraphs(
    text: str, heading_path: str, max_tokens: int = MAX_CHUNK_TOKENS
) -> list[Chunk]:
    """Split text into paragraph-grouped chunks, each ≤ max_tokens."""
    paragraphs = re.split(r'\n\s*\n', text.strip())
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        para_tokens = count_tokens(para)

        # Single paragraph exceeds limit — force it as its own chunk
        if para_tokens > max_tokens:
            if current_parts:
                merged = "\n\n".join(current_parts)
                chunks.append(Chunk(
                    text=merged,
                    heading_path=heading_path,
                    token_count=count_tokens(merged),
                ))
                current_parts = []
                current_tokens = 0
            chunks.append(Chunk(
                text=para,
                heading_path=heading_path,
                token_count=para_tokens,
            ))
            continue

        if current_tokens + para_tokens > max_tokens and current_parts:
            merged = "\n\n".join(current_parts)
            chunks.append(Chunk(
                text=merged,
                heading_path=heading_path,
                token_count=count_tokens(merged),
            ))
            current_parts = []
            current_tokens = 0

        current_parts.append(para)
        current_tokens += para_tokens

    if current_parts:
        merged = "\n\n".join(current_parts)
        chunks.append(Chunk(
            text=merged,
            heading_path=heading_path,
            token_count=count_tokens(merged),
        ))

    return chunks


def chunk_markdown(text: str) -> list[Chunk]:
    """Chunk markdown text using heading-aware splitting.

    Strategy:
    1. Parse into sections by headings (h1 → h2 → h3)
    2. Each section = candidate chunk, prefixed with heading breadcrumb
    3. If section > MAX_CHUNK_TOKENS: split by paragraphs
    4. If section < MIN_CHUNK_TOKENS: merge with next sibling
    """
    sections = _parse_sections(text)
    if not sections:
        return []

    # Phase 1: Merge small sibling sections
    merged_sections: list[_Section] = []
    i = 0
    while i < len(sections):
        section = sections[i]
        tokens = count_tokens(section.body)

        # Try merging with subsequent siblings at the same level
        while (
            tokens < MIN_CHUNK_TOKENS
            and i + 1 < len(sections)
            and sections[i + 1].level >= section.level
        ):
            next_sec = sections[i + 1]
            combined_tokens = count_tokens(section.body + "\n\n" + next_sec.body)
            if combined_tokens > MAX_CHUNK_TOKENS:
                break
            # Merge: keep the broader heading path
            section = _Section(
                heading_path=section.heading_path,
                level=section.level,
                body=section.body + "\n\n" + next_sec.body,
            )
            tokens = combined_tokens
            i += 1

        merged_sections.append(section)
        i += 1

    # Phase 2: Convert sections to chunks, splitting large ones
    raw_chunks: list[Chunk] = []
    for section in merged_sections:
        tokens = count_tokens(section.body)
        if not section.body:
            continue

        if tokens <= MAX_CHUNK_TOKENS:
            raw_chunks.append(Chunk(
                text=section.body,
                heading_path=section.heading_path,
                token_count=tokens,
            ))
        else:
            raw_chunks.extend(
                _split_by_paragraphs(section.body, section.heading_path)
            )

    # Phase 3: Add overlap between consecutive chunks
    final_chunks: list[Chunk] = []
    for i, chunk in enumerate(raw_chunks):
        if i > 0 and raw_chunks[i - 1].heading_path == chunk.heading_path:
            overlap = _last_sentences(raw_chunks[i - 1].text)
            if overlap:
                text_with_overlap = overlap + "\n\n" + chunk.text
                final_chunks.append(Chunk(
                    text=text_with_overlap,
                    heading_path=chunk.heading_path,
                    token_count=count_tokens(text_with_overlap),
                ))
                continue
        final_chunks.append(chunk)

    return final_chunks


# ── Plain text chunking (for extracted PDF/doc text) ─────────


def chunk_plain_text(
    text: str, source_prefix: str = ""
) -> list[Chunk]:
    """Chunk plain text by paragraphs with optional source prefix."""
    return _split_by_paragraphs(text, heading_path=source_prefix)
