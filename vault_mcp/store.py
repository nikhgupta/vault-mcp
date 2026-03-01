"""SQLite + sqlite-vec storage for chunks and embeddings."""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import sqlite_vec

# Default DB location
DEFAULT_DB_PATH = Path.home() / ".vault-mcp" / "index.db"


def _serialize_f32(vec: list[float]) -> bytes:
    """Serialize a float32 vector to bytes for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


class VaultStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                content_hash TEXT NOT NULL,
                last_indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                chunk_idx INTEGER NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                heading_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(file_path, chunk_idx)
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_file_path
                ON chunks(file_path);
            CREATE INDEX IF NOT EXISTS idx_chunks_text_hash
                ON chunks(file_path, chunk_idx, text_hash);
        """)
        # Create sqlite-vec virtual table (1536 dims for text-embedding-3-small)
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec "
            "USING vec0(embedding float[1536])"
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ── File tracking ─────────────────────────────────────────

    def get_file(self, path: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_file(self, path: str, mtime: float, content_hash: str):
        self.conn.execute(
            """INSERT INTO files (path, mtime, content_hash, last_indexed_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(path) DO UPDATE SET
                 mtime = excluded.mtime,
                 content_hash = excluded.content_hash,
                 last_indexed_at = datetime('now')""",
            (path, mtime, content_hash),
        )
        self.conn.commit()

    def get_all_file_paths(self) -> set[str]:
        rows = self.conn.execute("SELECT path FROM files").fetchall()
        return {r["path"] for r in rows}

    def get_file_content_hash(self, path: str) -> str | None:
        row = self.conn.execute(
            "SELECT content_hash FROM files WHERE path = ?", (path,)
        ).fetchone()
        return row["content_hash"] if row else None

    def rename_file(self, old_path: str, new_path: str):
        """Update path in both files and chunks tables (zero re-embedding)."""
        self.conn.execute(
            "UPDATE files SET path = ? WHERE path = ?", (new_path, old_path)
        )
        self.conn.execute(
            "UPDATE chunks SET file_path = ? WHERE file_path = ?",
            (new_path, old_path),
        )
        self.conn.commit()

    def delete_file(self, path: str):
        """Remove file and all its chunks (including vec entries)."""
        chunk_ids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM chunks WHERE file_path = ?", (path,)
            ).fetchall()
        ]
        if chunk_ids:
            placeholders = ",".join("?" * len(chunk_ids))
            self.conn.execute(
                f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})",
                chunk_ids,
            )
        self.conn.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
        self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self.conn.commit()

    # ── Chunk operations ──────────────────────────────────────

    def get_chunks_for_file(self, file_path: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE file_path = ? ORDER BY chunk_idx",
            (file_path,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_chunk_hash(self, file_path: str, chunk_idx: int) -> str | None:
        row = self.conn.execute(
            "SELECT text_hash FROM chunks WHERE file_path = ? AND chunk_idx = ?",
            (file_path, chunk_idx),
        ).fetchone()
        return row["text_hash"] if row else None

    def upsert_chunk(
        self,
        file_path: str,
        chunk_idx: int,
        text: str,
        text_hash: str,
        embedding: list[float],
        heading_path: str = "",
    ) -> int:
        """Insert or update a chunk and its embedding. Returns chunk id."""
        cursor = self.conn.execute(
            "SELECT id FROM chunks WHERE file_path = ? AND chunk_idx = ?",
            (file_path, chunk_idx),
        )
        existing = cursor.fetchone()

        if existing:
            chunk_id = existing["id"]
            self.conn.execute(
                """UPDATE chunks SET text = ?, text_hash = ?, heading_path = ?
                   WHERE id = ?""",
                (text, text_hash, heading_path, chunk_id),
            )
            # Update vec entry
            self.conn.execute(
                "DELETE FROM chunks_vec WHERE rowid = ?", (chunk_id,)
            )
            self.conn.execute(
                "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                (chunk_id, _serialize_f32(embedding)),
            )
        else:
            cursor = self.conn.execute(
                """INSERT INTO chunks (file_path, chunk_idx, text, text_hash, heading_path)
                   VALUES (?, ?, ?, ?, ?)""",
                (file_path, chunk_idx, text, text_hash, heading_path),
            )
            chunk_id = cursor.lastrowid
            self.conn.execute(
                "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                (chunk_id, _serialize_f32(embedding)),
            )

        self.conn.commit()
        return chunk_id

    def delete_chunks_after(self, file_path: str, chunk_idx: int):
        """Delete chunks with index >= chunk_idx (file shrank)."""
        chunk_ids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM chunks WHERE file_path = ? AND chunk_idx >= ?",
                (file_path, chunk_idx),
            ).fetchall()
        ]
        if chunk_ids:
            placeholders = ",".join("?" * len(chunk_ids))
            self.conn.execute(
                f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})",
                chunk_ids,
            )
            self.conn.execute(
                f"DELETE FROM chunks WHERE id IN ({placeholders})", chunk_ids
            )
            self.conn.commit()

    # ── Search ────────────────────────────────────────────────

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        path_filter: str | None = None,
    ) -> list[dict]:
        """KNN search using sqlite-vec, with optional path prefix filter."""
        # sqlite-vec KNN search requires k=? in WHERE clause
        fetch_k = top_k * 3 if path_filter else top_k
        rows = self.conn.execute(
            """SELECT rowid, distance
               FROM chunks_vec
               WHERE embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (_serialize_f32(query_embedding), fetch_k),
        ).fetchall()

        results = []
        for row in rows:
            chunk = self.conn.execute(
                "SELECT * FROM chunks WHERE id = ?", (row["rowid"],)
            ).fetchone()
            if chunk is None:
                continue
            if path_filter and not chunk["file_path"].startswith(path_filter):
                continue
            results.append(
                {
                    "text": chunk["text"],
                    "file_path": chunk["file_path"],
                    "heading_path": chunk["heading_path"],
                    "chunk_idx": chunk["chunk_idx"],
                    "score": 1.0 - row["distance"],  # cosine similarity
                }
            )
            if len(results) >= top_k:
                break

        return results

    # ── Stats ─────────────────────────────────────────────────

    def stats(self) -> dict:
        file_count = self.conn.execute("SELECT COUNT(*) as c FROM files").fetchone()["c"]
        chunk_count = self.conn.execute("SELECT COUNT(*) as c FROM chunks").fetchone()["c"]
        last_indexed = self.conn.execute(
            "SELECT MAX(last_indexed_at) as t FROM files"
        ).fetchone()["t"]
        return {
            "total_files": file_count,
            "total_chunks": chunk_count,
            "last_indexed_at": last_indexed,
        }
