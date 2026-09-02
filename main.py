"""Supported local entry point for the reservation service.

The old CLI and GitHub Actions workflow have been retired because they bypass
the durable plan state, target-day parameter checks and post-submit
verification implemented by the local Web service.
"""

from __future__ import annotations

import argparse
import logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="chaoxing-local-reserve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("服务仅允许监听 127.0.0.1 或 localhost")

    import uvicorn

    from app.single_instance import acquire_service_mutex, release_service_mutex

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    if not acquire_service_mutex():
        logging.warning("本地预约服务已经在运行；请打开 http://127.0.0.1:8787/")
        return
    try:
        uvicorn.run("app.web:app", host=args.host, port=args.port, reload=False, access_log=False)
    finally:
        release_service_mutex()


if __name__ == "__main__":
    main()
