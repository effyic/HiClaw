"""命令行入口：sensitive-content serve / migrate。"""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="sensitive-content")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="启动 HTTP 服务（不执行 DDL）")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    migrate_parser = sub.add_parser("migrate", help="执行数据库迁移（唯一 DDL 执行方）")
    migrate_parser.add_argument(
        "--wait-seconds", type=int, default=None,
        help="等待数据库可用的上限（默认取 SENSITIVE_CONTENT_MIGRATE_WAIT_SECONDS，120s）",
    )

    args = parser.parse_args(argv)

    if args.command == "migrate":
        from sensitive_content.migrate import run_migrations

        run_migrations(max_wait_seconds=args.wait_seconds)
        return 0

    if args.command == "serve":
        import uvicorn

        from sensitive_content import config
        from sensitive_content.api import create_app

        uvicorn.run(
            create_app(),
            host=args.host or config.serve_host(),
            port=args.port or config.serve_port(),
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
