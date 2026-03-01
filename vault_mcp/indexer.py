"""Vault indexer — walks folder, tracks changes, orchestrates pipeline."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .chunker import Chunk, chunk_markdown, chunk_plain_text
from .embeddings import embed_texts
from .extractor import TEXT_EXTENSIONS, extract_text, is_supported
from .store import VaultStore

log = logging.getLogger(__name__)

# Directories/files to skip
SKIP_DIRS = {".git", ".vault-mcp", "__pycache__", "node_modules", ".obsidian"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _walk_vault(vault_path: Path) -> list[Path]:
    """Walk vault and return all supported files."""
    files = []
    for item in sorted(vault_path.rglob("*")):
        if item.is_dir():
            continue
        if any(part in SKIP_DIRS for part in item.parts):
            continue
        if item.name in SKIP_FILES:
            continue
        if is_supported(item):
            files.append(item)
    return files


def _make_rel(path: Path, vault_path: Path) -> str:
    """Make path relative to vault root."""
    return str(path.relative_to(vault_path))


def _chunk_file(rel_path: str, text: str) -> list[Chunk]:
    """Choose chunking strategy based on file type."""
    suffix = Path(rel_path).suffix.lower()
    if suffix in (".md", ".txt"):
        return chunk_markdown(text)
    else:
        source_prefix = rel_path
        return chunk_plain_text(text, source_prefix=source_prefix)


class IndexResult:
    def __init__(self):
        self.indexed = 0
        self.skipped = 0
        self.deleted = 0
        self.renamed = 0
        self.chunks_embedded = 0
        self.chunks_reused = 0

    def __str__(self):
        parts = []
        if self.indexed:
            parts.append(f"{self.indexed} files indexed")
        if self.skipped:
            parts.append(f"{self.skipped} skipped (unchanged)")
        if self.renamed:
            parts.append(f"{self.renamed} renamed")
        if self.deleted:
            parts.append(f"{self.deleted} deleted")
        parts.append(f"{self.chunks_embedded} chunks embedded, {self.chunks_reused} reused")
        return ", ".join(parts)


def reindex_file(
    store: VaultStore,
    vault_path: Path,
    file_path: Path,
    result: IndexResult | None = None,
    force: bool = False,
) -> IndexResult:
    """Index a single file with chunk-level dedup."""
    if result is None:
        result = IndexResult()

    rel_path = _make_rel(file_path, vault_path)
    stat = file_path.stat()

    # Fast skip: mtime unchanged
    if not force:
        existing = store.get_file(rel_path)
        if existing and existing["mtime"] == stat.st_mtime:
            result.skipped += 1
            return result

    # Extract text
    text = extract_text(file_path)
    if text is None:
        log.warning("Could not extract text from %s", rel_path)
        return result

    file_hash = _content_hash(text)

    # File-level skip: content unchanged (mtime changed but content didn't)
    if not force:
        stored_hash = store.get_file_content_hash(rel_path)
        if stored_hash == file_hash:
            store.upsert_file(rel_path, stat.st_mtime, file_hash)
            result.skipped += 1
            return result

    # Chunk the text
    chunks = _chunk_file(rel_path, text)
    if not chunks:
        store.upsert_file(rel_path, stat.st_mtime, file_hash)
        result.indexed += 1
        return result

    # Chunk-level dedup: only embed changed chunks
    to_embed: list[tuple[int, Chunk]] = []  # (chunk_idx, chunk)
    for idx, chunk in enumerate(chunks):
        c_hash = _content_hash(chunk.text)
        stored_hash = store.get_chunk_hash(rel_path, idx)
        if stored_hash == c_hash and not force:
            result.chunks_reused += 1
        else:
            to_embed.append((idx, chunk))

    # Batch embed only changed chunks
    if to_embed:
        texts_to_embed = [c.text for _, c in to_embed]
        embeddings = embed_texts(texts_to_embed)

        for (idx, chunk), embedding in zip(to_embed, embeddings):
            store.upsert_chunk(
                file_path=rel_path,
                chunk_idx=idx,
                text=chunk.text,
                text_hash=_content_hash(chunk.text),
                embedding=embedding,
                heading_path=chunk.heading_path,
            )
            result.chunks_embedded += 1

    # Clean up removed chunks (file shrank)
    store.delete_chunks_after(rel_path, len(chunks))

    # Update file record
    store.upsert_file(rel_path, stat.st_mtime, file_hash)
    result.indexed += 1
    return result


def reindex_vault(
    store: VaultStore,
    vault_path: Path,
    force: bool = False,
) -> IndexResult:
    """Full vault reindex with rename detection and change tracking."""
    result = IndexResult()
    vault_path = vault_path.resolve()

    # Walk vault
    disk_files = _walk_vault(vault_path)
    disk_paths = {_make_rel(f, vault_path) for f in disk_files}

    # Get stored paths
    stored_paths = store.get_all_file_paths()

    # Detect renames: disappeared files matched to new files by content_hash
    disappeared = stored_paths - disk_paths
    appeared = disk_paths - stored_paths

    if disappeared and appeared:
        # Build hash → path maps
        disappeared_hashes: dict[str, str] = {}
        for path in disappeared:
            h = store.get_file_content_hash(path)
            if h:
                disappeared_hashes[h] = path

        for new_path in list(appeared):
            new_file = vault_path / new_path
            if not new_file.exists():
                continue
            text = extract_text(new_file)
            if text is None:
                continue
            h = _content_hash(text)
            if h in disappeared_hashes:
                old_path = disappeared_hashes[h]
                store.rename_file(old_path, new_path)
                # Update mtime
                store.upsert_file(new_path, new_file.stat().st_mtime, h)
                disappeared.discard(old_path)
                appeared.discard(new_path)
                result.renamed += 1
                log.info("Detected rename: %s → %s", old_path, new_path)

    # Delete truly removed files
    for path in disappeared:
        store.delete_file(path)
        result.deleted += 1
        log.info("Deleted from index: %s", path)

    # Index all current files (reindex_file handles skip logic)
    for file_path in disk_files:
        reindex_file(store, vault_path, file_path, result, force=force)

    return result


def reindex_path(
    store: VaultStore,
    vault_path: Path,
    target_path: Path,
) -> IndexResult:
    """Reindex a specific file or directory."""
    result = IndexResult()
    vault_path = vault_path.resolve()
    target_path = target_path.resolve()

    if target_path.is_file():
        reindex_file(store, vault_path, target_path, result, force=True)
    elif target_path.is_dir():
        for file_path in _walk_vault(target_path):
            reindex_file(store, vault_path, file_path, result, force=True)

    return result
