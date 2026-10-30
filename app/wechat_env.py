"""Environment checks for the WeChat 4.x / wxauto4 transport.

The bot is validated against exactly one client combination. Anything else
fails here with a readable message instead of a low-level UI-automation
traceback several layers deeper.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass

from app.errors import ConfigurationError

VERIFIED_WECHAT_VERSION = "4.1.8.107"
VERIFIED_PYTHON_MIN = (3, 9)
VERIFIED_PYTHON_MAX = (3, 12)
MIN_WXAUTO4_VERSION = (41, 1, 7)

# Documented escape hatch for users who want to try an unverified WeChat build.
# It only relaxes the client-version equality check, never the OS/Python ones.
SKIP_VERSION_CHECK_ENV = "WECHAT_SKIP_VERSION_CHECK"

_RECOMMENDATION = (
    "推荐：\n"
    f"微信 {VERIFIED_WECHAT_VERSION}\n"
    "Python 3.12\n"
    "wxauto4"
)

logger = logging.getLogger(__name__)

_PROCESS_NAMES = {"wechat.exe", "weixin.exe"}
_INSTALL_NAME_RE = re.compile(r"wechat|weixin|微信", re.IGNORECASE)
_UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


@dataclass(frozen=True, slots=True)
class WeChatEnvironment:
    platform: str
    python_version: str
    wxauto4_version: str
    wechat_version: str | None
    wechat_path: str | None

    def describe(self) -> str:
        client = f"微信 {self.wechat_version}" if self.wechat_version else "微信版本未知"
        if self.wechat_path:
            client = f"{client}（{self.wechat_path}）"
        return (
            f"{client} / Python {self.python_version} / "
            f"wxauto4 {self.wxauto4_version} / {self.platform}"
        )


def parse_version(value: object) -> tuple[int, ...]:
    """Turn ``4.1.8.107`` (or a file version string) into a comparable tuple."""

    if value is None:
        return ()
    numbers = re.findall(r"\d+", str(value))
    return tuple(int(item) for item in numbers[:4]) if numbers else ()


def is_verified_wechat_version(version: str | None) -> bool:
    return parse_version(version) == parse_version(VERIFIED_WECHAT_VERSION)


def is_supported_python(version_info: tuple[int, ...] | None = None) -> bool:
    info = tuple(version_info or sys.version_info[:2])
    return VERIFIED_PYTHON_MIN <= info <= VERIFIED_PYTHON_MAX


def detect_wxauto4_version() -> str:
    try:
        from wxauto4.param import VERSION
    except Exception as exc:  # pragma: no cover - exercised on non-Windows hosts
        raise ConfigurationError(
            "wxauto4 未安装或无法导入。\n\n"
            "请执行：\n"
            "pip install wxauto4\n\n" + _RECOMMENDATION
        ) from exc
    return str(VERSION)


def detect_wechat_installation() -> tuple[str | None, str | None]:
    """Return ``(version, executable path)`` for the desktop WeChat client.

    A running client wins over registry metadata because that is the process
    wxauto4 will attach to; the registry is used when WeChat is not running.
    """

    path = _running_wechat_path()
    if path:
        version = _file_version(path)
        if version:
            return version, path

    installed = _registry_installations()
    if path:
        return (installed[0][0] if installed else None), path
    if installed:
        return installed[0]
    return None, None


def check_environment(*, skip_version_check: bool | None = None) -> WeChatEnvironment:
    """Validate OS, Python, wxauto4 and the WeChat client version."""

    if sys.platform != "win32":
        raise ConfigurationError(
            "本项目依赖 Windows UI 自动化（wxauto4），只能在 Windows 上运行。\n\n"
            + _RECOMMENDATION
        )

    python_version = "{}.{}.{}".format(*sys.version_info[:3])
    if not is_supported_python():
        raise ConfigurationError(
            "当前 Python 版本不在本项目验证范围内。\n\n"
            f"检测到：Python {python_version}（需要 3.9–3.12）\n\n" + _RECOMMENDATION
        )

    wxauto4_version = detect_wxauto4_version()
    if parse_version(wxauto4_version) < MIN_WXAUTO4_VERSION:
        raise ConfigurationError(
            "wxauto4 版本过低。\n\n"
            f"检测到：wxauto4 {wxauto4_version}（需要 >= "
            f"{'.'.join(str(part) for part in MIN_WXAUTO4_VERSION)}）\n\n"
            "请执行：pip install -U wxauto4\n\n" + _RECOMMENDATION
        )

    wechat_version, wechat_path = detect_wechat_installation()
    environment = WeChatEnvironment(
        platform=sys.platform,
        python_version=python_version,
        wxauto4_version=wxauto4_version,
        wechat_version=wechat_version,
        wechat_path=wechat_path,
    )

    override = (
        skip_version_check
        if skip_version_check is not None
        else os.environ.get(SKIP_VERSION_CHECK_ENV) == "1"
    )
    if not is_verified_wechat_version(wechat_version):
        if override:
            logger.warning(
                "wechat_version_unverified detected=%s verified=%s reason=%s",
                wechat_version or "unknown",
                VERIFIED_WECHAT_VERSION,
                SKIP_VERSION_CHECK_ENV,
            )
            return environment
        if wechat_version:
            headline = "当前微信版本不在本项目验证范围内。"
        else:
            headline = "未检测到微信 PC 客户端。"
        raise ConfigurationError(
            f"{headline}\n\n"
            f"检测到：{environment.describe()}\n"
            f"本项目验证版本：微信 {VERIFIED_WECHAT_VERSION}\n\n"
            f"{_RECOMMENDATION}\n\n"
            f"确认要继续可以设置环境变量 {SKIP_VERSION_CHECK_ENV}=1。"
        )

    return environment


def _running_wechat_path() -> str | None:
    try:
        import psutil
    except Exception:  # pragma: no cover - psutil ships with wxauto4
        return None
    try:
        iterator = psutil.process_iter(["name", "exe"])
        for proc in iterator:
            name = str(proc.info.get("name") or "").lower()
            if name in _PROCESS_NAMES:
                exe = proc.info.get("exe")
                if exe:
                    return str(exe)
    except Exception:  # pragma: no cover - permission errors on locked-down hosts
        logger.debug("wechat_process_scan_failed", exc_info=True)
    return None


def _file_version(path: str) -> str | None:
    try:
        import win32api
    except Exception:  # pragma: no cover - pywin32 ships with wxauto4
        return None
    try:
        info = win32api.GetFileVersionInfo(path, "\\")
    except Exception:
        return None
    if not info:
        return None
    ms = info.get("FileVersionMS")
    ls = info.get("FileVersionLS")
    if ms is None or ls is None:
        return None
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def _registry_installations() -> list[tuple[str | None, str | None]]:
    try:
        import winreg
    except Exception:  # pragma: no cover - non-Windows
        return []

    found: list[tuple[str | None, str | None]] = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_path in _UNINSTALL_KEYS:
            try:
                uninstall = winreg.OpenKey(root, key_path)
            except OSError:
                continue
            with uninstall:
                for index in range(winreg.QueryInfoKey(uninstall)[0]):
                    entry = _read_uninstall_entry(winreg, uninstall, index)
                    if entry:
                        found.append(entry)
    return found


def _read_uninstall_entry(winreg, uninstall, index: int):
    try:
        name = winreg.EnumKey(uninstall, index)
        sub_key = winreg.OpenKey(uninstall, name)
    except OSError:
        return None
    with sub_key:
        display = _query_value(winreg, sub_key, "DisplayName")
        if not display or not _INSTALL_NAME_RE.search(str(display)):
            return None
        version = _query_value(winreg, sub_key, "DisplayVersion")
        location = _query_value(winreg, sub_key, "InstallLocation")
        path = str(location).strip().strip('"') if location else None
        return (str(version) if version else None, path)


def _query_value(winreg, key, name: str):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None
