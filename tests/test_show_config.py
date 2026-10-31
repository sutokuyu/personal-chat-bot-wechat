from __future__ import annotations

from pathlib import Path

from app.config import config_overrides, effective_config, load_instance
from app.main import main

ROOT = Path(__file__).resolve().parent.parent


def _loaded():
    return load_instance("deepseek-chan", config_dir=ROOT / "config", project_root=ROOT)


def test_effective_config_merges_instance_over_defaults():
    data = effective_config(_loaded())

    assert data["wechat"]["active_hours_start"] == "08:00"
    assert data["reply"]["channel_cooldown_seconds"] == 45.0
    assert data["context"]["max_messages"] == 30
    assert data["_resolved"]["database_path"].endswith("bot.db")


def test_overrides_only_list_values_that_differ_from_defaults():
    overrides = config_overrides(_loaded())

    assert overrides["reply.max_reply_length"] == 450
    assert overrides["wechat.active_hours_start"] == "08:00"
    assert "reply.split_probability" not in overrides
    assert "context.merge_window_seconds" not in overrides


def test_show_config_prints_one_resolved_document(capsys):
    assert main(["--show-config", "--no-log-file"]) == 0

    out = capsys.readouterr().out
    assert "active_hours_start: 08:00" in out
    assert "channel_cooldown_seconds: 45.0" in out
    assert "settings that differ from the code defaults" in out


def test_show_config_never_prints_secret_values(monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-appear")

    assert main(["--show-config", "--no-log-file"]) == 0

    assert "sk-should-not-appear" not in capsys.readouterr().out
