from __future__ import annotations

import sys

import pytest

from app import wechat_env
from app.errors import ConfigurationError
from app.wechat_env import (
    VERIFIED_WECHAT_VERSION,
    check_environment,
    is_supported_python,
    is_verified_wechat_version,
    parse_version,
)


def test_parse_version_handles_wechat_and_file_versions():
    assert parse_version("4.1.8.107") == (4, 1, 8, 107)
    assert parse_version("4.1.8.107 ") == (4, 1, 8, 107)
    assert parse_version("3.9.12.57") == (3, 9, 12, 57)
    assert parse_version(None) == ()
    assert parse_version("unknown") == ()


def test_verified_client_version_is_exact():
    assert is_verified_wechat_version(VERIFIED_WECHAT_VERSION)
    assert not is_verified_wechat_version("4.1.8.106")
    assert not is_verified_wechat_version("4.1.9.1")
    assert not is_verified_wechat_version("3.9.12.57")
    assert not is_verified_wechat_version(None)


def test_python_range_matches_project_support():
    assert is_supported_python((3, 9))
    assert is_supported_python((3, 11))
    assert is_supported_python((3, 12))
    assert not is_supported_python((3, 8))
    assert not is_supported_python((3, 13))
    assert not is_supported_python((3, 14))


def _patch_detection(monkeypatch, *, platform="win32", wechat=("4.1.8.107", r"C:\WeChat\WeChat.exe"), wxauto4="41.1.7"):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(wechat_env, "detect_wechat_installation", lambda: wechat)
    monkeypatch.setattr(wechat_env, "detect_wxauto4_version", lambda: wxauto4)


def test_check_environment_accepts_verified_client(monkeypatch):
    _patch_detection(monkeypatch)
    environment = check_environment(skip_version_check=False)
    assert environment.wechat_version == VERIFIED_WECHAT_VERSION
    assert environment.wxauto4_version == "41.1.7"
    assert "4.1.8.107" in environment.describe()


def test_check_environment_rejects_unverified_client(monkeypatch):
    _patch_detection(monkeypatch, wechat=("3.9.12.57", r"C:\Program Files\Tencent\WeChat\WeChat.exe"))
    with pytest.raises(ConfigurationError) as err:
        check_environment(skip_version_check=False)

    message = str(err.value)
    assert "当前微信版本不在本项目验证范围内" in message
    assert "3.9.12.57" in message
    assert VERIFIED_WECHAT_VERSION in message
    assert "wxauto4" in message


def test_check_environment_reports_missing_client(monkeypatch):
    _patch_detection(monkeypatch, wechat=(None, None))
    with pytest.raises(ConfigurationError) as err:
        check_environment(skip_version_check=False)
    assert "未检测到微信 PC 客户端" in str(err.value)


def test_check_environment_allows_documented_override(monkeypatch):
    _patch_detection(monkeypatch, wechat=("4.1.9.1", None))
    environment = check_environment(skip_version_check=True)
    assert environment.wechat_version == "4.1.9.1"


def test_check_environment_reads_override_from_environment(monkeypatch):
    _patch_detection(monkeypatch, wechat=("4.1.9.1", None))
    monkeypatch.setenv("WECHAT_SKIP_VERSION_CHECK", "1")
    assert check_environment().wechat_version == "4.1.9.1"


def test_check_environment_rejects_unsupported_os(monkeypatch):
    _patch_detection(monkeypatch, platform="linux")
    with pytest.raises(ConfigurationError) as err:
        check_environment()
    assert "只能在 Windows 上运行" in str(err.value)


def test_check_environment_rejects_unsupported_python(monkeypatch):
    _patch_detection(monkeypatch)
    monkeypatch.setattr(wechat_env, "is_supported_python", lambda version_info=None: False)
    with pytest.raises(ConfigurationError) as err:
        check_environment()
    assert "Python 版本" in str(err.value)


def test_check_environment_rejects_old_wxauto4(monkeypatch):
    _patch_detection(monkeypatch, wxauto4="41.0.1")
    with pytest.raises(ConfigurationError) as err:
        check_environment()
    assert "wxauto4 版本过低" in str(err.value)
