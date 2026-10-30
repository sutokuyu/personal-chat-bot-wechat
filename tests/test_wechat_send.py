"""Test 2: wxauto4 sends text to 文件传输助手."""

from __future__ import annotations

from tests._wechat_live import FILE_TRANSFER_HELPER, requires_live_wechat

pytestmark = requires_live_wechat


def test_send_text_to_file_transfer_helper() -> None:
    from wxauto4 import WeChat

    wx = WeChat()
    response = wx.SendMsg("wxauto4 test", who=FILE_TRANSFER_HELPER, exact=True)

    print(f"send_response = {response}", flush=True)
    assert response.is_success, response


if __name__ == "__main__":  # pragma: no cover - manual run
    test_send_text_to_file_transfer_helper()
