import httpx

from .config import required_env
from .utils import log_title
from .vector_store import VectorStore


class EmbeddingRetriever:
    def __init__(self, embedding_model: str) -> None:
        self.embedding_model = embedding_model
        self.vector_store = VectorStore()
        self.base_url = required_env("EMBEDDING_BASE_URL").rstrip("/")
        self.api_key = required_env("EMBEDDING_KEY")

    async def embed_document(self, document: str) -> list[float]:
        log_title("EMBEDDING DOCUMENT")
        embedding = await self.embed(document)
        self.vector_store.add_embedding(embedding, document)
        return embedding

    async def embed_query(self, query: str) -> list[float]:
        log_title("EMBEDDING QUERY")
        return await self.embed(query)

    async def embed(self, document: str) -> list[float]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.embedding_model,
                    "input": document,
                    "encoding_format": "float",
                },
            )
            # 【修改】保留服务端的错误原因，避免终端只显示笼统的 HTTP 400。
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                try:
                    error = response.json()
                except ValueError:
                    error = response.text
                if isinstance(error, dict):
                    error = error.get("error", error)
                if isinstance(error, dict):
                    detail = "，".join(
                        f"{field}={error[field]}" for field in ("code", "message") if field in error
                    )
                else:
                    detail = str(error)
                detail = (detail or response.reason_phrase).replace(self.api_key, "[REDACTED]")[:500]
                message = (
                    f"向量接口请求失败：HTTP {response.status_code}，"
                    f"模型 {self.embedding_model}，输入 {len(document)} 字符；"
                    f"服务端详情：{detail}"
                )
                trace_id = response.headers.get("x-siliconcloud-trace-id")
                if trace_id:
                    message += f"；trace_id={trace_id[:128]}"
                raise httpx.HTTPStatusError(
                    message.replace(self.api_key, "[REDACTED]"),
                    request=exc.request, response=exc.response,
                ) from exc
            data = response.json()
        try:
            embedding = data["data"][0]["embedding"]
            if not isinstance(embedding, list) or not embedding:
                raise ValueError("缺少有效的 embedding 数组")
            return [float(value) for value in embedding]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("向量接口返回的数据格式不正确") from exc

    async def retrieve(self, query: str, top_k: int = 3) -> list[str]:
        return self.vector_store.search(await self.embed_query(query), top_k)
