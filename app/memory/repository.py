"""Repository layer: all SQL lives here.

Public read/write methods take a :class:`TrustedContext` and enforce the central
:class:`MemoryAccessPolicy`; the LLM cannot pass a user id to widen its scope.
A small state repository stores cooldowns and extraction cursors in ``bot_state``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from app.errors import MemoryValidationError
from app.memory.access_policy import MemoryAccessPolicy
from app.memory.models import (
    MemoryCategory,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    TrustedContext,
    UserProfile,
)
from app.storage.database import Database
from app.utils import (
    expires_in,
    isoformat,
    normalize_text,
    parse_iso,
    tokenize,
    truncate,
    utcnow,
)

logger = logging.getLogger(__name__)

_ACTIVE = MemoryStatus.ACTIVE.value
_UNSET = object()
_CATEGORY_WEIGHT = {
    "preference": 0.05,
    "experience": 0.05,
    "plan": 0.05,
    "state": 0.04,
    "relationship": 0.04,
    "other": 0.02,
}


def content_fingerprint(content: str) -> str:
    return hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest()


class MemoryRepository:
    def __init__(
        self,
        database: Database,
        policy: MemoryAccessPolicy,
        *,
        max_search_scan: int = 500,
    ) -> None:
        self.db = database
        self.policy = policy
        self.max_search_scan = max_search_scan

    # ------------------------------------------------------------------ profiles
    def upsert_user_profile(self, ctx: TrustedContext) -> UserProfile:
        now = isoformat(utcnow())

        def operation() -> UserProfile:
            with self.db.transaction(write=True) as conn:
                conn.execute(
                    """
                    INSERT INTO user_profiles
                        (guild_id, user_id, persona_id, first_seen_at, last_seen_at,
                         interaction_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT (guild_id, user_id, persona_id) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        interaction_count = user_profiles.interaction_count + 1,
                        updated_at = excluded.updated_at
                    """,
                    (ctx.guild_id, ctx.user_id, ctx.persona_id, now, now, now, now),
                )
                row = conn.execute(
                    """
                    SELECT * FROM user_profiles
                    WHERE guild_id = ? AND user_id = ? AND persona_id = ?
                    """,
                    (ctx.guild_id, ctx.user_id, ctx.persona_id),
                ).fetchone()
            if row is None:
                raise MemoryValidationError("failed to upsert user profile")
            return UserProfile.model_validate(dict(row))

        return self.db.write(operation)

    def get_user_profile(self, ctx: TrustedContext) -> UserProfile | None:
        row = self.db.read(
            lambda conn: conn.execute(
                """
                SELECT * FROM user_profiles
                WHERE guild_id = ? AND user_id = ? AND persona_id = ?
                """,
                (ctx.guild_id, ctx.user_id, ctx.persona_id),
            ).fetchone()
        )
        return UserProfile.model_validate(dict(row)) if row else None

    # ------------------------------------------------------------------- memory
    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord.model_validate(dict(row))

    def get_memory(self, ctx: TrustedContext, memory_id: int) -> MemoryRecord | None:
        row = self.db.read(
            lambda conn: conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        )
        if row is None:
            return None
        memory = self._row_to_memory(row)
        if not self.policy.can_read(ctx, memory):
            return None
        return memory

    def search_memories(
        self,
        ctx: TrustedContext,
        query: str,
        *,
        limit: int = 5,
        max_chars: int = 500,
    ) -> list[MemoryRecord]:
        query = query.strip()
        if len(query) > 300:
            query = query[:300]
        now_iso = isoformat(utcnow())
        clauses = [
            "guild_id = ?",
            "user_id = ?",
            "status = ?",
            "(expires_at IS NULL OR expires_at > ?)",
        ]
        params: list[Any] = [ctx.guild_id, ctx.user_id, _ACTIVE, now_iso]
        scope_parts = ["(scope = 'persona' AND persona_id = ?)"]
        params.append(ctx.persona_id)
        if self.policy.shared_memory_enabled and ctx.allow_shared_memory:
            scope_parts.append("(scope = 'shared' AND persona_id IS NULL)")
        clauses.append("(" + " OR ".join(scope_parts) + ")")
        sql = (
            "SELECT * FROM memories WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(self.max_search_scan)

        rows = self.db.read(
            lambda conn: conn.execute(sql, tuple(params)).fetchall()
        )
        memories: list[MemoryRecord] = []
        for row in rows:
            memory = self._row_to_memory(row)
            if self.policy.can_read(ctx, memory):
                memories.append(memory)
        query_tokens = tokenize(query)
        now = utcnow()
        scored: list[tuple[float, MemoryRecord]] = []
        for memory in memories:
            content_tokens = tokenize(memory.content)
            if query_tokens:
                overlap = len(query_tokens & content_tokens) / len(query_tokens)
                if overlap <= 0.0:
                    continue
            else:
                overlap = 0.0
            age_days = max(0.0, (now - memory.updated_at).total_seconds() / 86400)
            recency = max(0.0, 1.0 - min(age_days / 365.0, 1.0))
            category_weight = _CATEGORY_WEIGHT.get(memory.category.value, 0.0)
            score = (
                0.55 * overlap
                + 0.25 * memory.importance
                + 0.15 * recency
                + category_weight
            )
            scored.append((score, memory))
        scored.sort(key=lambda pair: (pair[0], pair[1].importance, pair[1].updated_at), reverse=True)

        results: list[MemoryRecord] = []
        for _score, memory in scored[: max(1, limit)]:
            if len(memory.content) > max_chars:
                memory = memory.model_copy(update={"content": truncate(memory.content, max_chars)})
            results.append(memory)
        return results

    def find_duplicates(
        self,
        ctx: TrustedContext,
        *,
        scope: MemoryScope,
        persona_id: str | None,
        category: str,
        content: str,
        threshold: float = 0.86,
        scan_limit: int = 200,
    ) -> list[MemoryRecord]:
        if scope is MemoryScope.PERSONA:
            persona_clause = "persona_id = ?"
            persona_params: tuple[Any, ...] = (persona_id,)
        else:
            persona_clause = "persona_id IS NULL"
            persona_params = ()
        sql = f"""
            SELECT * FROM memories
            WHERE guild_id = ? AND user_id = ? AND status = ?
              AND scope = ? AND category = ? AND {persona_clause}
            ORDER BY updated_at DESC LIMIT ?
        """
        params = (
            ctx.guild_id,
            ctx.user_id,
            _ACTIVE,
            scope.value,
            category,
            *persona_params,
            scan_limit,
        )
        rows = self.db.read(lambda conn: conn.execute(sql, params).fetchall())
        target = normalize_text(content)
        target_tokens = tokenize(content)
        matches: list[MemoryRecord] = []
        for row in rows:
            memory = self._row_to_memory(row)
            if not self.policy.can_read(ctx, memory):
                continue
            candidate = normalize_text(memory.content)
            if candidate == target:
                matches.append(memory)
                continue
            ratio = SequenceMatcher(None, target, candidate).ratio()
            other_tokens = tokenize(memory.content)
            union = target_tokens | other_tokens
            jaccard = len(target_tokens & other_tokens) / len(union) if union else 0.0
            if max(ratio, jaccard) >= threshold:
                matches.append(memory)
        return matches

    def add_memory(
        self,
        ctx: TrustedContext,
        *,
        scope: MemoryScope,
        category: str,
        content: str,
        importance: float,
        confidence: float,
        source_message_id: str | None = None,
        ttl_days: int | None = None,
    ) -> MemoryRecord:
        content = " ".join(content.split()).strip()
        if not content:
            raise MemoryValidationError("memory content is empty")
        if len(content) > 2000:
            raise MemoryValidationError("memory content is too long")
        if not 0.0 <= importance <= 1.0:
            raise MemoryValidationError("importance must be between 0 and 1")
        if not 0.0 <= confidence <= 1.0:
            raise MemoryValidationError("confidence must be between 0 and 1")
        if ttl_days is not None and ttl_days < 1:
            raise MemoryValidationError("ttl_days must be positive")
        if category not in {item.value for item in MemoryCategory}:
            raise MemoryValidationError(f"unknown memory category: {category}")
        self.policy.require_write_scope(ctx, scope)
        persona_id = ctx.persona_id if scope is MemoryScope.PERSONA else None
        now = utcnow()
        now_iso = isoformat(now)
        expires_at = expires_in(ttl_days, now=now)
        fingerprint = content_fingerprint(content)

        def operation() -> MemoryRecord:
            with self.db.transaction(write=True) as conn:
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO memories
                            (guild_id, user_id, scope, persona_id, category, content,
                             content_hash, importance, confidence, source_message_id,
                             created_at, updated_at, expires_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                        """,
                        (
                            ctx.guild_id,
                            ctx.user_id,
                            scope.value,
                            persona_id,
                            category,
                            content,
                            fingerprint,
                            importance,
                            confidence,
                            source_message_id,
                            now_iso,
                            now_iso,
                            expires_at,
                        ),
                    )
                    memory_id = int(cursor.lastrowid)
                except sqlite3.IntegrityError:
                    row = conn.execute(
                        """
                        SELECT * FROM memories
                        WHERE guild_id = ? AND user_id = ? AND scope = ?
                          AND COALESCE(persona_id, '') = COALESCE(?, '')
                          AND category = ? AND content_hash = ? AND status = 'active'
                        """,
                        (
                            ctx.guild_id,
                            ctx.user_id,
                            scope.value,
                            persona_id,
                            category,
                            fingerprint,
                        ),
                    ).fetchone()
                    if row is None:
                        raise
                    return self._row_to_memory(row)
                row = conn.execute(
                    "SELECT * FROM memories WHERE id = ?", (memory_id,)
                ).fetchone()
            assert row is not None
            return self._row_to_memory(row)

        return self.db.write(operation)

    def update_memory(
        self,
        ctx: TrustedContext,
        memory_id: int,
        *,
        content: str | None = None,
        category: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        ttl_days: int | None | object = _UNSET,
        source_message_id: str | None = None,
    ) -> MemoryRecord:
        current = self.get_memory(ctx, memory_id)
        if current is None:
            raise MemoryValidationError(f"memory {memory_id} does not exist in scope")
        self.policy.require_modify(ctx, current)

        fields: dict[str, Any] = {"updated_at": isoformat(utcnow())}
        if content is not None:
            cleaned = " ".join(content.split()).strip()
            if not cleaned:
                raise MemoryValidationError("memory content is empty")
            fields["content"] = cleaned
            fields["content_hash"] = content_fingerprint(cleaned)
        if category is not None:
            if category not in {item.value for item in MemoryCategory}:
                raise MemoryValidationError(f"unknown memory category: {category}")
            fields["category"] = category
        if importance is not None:
            fields["importance"] = max(0.0, min(1.0, float(importance)))
        if confidence is not None:
            fields["confidence"] = max(0.0, min(1.0, float(confidence)))
        if ttl_days is not _UNSET:
            fields["expires_at"] = (
                expires_in(int(ttl_days)) if ttl_days is not None else None
            )
        if source_message_id is not None:
            fields["source_message_id"] = source_message_id

        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = tuple(fields.values()) + (memory_id,)

        def operation() -> MemoryRecord:
            with self.db.transaction(write=True) as conn:
                try:
                    conn.execute(
                        f"UPDATE memories SET {assignments} WHERE id = ?", values
                    )
                except sqlite3.IntegrityError as exc:
                    raise MemoryValidationError(
                        "update would duplicate another active memory"
                    ) from exc
                row = conn.execute(
                    "SELECT * FROM memories WHERE id = ?", (memory_id,)
                ).fetchone()
            if row is None:
                raise MemoryValidationError(f"memory {memory_id} disappeared")
            return self._row_to_memory(row)

        return self.db.write(operation)

    def delete_memory(self, ctx: TrustedContext, memory_id: int, *, hard: bool = False) -> bool:
        current = self.get_memory(ctx, memory_id)
        if current is None:
            return False
        self.policy.require_modify(ctx, current)

        def operation() -> bool:
            with self.db.transaction(write=True) as conn:
                if hard:
                    cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
                else:
                    cursor = conn.execute(
                        "UPDATE memories SET status = 'deleted', updated_at = ? WHERE id = ?",
                        (isoformat(utcnow()), memory_id),
                    )
                return cursor.rowcount > 0

        return self.db.write(operation)

    def expire_memories(self, *, now: datetime | None = None) -> int:
        now_iso = isoformat(now or utcnow())

        def operation() -> int:
            with self.db.transaction(write=True) as conn:
                cursor = conn.execute(
                    """
                    UPDATE memories SET status = 'expired', updated_at = ?
                    WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
                    """,
                    (now_iso, now_iso),
                )
                return cursor.rowcount

        return self.db.write(operation)

    def purge_user_memories(
        self,
        *,
        guild_id: str,
        user_id: str,
        persona_id: str | None = None,
        include_shared: bool = False,
    ) -> int:
        """Explicit administrator operation. Never exposed as an LLM tool."""

        clauses = ["guild_id = ?", "user_id = ?"]
        params: list[Any] = [guild_id, user_id]
        scope_parts = ["(scope = 'persona' AND persona_id = ?)"]
        params.append(persona_id or "")
        if include_shared:
            scope_parts.append("scope = 'shared'")
        clauses.append("(" + " OR ".join(scope_parts) + ")")

        def operation() -> int:
            with self.db.transaction(write=True) as conn:
                cursor = conn.execute(
                    "DELETE FROM memories WHERE " + " AND ".join(clauses), tuple(params)
                )
                return cursor.rowcount

        return self.db.write(operation)

    def count_active_memories(
        self, *, guild_id: str | None = None, user_id: str | None = None
    ) -> int:
        clauses = ["status = 'active'"]
        params: list[Any] = []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        row = self.db.read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE " + " AND ".join(clauses),
                tuple(params),
            ).fetchone()
        )
        return int(row["n"]) if row else 0


