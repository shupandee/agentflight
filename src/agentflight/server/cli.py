"""``agentflight-server`` command: run the server or apply retention."""

from __future__ import annotations

import argparse
import logging
import time

from agentflight.server import service
from agentflight.server.db import make_engine, session_factory
from agentflight.server.settings import Settings

logger = logging.getLogger("agentflight.cleanup")

NS_PER_DAY = 86_400 * 1_000_000_000


def cleanup_once(settings: Settings, *, days: float | None, dry_run: bool) -> int:
    keep_days = days if days is not None else settings.retention_days
    cutoff_ns = time.time_ns() - int(keep_days * NS_PER_DAY)
    engine = make_engine()
    try:
        with session_factory(engine)() as session:
            count = service.cleanup_expired(session, cutoff_ns=cutoff_ns, dry_run=dry_run)
    finally:
        engine.dispose()
    verb = "would delete" if dry_run else "deleted"
    logger.info("retention: %s %d runs older than %s days", verb, count, keep_days)
    print(f"{verb} {count} runs older than {keep_days:g} days")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflight-server")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    clean = sub.add_parser("cleanup", help="delete runs older than the retention period")
    clean.add_argument("--days", type=float, help="override AGENTFLIGHT_RETENTION_DAYS")
    clean.add_argument("--dry-run", action="store_true", help="count, don't delete")
    clean.add_argument(
        "--loop",
        action="store_true",
        help="repeat every AGENTFLIGHT_CLEANUP_INTERVAL_HOURS instead of running once",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("agentflight.server.main:app", host=args.host, port=args.port)
        return 0

    settings = Settings.from_env()
    if not settings.retention_enabled:
        print("retention disabled (AGENTFLIGHT_RETENTION_ENABLED=false); nothing to do")
        return 0
    if args.days is not None and args.days <= 0:
        parser.error("--days must be positive")
    while True:
        cleanup_once(settings, days=args.days, dry_run=args.dry_run)
        if not args.loop:
            return 0
        time.sleep(settings.cleanup_interval_hours * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
