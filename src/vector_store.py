"""内存向量库：整份文档对应一个向量，余弦相似度检索。"""

from dataclasses import dataclass
import math


@dataclass
class VectorStoreItem:
    embedding: list[float]
    document: str


class VectorStore:
    def __init__(self) -> None:
        self.items: list[VectorStoreItem] = []

    def add_embedding(self, embedding: list[float], document: str) -> None:
        if not embedding or not all(math.isfinite(x) for x in embedding):
            raise ValueError("向量必须是非空的有限数值数组")
        if self.items and len(embedding) != len(self.items[0].embedding):
            raise ValueError("向量维度不一致")
        self.items.append(VectorStoreItem(list(embedding), document))

    def search(self, query_embedding: list[float], top_k: int = 3) -> list[str]:
        if top_k < 0:
            raise ValueError("top_k 不能小于 0")
        scored = [
            (self.cosine_similarity(query_embedding, item.embedding), item.document)
            for item in self.items
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [document for _, document in scored[:top_k]]

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or len(a) != len(b):
            raise ValueError("比较的两个向量必须非空且维度一致")
        if not all(math.isfinite(x) for x in [*a, *b]):
            raise ValueError("向量包含无效数值")
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)
