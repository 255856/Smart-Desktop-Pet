# -*- coding: utf-8 -*-
"""桌宠控制服务的客户端（控制台 → 桌宠主进程）。

设置搬进控制台后，面板就是桌宠的遥控器：改设置、换 Live2D 外观、试听语音
都走这里。桌宠进程开着一个小 HTTP 端口接这些请求（app/core/pet_control.py）。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0


class PetControlClient:
    """控制服务的 HTTP 客户端。

    桌宠没在跑时所有调用都返回 ``{"ok": False, "error": "..."}`` ——
    面板据此提示「桌宠没连上」，而不是抛异常。
    """

    def __init__(self, base_url: str = "", timeout: float = DEFAULT_TIMEOUT):
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = float(timeout or DEFAULT_TIMEOUT)

    # -- 底层 ------------------------------------------------------------
    def _call(self, method: str, path: str, payload=None) -> dict:
        if not self.base_url:
            return {"ok": False, "error": "桌宠进程没连上（控制端口未知）"}
        url = self.base_url + path
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {"ok": False, "error": "HTTP %s" % e.code}
            return body if isinstance(body, dict) else {"ok": False}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "连不上桌宠进程：%s" % e}
        return body if isinstance(body, dict) else {"ok": False}

    # -- 端点 ------------------------------------------------------------
    def health(self) -> dict:
        """桌宠进程活着没。"""
        return self._call("GET", "/health")

    def settings(self) -> dict:
        """当前所有设置项的值。"""
        return self._call("GET", "/settings")

    def schema(self) -> dict:
        """设置项清单（标签 / 范围 / 选项）。"""
        return self._call("GET", "/schema")

    def update_settings(self, changes: dict) -> dict:
        """改一批设置（立即在桌宠上生效）。"""
        return self._call("POST", "/settings", {"changes": changes or {}})

    def action(self, action: str, **params) -> dict:
        """触发一个运行时动作（换外观 / 试听语音 / 换头像…）。"""
        return self._call("POST", "/action",
                          {"action": action, "params": params or {}})

    def appearance(self) -> dict:
        """Live2D 外观分类（面板渲染成 chip 网格）。"""
        return self._call("GET", "/appearance")

    def avatars(self) -> dict:
        """可选头像列表。"""
        return self._call("GET", "/avatars")
