# -*- coding: utf-8 -*-
"""A1 的真·端到端：**真 sidecar 进程** + 真语义检索（opt-in，默认跳过）。

跑法::

    DESKTOP_PET_MEMORY_SIDECAR_E2E=1 python -m pytest tests/test_memory_sidecar_e2e.py -q -s

为什么不进默认套件（A7）：这条路会拉起一个真的 memory_sidecar 子进程，加载
bge 向量模型 + cross-encoder 精排（本机缓存里没有模型时会很慢或者降级），
默认套件不许依赖它。默认套件里的等价覆盖是
tests/test_memory_tools_sidecar.py（假 HTTP 层）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.brain.memory_client import MemoryClient  # noqa: E402
from app.engine.tools import ToolRegistry  # noqa: E402
from app.engine.tools._memory import register as register_memory_tools  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DESKTOP_PET_MEMORY_SIDECAR_E2E"),
    reason="opt-in：需要真的跑 memory_sidecar（DESKTOP_PET_MEMORY_SIDECAR_E2E=1）",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def sidecar(tmp_path_factory):
    """真 sidecar 子进程（临时数据目录：不碰仓库的 data/，A8）。"""
    data_dir = tmp_path_factory.mktemp("sidecar_e2e")
    port = _free_port()
    env = dict(os.environ, DESKTOP_PET_MEMORY_DIR=str(data_dir))
    proc = subprocess.Popen(
        [sys.executable, "-m", "memory_sidecar", "serve",
         "--port", str(port), "--data-dir", str(data_dir)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    client = MemoryClient(base_url="http://127.0.0.1:%d" % port, timeout=30.0)
    deadline = time.time() + 60.0
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("sidecar 进程退了（退出码 %s）" % proc.returncode)
            if client.health():
                break
            time.sleep(0.3)
        else:
            raise RuntimeError("sidecar 60s 内没起来")
        yield client, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
            proc.wait(timeout=10)
        assert proc.poll() is not None, "退出后不许留孤儿进程（A5）"


def test_a1_remember_a_fact_then_recall_it_semantically_then_forget(sidecar, capsys):
    """A1：记一条事实 → 用**没有字面重合**的问法召回 → 删掉。"""
    client, port = sidecar
    reg = ToolRegistry()
    register_memory_tools(reg, memory=client)
    remember = reg.get("remember_fact").fn
    recall = reg.get("recall_memory").fn
    forget = reg.get("forget_memory").fn

    # 先放两条干扰项，证明不是「库里只有一条所以必中」
    remember(content="主人喜欢在周末爬山", category="event", importance=0.4)
    remember(content="主人的猫叫团子", category="fact", importance=0.5)
    out = remember(content="主人不吃辣", category="preference", importance=0.8)
    assert out.startswith("已记住"), out
    card_id = out.split("id=")[1].split(" ")[0]

    health = client.health()
    print("健康：", json.dumps(health, ensure_ascii=False))
    assert health.get("ok") is True
    assert health.get("cards") == 3

    rows = client.search("我不能吃什么", top_k=3)
    print("检索「我不能吃什么」→")
    for row in rows:
        print("   ", json.dumps({k: row.get(k) for k in
                                 ("id", "kind", "text", "score", "rel", "rerankLogit")},
                                ensure_ascii=False))
    assert rows, "语义检索一条都没回来"
    assert rows[0]["text"] == "主人不吃辣", rows
    assert rows[0]["id"] == card_id

    tool_out = recall(keyword="我不能吃什么", limit=3)
    print("工具召回输出：")
    print(tool_out)
    assert "主人不吃辣" in tool_out

    # 记忆块（易变通道）也是同一份数据
    block = client.system_block("我不能吃什么")
    print("记忆块：")
    print(block)
    assert "主人不吃辣" in block

    assert forget(memory_id=card_id).startswith("已删除 1 条记忆"), "按 id 删不掉"
    assert all("不吃辣" not in r.get("text", "") for r in client.search("我不能吃什么"))
