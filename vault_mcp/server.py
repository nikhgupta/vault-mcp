"""FastMCP server exposing search, reindex, and stats tools."""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import subprocess
import sys
from pathlib import Path

from fastmcp import FastMCP

from .embeddings import embed_query
from .indexer import reindex_path, reindex_vault
from .store import DEFAULT_DB_PATH, VaultStore
from .watcher import start_watcher

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────

VAULT_PATH = Path(os.environ.get("VAULT_PATH", ".")).resolve()
DB_PATH = Path(os.environ.get("VAULT_DB_PATH", str(DEFAULT_DB_PATH)))

# ── MCP Server ───────────────────────────────────────────────

mcp = FastMCP("vault-mcp", instructions="Semantic search over a git-backed knowledge vault.")

_store: VaultStore | None = None


def _get_store() -> VaultStore:
    global _store
    if _store is None:
        _store = VaultStore(DB_PATH)
    return _store


@mcp.tool()
def search(
    query: str,
    top_k: int = 10,
    path_filter: str | None = None,
) -> list[dict]:
    """Semantic search over the vault.

    Args:
        query: Natural language search query.
        top_k: Number of results to return (default 10).
        path_filter: Optional path prefix to scope results (e.g. "captures/saturn/").

    Returns:
        List of matching chunks with text, file_path, heading_path, score, chunk_idx.
    """
    store = _get_store()
    query_vec = embed_query(query)
    return store.search(query_vec, top_k=top_k, path_filter=path_filter)


@mcp.tool()
def reindex(path: str | None = None) -> str:
    """Reindex the vault or a specific file/directory.

    Args:
        path: Optional relative path within the vault to reindex.
              If not provided, reindexes the entire vault (only changed files).

    Returns:
        Summary of indexing results.
    """
    store = _get_store()
    if path:
        target = VAULT_PATH / path
        if not target.exists():
            return f"Path not found: {path}"
        result = reindex_path(store, VAULT_PATH, target)
    else:
        result = reindex_vault(store, VAULT_PATH)
    return str(result)


@mcp.tool()
def stats() -> dict:
    """Get vault index statistics.

    Returns:
        Dict with total_files, total_chunks, last_indexed_at,
        vault_path, embedding_model, and db_path.
    """
    store = _get_store()
    s = store.stats()
    s["vault_path"] = str(VAULT_PATH)
    s["embedding_model"] = "text-embedding-3-small"
    s["db_path"] = str(DB_PATH)
    return s


@mcp.tool()
def write(
    path: str,
    content: str,
    commit_message: str | None = None,
) -> dict:
    """Write a file to the vault and optionally git commit.

    The caller is responsible for classification, frontmatter, and path structure.
    vault-mcp just writes the bytes and commits. The watcher handles re-indexing.

    Args:
        path: Relative path within the vault (e.g. "captures/saturn/2026/03/slug.md").
        content: Full file content including frontmatter.
        commit_message: If provided, git add + commit with this message. If omitted, file is written but not committed.

    Returns:
        Dict with status, absolute path, and optional git commit hash.
    """
    # Validate: no path traversal
    resolved = (VAULT_PATH / path).resolve()
    if not resolved.is_relative_to(VAULT_PATH):
        return {"status": "error", "error": "Path traversal not allowed"}

    # Write
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    result = {"status": "ok", "path": str(resolved), "relative_path": path}

    # Optional git commit
    if commit_message:
        try:
            subprocess.run(
                ["git", "add", path],
                cwd=VAULT_PATH, check=True, capture_output=True,
            )
            cp = subprocess.run(
                ["git", "commit", "-m", commit_message],
                cwd=VAULT_PATH, check=True, capture_output=True, text=True,
            )
            # Extract short hash
            rev = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=VAULT_PATH, capture_output=True, text=True,
            )
            result["committed"] = True
            result["commit_hash"] = rev.stdout.strip()
        except subprocess.CalledProcessError as e:
            result["committed"] = False
            result["git_error"] = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr

    return result


# ── CLI Entry Point ──────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        prog="vault-mcp",
        description="Git-backed folder → embeddings → MCP search",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    serve_parser = sub.add_parser("serve", help="Start MCP server")
    serve_parser.add_argument(
        "--watch", action="store_true", help="Watch vault for changes"
    )
    serve_parser.add_argument(
        "--transport", default="streamable-http",
        choices=["stdio", "streamable-http", "sse"],
        help="MCP transport (default: streamable-http)",
    )
    serve_parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    serve_parser.add_argument("--port", type=int, default=8100, help="Bind port (default: 8100)")

    # reindex
    reindex_parser = sub.add_parser("reindex", help="Reindex the vault")
    reindex_parser.add_argument(
        "path", nargs="?", default=None,
        help="Relative path within vault to reindex (default: full vault)",
    )
    reindex_parser.add_argument(
        "--force", action="store_true", help="Force re-embed all chunks"
    )

    # stats
    sub.add_parser("stats", help="Show index statistics")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.command == "serve":
        if args.watch:
            store = _get_store()
            stop_fn = start_watcher(store, VAULT_PATH)
            atexit.register(stop_fn)
            log.info("Watching %s for changes", VAULT_PATH)

        transport_kwargs = {}
        if args.transport != "stdio":
            transport_kwargs = {"host": args.host, "port": args.port}
        mcp.run(transport=args.transport, **transport_kwargs)

    elif args.command == "reindex":
        store = VaultStore(DB_PATH)
        if args.path:
            # Resolve relative to vault, not CWD
            target = VAULT_PATH / args.path
            if not target.exists():
                print(f"Path not found: {args.path} (resolved: {target})")
                sys.exit(1)
            result = reindex_path(store, VAULT_PATH, target)
        else:
            result = reindex_vault(store, VAULT_PATH, force=args.force)
        print(result)
        store.close()

    elif args.command == "stats":
        store = VaultStore(DB_PATH)
        s = store.stats()
        print(f"Files:   {s['total_files']}")
        print(f"Chunks:  {s['total_chunks']}")
        print(f"Last:    {s['last_indexed_at']}")
        store.close()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
