"""Central memory access policy.

Every read and write that touches the ``memories`` table goes through these
rules. The LLM can request a memory tool, but this module -- not the prompt --
decides whether the current persona may see or change a row.
"""

from __future__ import annotations

from app.errors import MemoryAccessDenied
from app.memory.models import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    TrustedContext,
)
from app.utils import utcnow


class MemoryAccessPolicy:
    def __init__(self, *, shared_memory_enabled: bool = False) -> None:
        self.shared_memory_enabled = shared_memory_enabled

    def _shared_allowed(self, ctx: TrustedContext) -> bool:
        return self.shared_memory_enabled and ctx.allow_shared_memory

    def can_read(self, ctx: TrustedContext, memory: MemoryRecord) -> bool:
        if memory.status is not MemoryStatus.ACTIVE:
            return False
        if memory.is_expired():
            return False
        if memory.guild_id != ctx.guild_id or memory.user_id != ctx.user_id:
            return False
        if memory.scope is MemoryScope.PERSONA:
            return memory.persona_id == ctx.persona_id
        return memory.scope is MemoryScope.SHARED and self._shared_allowed(ctx)

    def can_write_scope(self, ctx: TrustedContext, scope: MemoryScope) -> bool:
        if scope is MemoryScope.PERSONA:
            return True
        return scope is MemoryScope.SHARED and self._shared_allowed(ctx)

    def can_modify(self, ctx: TrustedContext, memory: MemoryRecord) -> bool:
        if memory.guild_id != ctx.guild_id or memory.user_id != ctx.user_id:
            return False
        if memory.scope is MemoryScope.PERSONA:
            return memory.persona_id == ctx.persona_id
        return memory.scope is MemoryScope.SHARED and self._shared_allowed(ctx)

    def require_read(self, ctx: TrustedContext, memory: MemoryRecord) -> None:
        if not self.can_read(ctx, memory):
            raise MemoryAccessDenied(
                f"persona {ctx.persona_id!r} may not read memory {memory.id}"
            )

    def require_modify(self, ctx: TrustedContext, memory: MemoryRecord) -> None:
        if not self.can_modify(ctx, memory):
            raise MemoryAccessDenied(
                f"persona {ctx.persona_id!r} may not modify memory {memory.id}"
            )

    def require_write_scope(self, ctx: TrustedContext, scope: MemoryScope) -> None:
        if not self.can_write_scope(ctx, scope):
            raise MemoryAccessDenied(
                f"persona {ctx.persona_id!r} may not write scope {scope.value!r}"
            )
