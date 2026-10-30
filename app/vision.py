"""Image understanding for WeChat image messages.

The chat agent decides whether to speak from a text transcript, and that
transcript must mention what a picture contains. Images are therefore described
once with the vision-capable DeepSeek model, and the original files are also
attached to the agent call so it can answer questions about visual details.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

_VISION_PROMPT = (
    "用中文简要描述这张图片，1-3 句：主体、场景，以及图中可读的文字（如果有）。"
    "只描述你确实看到的内容，不要猜测或编造。"
)

_MIME_TYPES = {
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
}


def encode_image(path: str | Path) -> str | None:
    """Return a ``data:`` URL for the image, or ``None`` if it cannot be read."""

    try:
        payload = Path(path).read_bytes()
    except OSError:
        logger.warning("image_read_failed path=%s", path)
        return None
    suffix = Path(path).suffix.lower().lstrip(".")
    mime = _MIME_TYPES.get(suffix, "png")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


def image_content_block(path: str | Path, *, detail: str = "low") -> dict[str, Any] | None:
    """LangChain/OpenAI compatible ``image_url`` content block."""

    url = encode_image(path)
    if url is None:
        return None
    return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


class ImageDescriber:
    """Short natural-language description used by the participation judge."""

    def __init__(self, model: BaseChatModel, *, timeout_seconds: float = 30.0) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def describe(self, path: str | Path) -> str:
        block = image_content_block(path)
        if block is None:
            return ""
        message = HumanMessage(
            content=[{"type": "text", "text": _VISION_PROMPT}, block]
        )
        try:
            result = await asyncio.wait_for(
                self.model.ainvoke([message]), timeout=self.timeout_seconds
            )
        except Exception:
            logger.warning("image_description_failed path=%s", Path(path).name, exc_info=True)
            return ""

        text = result.content if isinstance(result.content, str) else str(result.content)
        return " ".join(text.split())[:300]
