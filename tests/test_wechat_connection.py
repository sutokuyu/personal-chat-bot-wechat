"""Test 1: wxauto4 attaches to the already logged-in WeChat client."""

from __future__ import annotations

from app.wechat_env import check_environment
from tests._wechat_live import requires_live_wechat

pytestmark = requires_live_wechat


def test_connection_attaches_to_logged_in_client() -> None:
    from wxauto4 import WeChat

    environment = check_environment()
    wx = WeChat()

    assert wx.IsOnline() is True
    info = wx.GetMyInfo()
    print(
        f"connected wechat={environment.wechat_version} "
        f"wxauto4={environment.wxauto4_version} info={info}",
        flush=True,
    )
    assert info


if __name__ == "__main__":  # pragma: no cover - manual run
    test_connection_attaches_to_logged_in_client()
