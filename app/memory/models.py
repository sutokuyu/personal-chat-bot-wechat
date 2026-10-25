"""Pydantic models for memory, profiles, and trusted request context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils import parse_iso, utcnow


class MemoryScope(str, Enum):
    PERSONA = "persona"
    SHARED = "shared"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"
    EXPIRED = "expired"


class MemoryCategory(str, Enum):
    PREFERENCE = "preference"
    EXPERIENCE = "experience"
    PLAN = "plan"
    STATE = "state"
    RELATIONSHIP = "relationship"
    OTHER = "other"


class MemoryOperation(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    IGNORE = "IGNORE"


@dataclass(frozen=True, slots=True)
class TrustedContext:
    """Identity derived by Python from a WeChat message, never from the LLM."""

    instance_id: str
    persona_id: str
    guild_id: str
    channel_id: str
    user_id: str
    user_name: str
    message_id: str
    allow_shared_memory: bool = False

    def for_user(self, user_id: str, user_name: str | None = None) -> "TrustedContext":
        return TrustedContext(
            instance_id=self.instance_id,
            persona_id=self.persona_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            user_id=user_id,
            user_name=user_name or user_id,
            message_id=self.message_id,
            allow_shared_memory=self.allow_shared_memory,
        )


class UserProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    guild_id: str
    user_id: str
    persona_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    interaction_count: int
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "first_seen_at", "last_seen_at", "created_at", "updated_at", mode="before"
    )
    @classmethod
    def _parse_dt(cls, value: Any) -> Any:
        return parse_iso(value) if isinstance(value, str) else value


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    guild_id: str
    user_id: str
    scope: MemoryScope
    persona_id: str | None
    category: MemoryCategory
    content: str
    content_hash: str
    importance: float
    confidence: float
    source_message_id: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    status: MemoryStatus

    @field_validator(
        "created_at", "updated_at", "expires_at", mode="before"
    )
    @classmethod
    def _parse_dt(cls, value: Any) -> Any:
        return parse_iso(value) if isinstance(value, str) else value

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or utcnow())


class MemoryCandidate(BaseModel):
    """One candidate produced by the extraction model.

    Conditional requirements are intentionally validated in Python business
    code as well, so a malformed model response cannot become a write.
    """

    model_config = ConfigDict(extra="ignore")

    operation: MemoryOperation
    user_id: str | None = Field(default=None, max_length=32)
    scope: MemoryScope = MemoryScope.PERSONA
    category: MemoryCategory = MemoryCategory.OTHER
    content: str = Field(default="", max_length=2000)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    target_memory_id: int | None = Field(default=None, ge=1)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)
    source_message_id: str | None = Field(default=None, max_length=32)
    reason: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _conditional_fields(self) -> "MemoryCandidate":
        if self.operation is not MemoryOperation.IGNORE:
            if not self.content.strip():
                raise ValueError("content is required for ADD/UPDATE")
            if not self.user_id:
                raise ValueError("user_id is required for ADD/UPDATE")
        if self.operation is MemoryOperation.UPDATE and self.target_memory_id is None:
            raise ValueError("target_memory_id is required for UPDATE")
        if self.operation is MemoryOperation.ADD and self.target_memory_id is not None:
            raise ValueError("target_memory_id must not be set for ADD")
        return self

    @field_validator("operation", mode="before")
    @classmethod
    def _normalize_operation(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("scope", "category", mode="before")
    @classmethod
    def _normalize_enum_text(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @field_validator("user_id", "source_message_id", mode="before")
    @classmethod
    def _stringify_ids(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int) else value


class MemoryExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=300)


class ApplyReport(BaseModel):
    added: int = 0
    updated: int = 0
    ignored: int = 0
    rejected: int = 0
    rejected_reasons: list[str] = Field(default_factory=list)

    def merge(self, other: "ApplyReport") -> "ApplyReport":
        return ApplyReport(
            added=self.added + other.added,
            updated=self.updated + other.updated,
            ignored=self.ignored + other.ignored,
            rejected=self.rejected + other.rejected,
            rejected_reasons=(self.rejected_reasons + other.rejected_reasons)[:20],
        )
