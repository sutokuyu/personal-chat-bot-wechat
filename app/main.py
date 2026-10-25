"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.config import (
    default_project_root,
    list_instances,
    load_dotenv_if_present,
    load_instance,
)
from app.errors import ConfigurationError
from app.logging_setup import configure_logging
from app.memory.access_policy import MemoryAccessPolicy
from app.memory.repository import MemoryRepository
from app.runtime import create_runtime, run_wechat
from app.storage.database import Database
from app.wxauto_adapter import stable_id, user_id_for

logger = logging.getLogger("app.main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run DeepSeek-chan on a Windows WeChat account via wxauto."
    )
    parser.add_argument(
        "--instance",
        default="deepseek-chan",
        help="instance id (default: deepseek-chan)",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="config directory (default: <project>/config)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=default_project_root(),
        help="project root used to resolve relative database paths",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--list-instances", action="store_true")
    parser.add_argument(
        "--purge-user-memory",
        metavar="USER_NAME",
        help="delete this WeChat user's memories for the configured persona",
    )
    parser.add_argument(
        "--chat-name",
        help="exact WeChat chat name used with --purge-user-memory",
    )
    parser.add_argument(
        "--include-shared",
        action="store_true",
        help="also delete shared memories for this user (affects other personas)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    load_dotenv_if_present(args.project_root)

    if args.list_instances:
        for name in list_instances(config_dir=args.config_dir):
            print(name)
        return 0

    if not args.instance:
        print("error: --instance is required (or use --list-instances)", file=sys.stderr)
        return 2

    try:
        loaded = load_instance(
            args.instance,
            config_dir=args.config_dir,
            project_root=args.project_root,
        )
    except ConfigurationError as exc:
        logger.error("configuration error: %s", exc)
        return 2

    if args.purge_user_memory:
        return _purge_memory(loaded, args)

    try:
        runtime = create_runtime(loaded)
        asyncio.run(run_wechat(loaded, runtime))
    except ConfigurationError as exc:
        logger.error("startup configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("shutdown requested")
    return 0


def _purge_memory(loaded, args) -> int:
    if not args.chat_name:
        logger.error("--chat-name is required with --purge-user-memory")
        return 2
    wechat = loaded.config.wechat
    database = Database(loaded.database_path)
    database.initialize()
    policy = MemoryAccessPolicy(
        shared_memory_enabled=loaded.config.memory.allow_shared_memory
    )
    repository = MemoryRepository(database, policy)
    deleted = repository.purge_user_memories(
        guild_id=str(stable_id("account", wechat.account_id)),
        user_id=str(
            user_id_for(wechat.account_id, args.chat_name, args.purge_user_memory)
        ),
        persona_id=loaded.config.persona_id,
        include_shared=bool(args.include_shared),
    )
    print(
        f"deleted {deleted} memories for WeChat user {args.purge_user_memory} "
        f"in chat {args.chat_name!r} (persona {loaded.config.persona_id}"
        f"{', including shared' if args.include_shared else ''})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
