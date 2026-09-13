"""Ingest a document and query an external HugeGraph 1.7.x graph.

Set HUGEGRAPH_URI and OPENAI_API_KEY before running from the repository root:
    python examples/lightrag_hugegraph_example.py path/to/document.txt

Use a fresh matching workspace and data directory, not an existing deployment's
KV/vector data with a newly selected graph backend. See docs/HugeGraphStorage.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from lightrag import LightRAG, QueryParam
from lightrag.llm.openai import gpt_4o_mini_complete, openai_embed
from lightrag.utils import logger, setup_logger


async def main(document: Path, workspace: str, working_dir: Path) -> None:
    """Use one event loop for storage initialization, all operations, and cleanup."""
    load_dotenv(override=False)
    if not os.environ.get("HUGEGRAPH_URI"):
        raise ValueError("Set HUGEGRAPH_URI to your external HugeGraph service URL")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY for the example LLM and embeddings")

    content = document.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("The input document must contain non-empty text")
    rag = LightRAG(
        working_dir=str(working_dir),
        workspace=workspace,
        graph_storage="HugeGraphStorage",
        llm_model_func=gpt_4o_mini_complete,
        embedding_func=openai_embed,
    )
    try:
        await rag.initialize_storages()
        await rag.ainsert(content, file_paths=[str(document)])
        for mode in ("local", "global", "hybrid", "mix"):
            answer = await rag.aquery(
                "What are the main entities and how are they related?",
                param=QueryParam(mode=mode, enable_rerank=False),
            )
            logger.info("%s query result:\n%s", mode, answer)
    finally:
        await rag.finalize_storages()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path, help="UTF-8 text document to ingest")
    parser.add_argument("--workspace", default="hugegraph-example")
    parser.add_argument(
        "--working-dir", type=Path, default=Path("./rag_storage/hugegraph-example")
    )
    args = parser.parse_args()
    setup_logger("lightrag", level="INFO")
    asyncio.run(main(args.document, args.workspace, args.working_dir))
