from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.vision import ImageDescriber, encode_image, image_content_block
from tests.conftest import ScriptedChatModel


def test_encode_image_returns_data_url(tmp_path):
    path = tmp_path / "cat.png"
    path.write_bytes(b"fake-png-bytes")

    url = encode_image(path)

    assert url is not None
    assert url.startswith("data:image/png;base64,")


def test_encode_image_handles_missing_file(tmp_path):
    assert encode_image(tmp_path / "missing.png") is None
    assert image_content_block(tmp_path / "missing.png") is None


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [("jpg", "jpeg"), ("jpeg", "jpeg"), ("png", "png"), ("webp", "webp"), ("gif", "gif")],
)
def test_encode_image_maps_mime_types(tmp_path, suffix, expected):
    path = tmp_path / f"photo.{suffix}"
    path.write_bytes(b"x")

    assert encode_image(path).startswith(f"data:image/{expected};base64,")


async def test_describer_sends_text_and_image_blocks(tmp_path):
    path = tmp_path / "cat.jpg"
    path.write_bytes(b"fake-jpeg")
    model = ScriptedChatModel(responses=[AIMessage(content=" 一只橘猫躺在沙发上 \n")])
    describer = ImageDescriber(model, timeout_seconds=5)

    caption = await describer.describe(path)

    assert caption == "一只橘猫躺在沙发上"
    message = model.calls[0][0]
    assert isinstance(message.content, list)
    assert message.content[0]["type"] == "text"
    assert message.content[1]["type"] == "image_url"
    assert message.content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert message.content[1]["image_url"]["detail"] == "low"


async def test_describer_returns_empty_when_model_fails(tmp_path):
    path = tmp_path / "cat.png"
    path.write_bytes(b"fake-png")

    class Boom:
        async def ainvoke(self, messages):
            raise RuntimeError("vision down")

    describer = ImageDescriber(Boom(), timeout_seconds=1)
    assert await describer.describe(path) == ""
