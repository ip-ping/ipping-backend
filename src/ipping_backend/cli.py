from __future__ import annotations

import argparse

from . import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ipping-backend")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Upgrade the database schema to the latest version")
    geoip = sub.add_parser(
        "geoip-update",
        help="Download the free DB-IP Lite city and ASN databases (CC BY 4.0) for location cross-checks",
    )
    geoip.add_argument("--dir", default=None, help="Where to put them (default: IPPING_GEOIP_DIR)")
    feeds = sub.add_parser(
        "geofeed-update",
        help="Fetch the geofeeds (RFC 8805) listed in IPPING_GEOFEED_URLS, or given with --url, for location cross-checks",
    )
    feeds.add_argument("--dir", default=None, help="Where to keep them (default: IPPING_GEOIP_DIR)")
    feeds.add_argument(
        "--url", action="append", default=[], help="A geofeed https URL (repeat for more)"
    )
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
    elif args.command == "geoip-update":
        from pathlib import Path

        from .geolocation.update import update

        directory = args.dir or settings.geoip_dir
        if not directory:
            parser.error("give --dir or set IPPING_GEOIP_DIR")
        for path in update(Path(directory)):
            print(f"{path.name}: {path.stat().st_size // 1_000_000} MB")
        print(
            "IP geolocation by DB-IP (db-ip.com), CC BY 4.0. The server picks the files up by itself."
        )
    elif args.command == "geofeed-update":
        from pathlib import Path

        from .geolocation.geofeed import FILE_NAME, host_of
        from .geolocation.geofeed import update as update_feeds

        directory = args.dir or settings.geoip_dir
        if not directory:
            parser.error("give --dir or set IPPING_GEOIP_DIR")
        urls = args.url or [
            u.strip() for u in (settings.geofeed_urls or "").split(",") if u.strip()
        ]
        if not urls:
            parser.error("give --url, or list the feeds in IPPING_GEOFEED_URLS")
        results = update_feeds(Path(directory) / FILE_NAME, urls)
        for result in results:
            if result.error:
                kept = " (kept what it had)" if result.kept_previous else ""
                print(f"{host_of(result.url)}: FAILED, {result.error}{kept}")
            else:
                print(
                    f"{host_of(result.url)}: {result.entries} prefixes, {result.skipped} lines skipped"
                )
        if all(r.error and not r.kept_previous for r in results):
            raise SystemExit(1)
        print("The server picks the file up by itself.")
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
