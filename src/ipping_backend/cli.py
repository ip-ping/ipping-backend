from __future__ import annotations

import argparse

from . import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ipping-backend")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Upgrade the database schema to the latest version")
    serve = sub.add_parser("serve", help="Run the API server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Server processes (default: IPPING_WORKERS or 1). More than one needs PostgreSQL.",
    )
    args = parser.parse_args(argv)

    from .config import get_settings

    settings = get_settings()
    if args.command == "migrate":
        from alembic import command

        from .migrate import alembic_config

        command.upgrade(alembic_config(settings.database_url), "head")
        print("Database is up to date.")
    elif args.command == "serve":
        import uvicorn

        workers = args.workers or settings.workers
        if workers < 1:
            parser.error("--workers must be at least 1")
        if workers > 1 and settings.database_url.startswith("sqlite"):
            parser.error(
                "more than one worker needs PostgreSQL: the processes agree on who is alive and "
                "which node is connected where through the database, and SQLite is for one process"
            )
        # Each worker is a full backend instance; they cooperate through PostgreSQL (see cluster.py).
        uvicorn.run(
            "ipping_backend.main:create_app",
            factory=True,
            host=args.host or settings.host,
            port=args.port or settings.port,
            workers=workers,
            proxy_headers=True,
            log_config=None,
        )


if __name__ == "__main__":
    main()
