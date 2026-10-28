"""``a6g-interactions``: serve the InteractionStore at the edge.

In memory only; a restart loses every entry (the robots' copies re-create them).
"""

import argparse
import os
from urllib.parse import urlsplit

import uvicorn
from a6g_common.config import load_config

from a6g_interactions.server import build_interactions_app
from a6g_interactions.store import InteractionStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default=os.environ.get("A6G_CONFIG_DIR", "config"))
    args = parser.parse_args()
    cfg = load_config(args.config_dir)
    url = urlsplit(cfg.interactions.url)
    store = InteractionStore(cfg.interactions.interaction_ttl_min)
    uvicorn.run(
        build_interactions_app(store),
        host=url.hostname or "127.0.0.1",
        port=url.port or 8410,
        access_log=False,  # paths carry ids only, but keep the edge service quiet
        log_level="warning",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
