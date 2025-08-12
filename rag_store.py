import os, json, uuid
from typing import List, Dict, Tuple
import numpy as np

class RagStore:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)
        self.index_path = os.path.join(self.base_dir, "index.jsonl")
        if not os.path.exists(self.index_path):
            open(self.index_path, "w").close()

    def _iter_records(self):
        if not os.path.exists(self.index_path):
            return
        with open(self.index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    def add_chunks(self, source_id: str, chunks: List[str], embeddings: List[List[float]]):
        with open(self.index_path, "a", encoding="utf-8") as f:
            for text, emb in zip(chunks, embeddings):
                rec = {"id": str(uuid.uuid4()), "source_id": source_id, "text": text, "embedding": emb}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def search(self, query_embedding: List[float], k: int = 5) -> List[Dict]:
        q = np.array(query_embedding, dtype=np.float32)
        results: List[Tuple[float, Dict]] = []
        for rec in self._iter_records():
            v = np.array(rec["embedding"], dtype=np.float32)
            denom = (np.linalg.norm(q) * np.linalg.norm(v)) or 1e-12
            sim = float(np.dot(q, v) / denom)
            results.append((sim, rec))
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:k]]
