"""Watchdog file monitor with debounce for vault changes."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .extractor import is_supported
from .indexer import SKIP_DIRS, SKIP_FILES, IndexResult, reindex_file
from .store import VaultStore

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0


class _DebouncedHandler(FileSystemEventHandler):
    """Handles file events with per-file debouncing."""

    def __init__(self, store: VaultStore, vault_path: Path):
        self.store = store
        self.vault_path = vault_path
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._db_lock = threading.Lock()  # serialize DB access

    def _should_skip(self, path: Path) -> bool:
        if any(part in SKIP_DIRS for part in path.parts):
            return True
        if path.name in SKIP_FILES:
            return True
        if path.is_dir():
            return True
        return not is_supported(path)

    def _schedule(self, key: str, action: str, **kwargs):
        with self._lock:
            if key in self._timers:
                self._timers[key].cancel()

            def _do():
                self._handle(action, **kwargs)
                with self._lock:
                    self._timers.pop(key, None)

            timer = threading.Timer(DEBOUNCE_SECONDS, _do)
            self._timers[key] = timer
            timer.start()

    def _handle(self, action: str, **kwargs):
        with self._db_lock:
            try:
                if action == "delete":
                    rel = kwargs["rel_path"]
                    self.store.delete_file(rel)
                    log.info("Removed from index: %s", rel)

                elif action == "rename":
                    old_rel = kwargs["old_rel"]
                    new_path = kwargs["new_path"]
                    new_rel = str(Path(new_path).relative_to(self.vault_path))
                    self.store.rename_file(old_rel, new_rel)
                    # Update mtime
                    p = Path(new_path)
                    if p.exists():
                        from .indexer import _content_hash
                        from .extractor import extract_text
                        text = extract_text(p)
                        if text:
                            self.store.upsert_file(
                                new_rel, p.stat().st_mtime, _content_hash(text)
                            )
                    log.info("Renamed in index: %s → %s", old_rel, new_rel)

                elif action in ("create", "modify"):
                    file_path = Path(kwargs["path"])
                    if file_path.exists():
                        result = IndexResult()
                        reindex_file(
                            self.store, self.vault_path, file_path, result
                        )
                        log.info("Reindexed: %s (%s)", file_path, result)

            except Exception:
                log.exception("Error handling %s: %s", action, kwargs)

    def on_created(self, event: FileSystemEvent):
        if not self._should_skip(Path(event.src_path)):
            self._schedule(event.src_path, "create", path=event.src_path)

    def on_modified(self, event: FileSystemEvent):
        if not self._should_skip(Path(event.src_path)):
            self._schedule(event.src_path, "modify", path=event.src_path)

    def on_deleted(self, event: FileSystemEvent):
        path = Path(event.src_path)
        if any(part in SKIP_DIRS for part in path.parts):
            return
        try:
            rel = str(path.relative_to(self.vault_path))
        except ValueError:
            return
        self._schedule(event.src_path, "delete", rel_path=rel)

    def on_moved(self, event: FileSystemEvent):
        old_path = Path(event.src_path)
        new_path = Path(event.dest_path)

        if any(part in SKIP_DIRS for part in old_path.parts):
            # Old path in skip dir — treat new as create
            if not self._should_skip(new_path):
                self._schedule(event.dest_path, "create", path=event.dest_path)
            return

        if self._should_skip(new_path):
            # Moved to unsupported location — treat as delete
            try:
                rel = str(old_path.relative_to(self.vault_path))
            except ValueError:
                return
            self._schedule(event.src_path, "delete", rel_path=rel)
            return

        # Both paths valid — use rename (zero re-embedding)
        try:
            old_rel = str(old_path.relative_to(self.vault_path))
        except ValueError:
            return
        # Use old path as key to cancel any pending delete
        self._schedule(
            event.src_path, "rename",
            old_rel=old_rel, new_path=event.dest_path,
        )


def start_watcher(store: VaultStore, vault_path: Path) -> callable:
    """Start watching vault_path for changes. Returns a stop function."""
    handler = _DebouncedHandler(store, vault_path.resolve())
    observer = Observer()
    observer.schedule(handler, str(vault_path), recursive=True)
    observer.start()
    log.info("Watching %s for changes", vault_path)

    def stop():
        observer.stop()
        observer.join()

    return stop
