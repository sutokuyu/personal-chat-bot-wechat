"""Trusted runtime context injected into LangChain tools.

The context is created by Python for each WeChat message and passed through
``create_agent(..., context_schema=AgentRuntimeContext)``. Tool parameters that
come from the model never contain user ids or guild ids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.memory.models import TrustedContext

if TYPE_CHECKING:
    from app.memory.access_policy import MemoryAccessPolicy
    from app.memory.repository import MemoryRepository


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    instance_id: str
    persona_id: str
    guild_id: str
    channel_id: str
    user_id: str
    user_name: str
    message_id: str
    allow_shared_memory: bool
    repository: "MemoryRepository"
    policy: "MemoryAccessPolicy"
    logger: logging.Logger

    def trusted(self) -> TrustedContext:
        return TrustedContext(
            instance_id=self.instance_id,
            persona_id=self.persona_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            user_id=self.user_id,
            user_name=self.user_name,
            message_id=self.message_id,
            allow_shared_memory=self.allow_shared_memory,
        )
