"""``a6g-kb``: the campus knowledge server at the edge (MCP over streamable HTTP)."""

import argparse
import logging
from urllib.parse import urlsplit

import uvicorn
from a6g_common.config import ModelRole, load_config

from a6g_kb.index import HashingEmbedder, ModelEmbedder
from a6g_kb.server import build_kb_server
from a6g_kb.service import KnowledgeService


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="a6g-kb", description=__doc__)
    p.add_argument("--config-dir", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument(
        "--offline-embedder",
        action="store_true",
        help="deterministic hashing embedder instead of the embedder role",
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    cfg = load_config(args.config_dir, profile=args.profile)
    service = KnowledgeService(
        cfg.campus, cfg.knowledge, embedder=make_embedder(cfg, args.offline_embedder)
    )
    url = urlsplit(cfg.knowledge.url)
    host = url.hostname or "127.0.0.1"
    app = build_kb_server(service).streamable_http_app(
        streamable_http_path=url.path or "/mcp", host=host
    )
    uvicorn.run(app, host=host, port=url.port or 8400, log_level="warning")


def make_embedder(cfg, offline: bool = False):
    """The ``embedder`` role, or the hashing embedder offline / when the role fails."""
    if offline or not cfg.knowledge.use_embeddings:
        return HashingEmbedder()
    try:
        from a6g_common.models import embeddings

        embedder = ModelEmbedder(embeddings(ModelRole.EMBEDDER, cfg.models))
        embedder.embed(["probe"])  # a revoked key or no network fails here, not later
        return embedder
    except Exception as exc:  # no key, no network: retrieval stays lexical + hashing
        logging.getLogger(__name__).warning("embedder role unavailable (%s): hashing", exc)
        return HashingEmbedder()


if __name__ == "__main__":  # pragma: no cover
    main()
