"""Retrieval-augmented generation over the regulatory KB, ADRs, provider docs and tenant documents (BB-§23)."""

EMBED_MODEL = "BAAI/bge-small-en-v1.5"          # 384-d, matches ai.chunks.embedding vector(384)
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
GLOBAL_TENANT = "ten_global"
