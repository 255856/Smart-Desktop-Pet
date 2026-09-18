"""截图分析：让用户用自然语言描述当前屏幕内容，桌宠自动截图并描述。"""
import base64
import io
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def take_screenshot() -> bytes | None:
    """截取当前屏幕，返回 PNG bytes。失败返回 None。"""
    try:
        # 尝试用 pyautogui
        import pyautogui
        img = pyautogui.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        pass
    except Exception as e:
        log.warning("截图失败：%s", e)
        return None

    try:
        # 备选：用 PIL
        from PIL import ImageGrab
        img = ImageGrab.grab()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log.warning("PIL 截图失败：%s", e)
        return None


def screenshot_to_base64() -> str | None:
    """截图并返回 base64 编码（用于发送给视觉模型）。"""
    png_data = take_screenshot()
    if png_data is None:
        return None
    return base64.b64encode(png_data).decode("utf-8")
