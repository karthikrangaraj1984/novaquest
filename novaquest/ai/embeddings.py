"""Local text embeddings through Ollama's HTTP API (optional; full-text search works without it)."""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Sequence

log = logging.getLogger("novaquest.embed")


class OllamaEmbedder:
    """Calls POST {url}/api/embed on a local Ollama server."""

    def __init__(self, url: str, model: str, dims: int, timeout_s: float = 30.0, batch: int = 32) -> None:
        self.url = url.rstrip("/") + "/api/embed"
        self.model = model
        self.dims = dims
        self.timeout_s = timeout_s
        self.batch = batch

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in batches; raises on a wrong vector size."""
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            body = json.dumps({"model": self.model, "input": list(texts[i:i + self.batch])}).encode()
            req = urllib.request.Request(self.url, data=body, headers={"content-type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # always local
            with opener.open(req, timeout=self.timeout_s) as resp:
                vecs = json.loads(resp.read())["embeddings"]
            for v in vecs:
                if len(v) != self.dims:
                    raise ValueError(f"embedding has {len(v)} dims, expected {self.dims}")
            out.extend(vecs)
        return out


def embed_missing(db, embedder: OllamaEmbedder, batch: int = 64) -> int:
    """Fill in embeddings for passages that have none. Returns how many were embedded."""
    with db.platform() as tx:
        if not tx.val("""SELECT count(*) FROM information_schema.columns
                         WHERE table_name = 'source_passages' AND column_name = 'embedding'"""):
            raise RuntimeError("pgvector is not enabled in this database. Install pgvector for your Postgres "
                               "(Windows: run the NovaQuest installer with --with-pgvector), then run `nq init` again.")
    done = 0
    while True:
        with db.platform() as tx:
            rows = tx.all("SELECT id, body FROM source_passages WHERE embedding IS NULL ORDER BY id LIMIT %s", [batch])
        if not rows:
            return done
        vecs = embedder([r["body"] for r in rows])
        with db.platform() as tx:
            tx.many("UPDATE source_passages SET embedding = %s::vector WHERE id = %s",
                    [("[" + ",".join(f"{x:.6f}" for x in v) + "]", r["id"]) for r, v in zip(rows, vecs)])
        done += len(rows)