class BotStateRepository:
    """Generic persistent state, scoped by instance and chat/user ids."""

    def __init__(self, database: Database) -> None:
        self.db = database

    def get_value(
        self,
        instance_id: str,
        key: str,
        *,
        guild_id: str = "",
        channel_id: str = "",
        user_id: str = "",
    ) -> str | None:
        row = self.db.read(
            lambda conn: conn.execute(
                """
                SELECT value FROM bot_state
                WHERE instance_id = ? AND guild_id = ? AND channel_id = ?
                  AND user_id = ? AND state_key = ?
                """,
                (instance_id, guild_id, channel_id, user_id, key),
            ).fetchone()
        )
        return str(row["value"]) if row else None

    def set_value(
        self,
        instance_id: str,
        key: str,
        value: str,
        *,
        guild_id: str = "",
        channel_id: str = "",
        user_id: str = "",
    ) -> None:
        now = isoformat(utcnow())

        def operation() -> None:
            with self.db.transaction(write=True) as conn:
                conn.execute(
                    """
                    INSERT INTO bot_state
                        (instance_id, guild_id, channel_id, user_id, state_key, value, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (instance_id, guild_id, channel_id, user_id, state_key)
                    DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (instance_id, guild_id, channel_id, user_id, key, value, now),
                )

        self.db.write(operation)

    def delete_value(
        self,
        instance_id: str,
        key: str,
        *,
        guild_id: str = "",
        channel_id: str = "",
        user_id: str = "",
    ) -> None:
        def operation() -> None:
            with self.db.transaction(write=True) as conn:
                conn.execute(
                    """
                    DELETE FROM bot_state
                    WHERE instance_id = ? AND guild_id = ? AND channel_id = ?
                      AND user_id = ? AND state_key = ?
                    """,
                    (instance_id, guild_id, channel_id, user_id, key),
                )

        self.db.write(operation)

    def get_datetime(
        self,
        instance_id: str,
        key: str,
        *,
        guild_id: str = "",
        channel_id: str = "",
        user_id: str = "",
    ) -> datetime | None:
        value = self.get_value(
            instance_id, key, guild_id=guild_id, channel_id=channel_id, user_id=user_id
        )
        if not value:
            return None
        try:
            return parse_iso(value)
        except ValueError:
            return None

    def advance_extraction_cursor(
        self, instance_id: str, channel_id: str, message_id: int
    ) -> bool:
        """Atomically advance a channel cursor. Returns False if already ahead."""

        now = isoformat(utcnow())
        key = "memory_extraction_cursor"

        def operation() -> bool:
            with self.db.transaction(write=True) as conn:
                row = conn.execute(
                    """
                    SELECT value FROM bot_state
                    WHERE instance_id = ? AND guild_id = '' AND channel_id = ?
                      AND user_id = '' AND state_key = ?
                    """,
                    (instance_id, channel_id, key),
                ).fetchone()
                current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
                if current >= message_id:
                    return False
                conn.execute(
                    """
                    INSERT INTO bot_state
                        (instance_id, guild_id, channel_id, user_id, state_key, value, updated_at)
                    VALUES (?, '', ?, '', ?, ?, ?)
                    ON CONFLICT (instance_id, guild_id, channel_id, user_id, state_key)
                    DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (instance_id, channel_id, key, str(message_id), now),
                )
                return True

        return self.db.write(operation)

    def get_extraction_cursor(self, instance_id: str, channel_id: str) -> int:
        value = self.get_value(instance_id, "memory_extraction_cursor", channel_id=channel_id)
        return int(value) if value and value.isdigit() else 0

    # -------------------------------------------------- cross-instance reply claim
    _CLAIM_INSTANCE = "__shared__"
    _CLAIM_KEY = "reply_claim"

    def claim_message_reply(
        self,
        *,
        guild_id: str,
        channel_id: str,
        message_id: int,
        instance_id: str,
        ttl_seconds: float,
    ) -> bool:
        """Allow only one process to answer a given incoming message.

        A claim is short-lived and stored in the shared database. If a process
        crashes, the TTL lets another persona reply later instead of blocking
        forever.
        """

        now = utcnow()
        now_iso = isoformat(now)
        payload = json.dumps(
            {
                "instance_id": instance_id,
                "message_id": message_id,
                "claimed_at": now_iso,
                "expires_at": isoformat(now + timedelta(seconds=ttl_seconds)),
            }
        )

        def operation() -> bool:
            with self.db.transaction(write=True) as conn:
                row = conn.execute(
                    """
                    SELECT value FROM bot_state
                    WHERE instance_id = ? AND guild_id = ? AND channel_id = ?
                      AND user_id = '' AND state_key = ?
                    """,
                    (self._CLAIM_INSTANCE, guild_id, channel_id, self._CLAIM_KEY),
                ).fetchone()
                if row is not None:
                    try:
                        previous = json.loads(str(row["value"]))
                        expires_at = parse_iso(str(previous["expires_at"]))
                        same_message = int(previous["message_id"]) == message_id
                        if same_message and now < expires_at:
                            return False
                    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                        pass  # corrupt/legacy value: overwrite it
                conn.execute(
                    """
                    INSERT INTO bot_state
                        (instance_id, guild_id, channel_id, user_id, state_key, value, updated_at)
                    VALUES (?, ?, ?, '', ?, ?, ?)
                    ON CONFLICT (instance_id, guild_id, channel_id, user_id, state_key)
                    DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (
                        self._CLAIM_INSTANCE,
                        guild_id,
                        channel_id,
                        self._CLAIM_KEY,
                        payload,
                        now_iso,
                    ),
                )
                return True

        return self.db.write(operation)

    def release_message_reply(
        self,
        *,
        guild_id: str,
        channel_id: str,
        message_id: int,
        instance_id: str,
    ) -> bool:
        """Release a claim only if it still belongs to this instance/message."""

        def operation() -> bool:
            with self.db.transaction(write=True) as conn:
                row = conn.execute(
                    """
                    SELECT value FROM bot_state
                    WHERE instance_id = ? AND guild_id = ? AND channel_id = ?
                      AND user_id = '' AND state_key = ?
                    """,
                    (self._CLAIM_INSTANCE, guild_id, channel_id, self._CLAIM_KEY),
                ).fetchone()
                if row is None:
                    return False
                try:
                    previous = json.loads(str(row["value"]))
                except json.JSONDecodeError:
                    return False
                try:
                    previous_message_id = int(previous.get("message_id", -1))
                except (TypeError, ValueError):
                    return False
                if (
                    previous.get("instance_id") != instance_id
                    or previous_message_id != message_id
                ):
                    return False
                conn.execute(
                    """
                    DELETE FROM bot_state
                    WHERE instance_id = ? AND guild_id = ? AND channel_id = ?
                      AND user_id = '' AND state_key = ?
                    """,
                    (self._CLAIM_INSTANCE, guild_id, channel_id, self._CLAIM_KEY),
                )
                return True

        return self.db.write(operation)
