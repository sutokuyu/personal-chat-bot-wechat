from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.deepseek_model import assert_tool_calling_model, build_chat_model
from app.config import (
    ConfigurationError,
    ModelConfig,
    load_instance,
    load_persona,
)
from app.tools.registry import validate_tool_names


PERSONA = "# Persona\n\nA sufficiently long test persona prompt for validation."


def _write_tree(tmp_path: Path, *, instance_id: str = "test", overrides: str = "") -> Path:
    config_dir = tmp_path / "config"
    (config_dir / "instances").mkdir(parents=True)
    (config_dir / "personas").mkdir(parents=True)
    (config_dir / "personas" / f"{instance_id}.md").write_text(PERSONA, encoding="utf-8")
    (config_dir / "instances" / f"{instance_id}.yaml").write_text(
        f"""
instance_id: {instance_id}
persona_id: {instance_id}
database_path: ./data/test.db
wechat:
  account_id: test-account
  chats:
    - name: "文件传输助手"
      kind: direct
model:
  model_name: deepseek-chat
{overrides}
""",
        encoding="utf-8",
    )
    return config_dir


def test_load_wechat_instance(tmp_path):
    config_dir = _write_tree(tmp_path)
    loaded = load_instance("test", config_dir=config_dir, project_root=tmp_path)
    assert loaded.config.wechat.account_id == "test-account"
    assert loaded.config.wechat.chats[0].name == "文件传输助手"
    assert loaded.config.wechat.chats[0].kind == "direct"
    assert loaded.database_path == (tmp_path / "data" / "test.db").resolve()


def test_instance_id_must_match_file_name(tmp_path):
    config_dir = _write_tree(tmp_path)
    source = config_dir / "instances" / "test.yaml"
    (config_dir / "instances" / "other.yaml").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="does not match"):
        load_instance("other", config_dir=config_dir, project_root=tmp_path)


def test_unknown_keys_and_missing_wechat_are_rejected(tmp_path):
    config_dir = _write_tree(tmp_path, overrides="  surprise: true")
    with pytest.raises(ConfigurationError, match="invalid instance config"):
        load_instance("test", config_dir=config_dir, project_root=tmp_path)

    config_dir2 = tmp_path / "missing-wechat" / "config"
    (config_dir2 / "instances").mkdir(parents=True)
    (config_dir2 / "personas").mkdir(parents=True)
    (config_dir2 / "personas" / "test.md").write_text(PERSONA, encoding="utf-8")
    (config_dir2 / "instances" / "test.yaml").write_text(
        "instance_id: test\npersona_id: test\n", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="invalid instance config"):
        load_instance("test", config_dir=config_dir2, project_root=tmp_path)


def test_duplicate_chat_names_are_rejected(tmp_path):
    config_dir = _write_tree(
        tmp_path,
        overrides="""
wechat:
  account_id: test-account
  chats:
    - name: "群"
      kind: group
    - name: "群"
      kind: group
""",
    )
    with pytest.raises(ConfigurationError, match="unique"):
        load_instance("test", config_dir=config_dir, project_root=tmp_path)


def test_missing_persona_file_is_reported(tmp_path):
    config_dir = _write_tree(tmp_path)
    (config_dir / "personas" / "test.md").unlink()
    with pytest.raises(ConfigurationError, match="persona prompt not found"):
        load_instance("test", config_dir=config_dir, project_root=tmp_path)


def test_short_persona_is_rejected(tmp_path):
    config_dir = _write_tree(tmp_path)
    (config_dir / "personas" / "test.md").write_text("too short", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="too short"):
        load_persona("test", config_dir=config_dir, project_root=tmp_path)


def test_tool_registry_rejects_unknown_names(tmp_path):
    loaded = load_instance(
        "deepseek-chan",
        config_dir=Path(__file__).resolve().parent.parent / "config",
        project_root=Path(__file__).resolve().parent.parent,
    )
    bad = loaded.config.model_copy(update={"tools": ["search_memory", "nope"]})
    with pytest.raises(ConfigurationError, match="unknown tools"):
        validate_tool_names(bad)

    disabled_memory = loaded.config.model_copy(
        update={
            "tools": ["search_memory"],
            "memory": loaded.config.memory.model_copy(update={"enabled": False}),
        }
    )
    with pytest.raises(ConfigurationError, match="memory is disabled"):
        validate_tool_names(disabled_memory)


def test_reasoner_model_is_refused_for_tool_calling():
    with pytest.raises(ConfigurationError, match="deepseek-reasoner"):
        assert_tool_calling_model(ModelConfig(model_name="deepseek-reasoner"))


def test_chat_model_uses_configured_api_key_env():
    config = ModelConfig(api_key_env="DEEPSEEK_API_KEY_WECHAT")
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY_WECHAT"):
        build_chat_model(config, env={})
    model = build_chat_model(
        config, env={"DEEPSEEK_API_KEY_WECHAT": "dummy-key"}
    )
    assert model.api_key.get_secret_value() == "dummy-key"


def test_example_wechat_instance_is_valid():
    root = Path(__file__).resolve().parent.parent
    loaded = load_instance(
        "deepseek-chan", config_dir=root / "config", project_root=root
    )
    assert loaded.persona.persona_id == "deepseek-chan"
    assert loaded.config.wechat.account_id == "wechat-main"
    assert loaded.config.wechat.chats
    assert all(chat.name.strip() for chat in loaded.config.wechat.chats)
    assert all(chat.kind in {"direct", "group"} for chat in loaded.config.wechat.chats)
    assert loaded.config.model.api_key_env == "DEEPSEEK_API_KEY"
