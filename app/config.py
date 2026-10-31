"""Configuration loading and validation.

Instance YAML files contain *behaviour* configuration; persona prompts live in
separate Markdown files so adding a character does not require code changes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import ConfigurationError

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelConfig(StrictModel):
    provider: Literal["deepseek"] = "deepseek"
    model_name: str = Field(default="deepseek-chat", min_length=1, max_length=100)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    api_key_env: str = Field(default="DEEPSEEK_API_KEY", min_length=1, max_length=100)
    api_base_env: str | None = Field(default=None, max_length=100)

    @field_validator("api_key_env")
    @classmethod
    def _valid_api_key_env(cls, value: str) -> str:
        if not _ENV_RE.match(value):
            raise ValueError("api_key_env must be a valid environment variable name")
        return value

    @field_validator("api_base_env")
    @classmethod
    def _valid_api_base_env(cls, value: str | None) -> str | None:
        if value is not None and not _ENV_RE.match(value):
            raise ValueError("api_base_env must be a valid environment variable name")
        return value


class WeChatChatConfig(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["direct", "group"] = "group"
    # Per-chat override for wechat.image_cache_size.
    image_cache_size: int | None = Field(default=None, ge=0, le=50)


class WeChatConfig(StrictModel):
    """Single WeChat account and the chats this bot listens to."""

    account_id: str = Field(default="wechat-main", min_length=1, max_length=64)
    chats: list[WeChatChatConfig] = Field(min_length=1)
    poll_interval_seconds: float = Field(default=1.0, ge=0.2, le=30.0)
    # Random +/- ratio applied to every poll interval so window switches do not
    # land on a perfectly regular beat.
    poll_jitter_ratio: float = Field(default=0.35, ge=0.0, le=0.9)
    # After this many polls the account "walks away" for a random pause, which
    # is what a human-driven client looks like over a whole day.
    poll_burst_size: int = Field(default=30, ge=1, le=1000)
    poll_pause_min_seconds: float = Field(default=10.0, ge=0.0, le=1800.0)
    poll_pause_max_seconds: float = Field(default=45.0, ge=0.0, le=3600.0)
    # Base delay before a send: the time the persona spends reading/typing.
    # 0 disables the whole simulated-typing delay (including per-char/jitter).
    send_delay_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    # Extra delay per character, so long replies take longer to type out.
    send_delay_per_char_seconds: float = Field(default=0.05, ge=0.0, le=1.0)
    # Uniform extra seconds on top, to avoid identical reaction times.
    send_delay_jitter_seconds: float = Field(default=1.5, ge=0.0, le=30.0)
    send_delay_max_seconds: float = Field(default=25.0, ge=0.0, le=120.0)
    # Short pause after a successful send before the next action.
    send_gap_seconds: float = Field(default=0.6, ge=0.0, le=30.0)
    bot_name: str | None = Field(default=None, max_length=100)
    text_only: bool = True
    # Download image messages and let the bot look at them. Other non-text
    # message types are still skipped.
    capture_images: bool = False
    # How long a captured image stays available for follow-up messages such as
    # "这张图里是什么". 0 deletes images as soon as they are handled.
    image_followup_seconds: float = Field(default=120.0, ge=0.0, le=900.0)
    # How many images to keep per chat (FIFO). Chats can override this.
    image_cache_size: int = Field(default=10, ge=0, le=50)
    # How many cached images to attach to one follow-up message.
    image_followup_max_images: int = Field(default=3, ge=1, le=6)

    @field_validator("chats")
    @classmethod
    def _unique_chat_names(cls, value: list[WeChatChatConfig]) -> list[WeChatChatConfig]:
        names = [chat.name for chat in value]
        if len(set(names)) != len(names):
            raise ValueError("wechat.chats names must be unique")
        return value


class WebSearchConfig(StrictModel):
    """DeepSeek Search API settings for the ``web_search`` tool."""

    max_results: int = Field(default=5, ge=1, le=10)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    include_content: bool = False
    api_base: str = Field(default="https://api.deepseek.com", min_length=1, max_length=200)
    api_key_env: str = Field(default="DEEPSEEK_API_KEY", min_length=1, max_length=100)

    @field_validator("api_base")
    @classmethod
    def _valid_api_base(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("api_base must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("api_key_env")
    @classmethod
    def _valid_api_key_env(cls, value: str) -> str:
        if not _ENV_RE.match(value):
            raise ValueError("api_key_env must be a valid environment variable name")
        return value


class MemoryConfig(StrictModel):
    enabled: bool = True
    allow_shared_memory: bool = False
    extraction_threshold: int = Field(default=5, ge=2, le=100)
    extraction_batch_size: int = Field(default=20, ge=2, le=100)
    extraction_max_attempts: int = Field(default=3, ge=1, le=10)
    extraction_timeout_seconds: float = Field(default=90.0, gt=0.0, le=600.0)
    # Keep messages waiting for extraction on disk so a restart does not throw
    # away progress towards extraction_threshold.
    persist_pending: bool = True
    pending_max_per_channel: int = Field(default=200, ge=10, le=2000)
    max_memories_per_search: int = Field(default=5, ge=1, le=20)
    max_memory_chars: int = Field(default=500, ge=20, le=2000)
    min_importance: float = Field(default=0.4, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    max_candidates_per_batch: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _threshold_fits_batch(self) -> "MemoryConfig":
        if self.extraction_threshold > self.extraction_batch_size:
            raise ValueError("extraction_threshold cannot exceed extraction_batch_size")
        return self


class AgentConfig(StrictModel):
    max_tool_rounds: int = Field(default=3, ge=1, le=20)
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)


class ReplyConfig(StrictModel):
    mention_priority: bool = True
    channel_cooldown_seconds: float = Field(default=20.0, ge=0.0, le=3600.0)
    user_cooldown_seconds: float = Field(default=10.0, ge=0.0, le=3600.0)
    max_reply_length: int = Field(default=1800, ge=50, le=2000)
    claim_message_across_instances: bool = True
    message_claim_ttl_seconds: float = Field(default=120.0, gt=0.0, le=3600.0)
    # 0 disables continuation handling. When >0, a user who was just answered can
    # continue the conversation without losing the turn to cooldowns/probability.
    continuation_window_seconds: float = Field(default=0.0, ge=0.0, le=3600.0)
    continuation_channel_cooldown_seconds: float = Field(default=2.0, ge=0.0, le=300.0)
    continuation_user_cooldown_seconds: float = Field(default=1.0, ge=0.0, le=300.0)
    # A message dropped only because of a cooldown is judged once more after the
    # cooldown expires, so one chatty user cannot starve the rest of the chat.
    retry_on_cooldown: bool = True
    max_cooldown_retry_seconds: float = Field(default=15.0, ge=0.0, le=300.0)
    # Simulate a person typing: long replies are sent as two or three separate
    # bubbles with a random pause in between instead of one wall of text.
    # Split points come from the model's own ``<split/>`` markers; these knobs
    # tune the punctuation-based fallback used when it emitted none.
    split_replies: bool = True
    split_min_chars: int = Field(default=160, ge=20, le=2000)
    split_max_parts: int = Field(default=3, ge=2, le=6)
    split_probability: float = Field(default=0.8, ge=0.0, le=1.0)
    split_gap_min_seconds: float = Field(default=0.8, ge=0.0, le=30.0)
    split_gap_max_seconds: float = Field(default=3.0, ge=0.0, le=60.0)
    empty_reply_fallback: str = Field(
        default="（不知道该说什么……先听你们聊。）",
        max_length=200,
    )


class ContextConfig(StrictModel):
    max_messages: int = Field(default=30, ge=5, le=200)
    merge_window_seconds: float = Field(default=3.0, ge=0.0, le=60.0)
    include_other_bots: bool = False


class ParticipationConfig(StrictModel):
    enabled: bool = True
    max_context_messages: int = Field(default=15, ge=3, le=50)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)
    default_reply_on_error: bool = False
    # Applied only to non-mention messages that the judge approved. 1.0 means
    # always reply when approved; 0.0 means never reply without a mention.
    reply_probability: float = Field(default=1.0, ge=0.0, le=1.0)
    # The judge also scores how much this persona would care about the topic.
    # Messages scoring below this are dropped even when the judge said "reply",
    # which keeps the bot out of conversations a person would not join.
    min_interest: float = Field(default=0.0, ge=0.0, le=1.0)
    # Topic-level throttle: for this many seconds after a reply, other people
    # talking about the same thing only get through with a low probability, so
    # one topic gets roughly one reply instead of one per message.
    topic_window_seconds: float = Field(default=0.0, ge=0.0, le=3600.0)
    topic_window_reply_probability: float = Field(default=0.15, ge=0.0, le=1.0)
    # If a message @s someone else but not this bot, stay out of the conversation.
    ignore_other_mentions: bool = True
    # Treat other bots as users: let their messages reach the participation judge
    # and possibly trigger a reply. Self-authored messages are always ignored.
    reply_to_other_bots: bool = False
    bot_reply_probability: float = Field(default=0.1, ge=0.0, le=1.0)
    allow_consecutive_bot_messages: bool = False


class InstanceConfig(StrictModel):
    instance_id: str
    persona_id: str
    database_path: str = "./data/bot.db"
    wechat: WeChatConfig
    tools: list[str] = Field(default_factory=lambda: ["search_memory", "get_user_profile"])
    model: ModelConfig = Field(default_factory=ModelConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    participation: ParticipationConfig = Field(default_factory=ParticipationConfig)

    @field_validator("instance_id", "persona_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(
                "ids must be 2-32 chars, lowercase letters/digits/_/-, starting alphanumeric"
            )
        return value

    @field_validator("tools")
    @classmethod
    def _unique_tools(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("tool names cannot be empty")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("tools must be unique")
        return cleaned


class Persona(StrictModel):
    persona_id: str
    prompt: str = Field(min_length=20)
    path: Path


class LoadedInstance(StrictModel):
    config: InstanceConfig
    persona: Persona
    database_path: Path
    config_path: Path


def default_project_root() -> Path:
    return _DEFAULT_PROJECT_ROOT


def default_config_dir(project_root: Path | None = None) -> Path:
    return (project_root or default_project_root()) / "config"


def _load_yaml(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"instance config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"instance config must be a mapping: {path}")
    return raw


def load_persona(
    persona_id: str,
    *,
    config_dir: Path | None = None,
    project_root: Path | None = None,
) -> Persona:
    root = project_root or default_project_root()
    directory = config_dir or default_config_dir(root)
    path = directory / "personas" / f"{persona_id}.md"
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigurationError(f"persona prompt not found: {path}") from exc
    if len(prompt) < 20:
        raise ConfigurationError(f"persona prompt is too short: {path}")
    return Persona(persona_id=persona_id, prompt=prompt, path=path)


def resolve_database_path(path_value: str, project_root: Path | None = None) -> Path:
    root = project_root or default_project_root()
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def load_instance(
    instance_id: str,
    *,
    config_dir: Path | None = None,
    project_root: Path | None = None,
) -> LoadedInstance:
    root = (project_root or default_project_root()).resolve()
    directory = (config_dir or default_config_dir(root)).resolve()
    path = directory / "instances" / f"{instance_id}.yaml"
    raw = _load_yaml(path)
    try:
        config = InstanceConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigurationError(f"invalid instance config {path}: {exc}") from exc

    if config.instance_id != instance_id:
        raise ConfigurationError(
            f"instance_id '{config.instance_id}' does not match file name '{instance_id}'"
        )

    persona = load_persona(config.persona_id, config_dir=directory, project_root=root)
    if config.memory.enabled:
        requested = set(config.tools)
        if not requested.intersection({"search_memory", "get_user_profile"}):
            raise ConfigurationError(
                f"{instance_id}: memory is enabled but no memory/profile tool is configured"
            )
    return LoadedInstance(
        config=config,
        persona=persona,
        database_path=resolve_database_path(config.database_path, root),
        config_path=path,
    )


def list_instances(*, config_dir: Path | None = None) -> list[str]:
    directory = (config_dir or default_config_dir()) / "instances"
    if not directory.exists():
        return []
    return sorted(path.stem for path in directory.glob("*.yaml"))


def load_dotenv_if_present(project_root: Path | None = None) -> None:
    """Load .env without overriding variables already set by the process."""

    from dotenv import load_dotenv

    load_dotenv((project_root or default_project_root()) / ".env", override=False)
