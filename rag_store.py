"""Local, dependency-light retrieval store for the knowledge-base / RAG feature.

Mirrors the storage style of memory_store.py / db.py (plain sqlite3, one file per concern),
but adds a numpy-backed brute-force cosine-similarity search over embedding vectors. LiteAgent
is single-user only (see tools.py's _admin_id docstring), so there is no per-user scoping here
-- one knowledge base, shared by whoever is using this instance, same spirit as the existing
workspace filesystem.

Chunk embeddings are stored as raw float32 bytes (struct via numpy.tobytes()) rather than JSON,
to keep the sqlite file compact and search fast (numpy.frombuffer avoids a parse step). Brute-
force cosine over the whole table is deliberately simple: fine for the thousands-of-chunks scale
a personal/small-team knowledge base actually reaches; if this ever needs to scale further, only
ChunkStore.search()'s internals would need to change (e.g. to a sqlite-vec ANN index) -- callers
and the on-disk schema wouldn't have to.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sqlite3

import numpy as np


class ChunkStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    source_path TEXT,
                    added_at TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES kb_documents(id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(document_id)")

    def add_document(
        self,
        title: str,
        source_path: str | None,
        chunks: list[str],
        vectors: list[list[float]],
    ) -> dict[str, Any]:
        if len(chunks) != len(vectors):
            raise ValueError("chunks 與 vectors 數量不一致")
        if not chunks:
            raise ValueError("沒有可索引的內容（文件可能是空的）")
        now = datetime.now(timezone.utc).isoformat()
        char_count = sum(len(c) for c in chunks)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO kb_documents (title, source_path, added_at, char_count, chunk_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (title, source_path, now, char_count, len(chunks)),
            )
            document_id = cur.lastrowid
            rows = []
            for idx, (text, vec) in enumerate(zip(chunks, vectors)):
                arr = np.asarray(vec, dtype=np.float32)
                rows.append((document_id, idx, text, int(arr.shape[0]), arr.tobytes()))
            conn.executemany(
                "INSERT INTO kb_chunks (document_id, chunk_index, text, dim, embedding) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        return {
            "document_id": document_id,
            "title": title,
            "chunk_count": len(chunks),
            "char_count": char_count,
        }

    def list_documents(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, source_path, added_at, char_count, chunk_count "
                "FROM kb_documents ORDER BY id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_document(self, document_id: int) -> bool:
        with self._connect() as conn:
            existing = conn.execute("SELECT id FROM kb_documents WHERE id=?", (document_id,)).fetchone()
            if not existing:
                return False
            conn.execute("DELETE FROM kb_chunks WHERE document_id=?", (document_id,))
            conn.execute("DELETE FROM kb_documents WHERE id=?", (document_id,))
        return True

    def is_empty(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM kb_chunks").fetchone()
        return int(row["n"]) == 0

    def search(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        """Brute-force cosine similarity over every stored chunk. Returns the top_k highest-
        scoring chunks, each tagged with its source document's title for citation."""
        with self._connect() as conn:
            chunk_rows = conn.execute(
                "SELECT c.id, c.document_id, c.chunk_index, c.text, c.dim, c.embedding, d.title, d.source_path "
                "FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id"
            ).fetchall()
        if not chunk_rows:
            return []
        query = np.asarray(query_vector, dtype=np.float32)
        query_norm = np.linalg.norm(query)
        if query_norm == 0:
            return []
        matrix = np.stack([
            np.frombuffer(row["embedding"], dtype=np.float32) for row in chunk_rows
        ])
        matrix_norms = np.linalg.norm(matrix, axis=1)
        matrix_norms[matrix_norms == 0] = 1e-9
        scores = (matrix @ query) / (matrix_norms * query_norm)
        top_k = max(1, min(top_k, len(chunk_rows)))
        top_indices = np.argpartition(-scores, top_k - 1)[:top_k]
        top_indices = top_indices[np.argsort(-scores[top_indices])]
        results = []
        for i in top_indices:
            row = chunk_rows[int(i)]
            results.append({
                "document_id": row["document_id"],
                "title": row["title"],
                "source_path": row["source_path"],
                "chunk_index": row["chunk_index"],
                "text": row["text"],
                "score": round(float(scores[int(i)]), 4),
            })
        return results
