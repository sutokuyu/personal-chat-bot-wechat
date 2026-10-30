"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
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
from app.tools.web_tools import format_results, search_web
from app.wxauto_adapter import stable_id, user_id_for

logger = logging.getLogger("app.main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run DeepSeek-chan on a Windows WeChat 4.x account via wxauto4."
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
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="also write logs here (default: <project>/data/logs/app.log)",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="only log to the console",
    )
    parser.add_argument("--list-instances", action="store_true")
    parser.add_argument(
        "--list-memories",
        action="store_true",
        help="print the long-term memories stored for this instance and exit",
    )
    parser.add_argument(
        "--memory-limit",
        type=int,
        default=50,
        help="how many memories --list-memories prints (default: 50)",
    )
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
    parser.add_argument(
        "--check-web-search",
        metavar="QUERY",
        help="run one web_search request with the instance settings and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_file = None
    if not args.no_log_file:
        log_file = args.log_file or (args.project_root / "data" / "logs" / "app.log")
    configure_logging(args.log_level, log_file=log_file)
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

    if args.list_memories:
        return _list_memories(loaded, args)

    if args.purge_user_memory:
        return _purge_memory(loaded, args)

    if args.check_web_search:
        return _check_web_search(loaded, args)

    try:
        runtime = create_runtime(loaded)
        asyncio.run(run_wechat(loaded, runtime))
    except ConfigurationError as exc:
        logger.error("startup configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("shutdown requested")
    return 0


def _list_memories(loaded, args) -> int:
    wechat = loaded.config.wechat
    # Keep stdout clean for a read-only listing command.
    logging.getLogger("app.storage.database").setLevel(logging.WARNING)
    database = Database(loaded.database_path)
    database.initialize()
    guild_id = str(stable_id("account", wechat.account_id))
    persona_id = loaded.config.persona_id
    limit = max(1, min(int(args.memory_limit), 500))

    rows = database.read(
        lambda conn: conn.execute(
            """
            SELECT user_id, scope, category, content, importance, confidence,
                   updated_at, expires_at
            FROM memories
            WHERE guild_id = ? AND persona_id = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT ?
            """,
            (guild_id, persona_id, limit),
        ).fetchall()
    )
    profile = database.read(
        lambda conn: conn.execute(
            """
            SELECT COUNT(*) AS users, COALESCE(SUM(interaction_count), 0) AS interactions
            FROM user_profiles
            WHERE guild_id = ? AND persona_id = ?
            """,
            (guild_id, persona_id),
        ).fetchone()
    )

    print(f"数据库：{database.path}")
    print(
        f"账号：{wechat.account_id}（guild={guild_id}）  人格：{persona_id}"
    )
    print(
        f"用户档案：{profile['users']} 人，累计互动 {profile['interactions']} 次"
    )
    if not rows:
        print("\n暂无长期记忆。")
        print(
            "记忆只在 bot 运行期间累计：同一聊天攒够 "
            f"{loaded.config.memory.extraction_threshold} 条消息才会抽取一次。"
        )
        return 0

    print(f"\n共 {len(rows)} 条长期记忆（最近的在最前）：")
    for index, row in enumerate(rows, start=1):
        expiry = f" / 到期 {_local_time(row['expires_at'])}" if row["expires_at"] else ""
        print(
            f"{index:>3}. [{row['user_id']}] {row['category']:<12} {row['content']}\n"
            f"     scope={row['scope']} 重要度={row['importance']:.2f} "
            f"置信度={row['confidence']:.2f} 更新于 {_local_time(row['updated_at'])}{expiry}"
        )
    print(
        "\n提示：user_id 是「账号+聊天名+昵称」的稳定哈希，改群名/昵称后会变成另一个 id。"
    )
    return 0


def _check_web_search(loaded, args) -> int:
    config = loaded.config.web_search
    try:
        payload = asyncio.run(search_web(config, args.check_web_search))
    except Exception as exc:
        logger.error("web search failed: %s", exc)
        return 1
    print(format_results(payload, max_results=config.max_results))
    return 0


def _local_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


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
