# -*- coding: utf-8 -*-
"""设置中枢 + 控制服务 + 控制台设置 Tab 的测试。

设置从 Qt 面板搬到了浏览器控制台，这套用例守住新的那条链：
控制台 → HTTP → 桌宠进程 → 设置中枢 → 落盘 + 立即生效。
"""
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── 设置中枢 ─────────────────────────────────────────────────────────────
def _service(tmp_path, **kwargs):
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    store = SettingsStore(tmp_path / "settings.json")
    return SettingsService(store, **kwargs), store


def test_update_persists_and_applies(tmp_path):
    svc, store = _service(tmp_path)
    applied = []
    svc.apply_hook = lambda values: applied.append(dict(values))

    result = svc.update({"scale": 0.75, "opacity": 0.9})

    assert result["ok"] is True
    assert set(result["applied"]) == {"scale", "opacity"}
    assert applied == [{"scale": 0.75, "opacity": 0.9}]
    assert abs(float(store.get("scale")) - 0.75) < 0.01


def test_update_clamps_values_to_schema_range(tmp_path):
    svc, store = _service(tmp_path)
    svc.update({"scale": 99.0, "opacity": 0.01, "frame_ms": 9999})
    assert float(store.get("scale")) <= 1.0
    assert float(store.get("opacity")) >= 0.7
    assert int(store.get("frame_ms")) <= 200


def test_update_rejects_unknown_key(tmp_path):
    svc, store = _service(tmp_path)
    result = svc.update({"根本没这个键": 1})
    assert result["ok"] is False
    assert "根本没这个键" in result["rejected"]
    assert store.get("根本没这个键") is None


def test_masked_api_key_is_not_written_back(tmp_path):
    """控制台回显的是掩码 —— 掩码不能被当成新 Key 写回去。"""
    svc, store = _service(tmp_path)
    store.set("model_api_key", "sk-real-key-1234")

    result = svc.update({"model_api_key": "******1234"})

    assert store.get("model_api_key") == "sk-real-key-1234", "掩码把真 Key 冲掉了"
    assert "model_api_key" in result["skipped"]


def test_values_masks_the_api_key(tmp_path):
    svc, store = _service(tmp_path)
    store.set("model_api_key", "sk-secret-9999")
    shown = svc.values()["model_api_key"]
    assert "sk-secret" not in shown
    assert shown.endswith("9999")


def test_reset_keeps_model_config(tmp_path):
    svc, store = _service(tmp_path)
    store.set("model_name", "my-model")
    store.set("scale", 0.5)
    svc.reset_defaults()
    assert store.get("model_name") == "my-model", "模型名被恢复默认冲掉了"
    assert float(store.get("scale")) != 0.5


def test_schema_has_labels_types_and_ranges(tmp_path):
    svc, _store = _service(tmp_path)
    items = {i["key"]: i for i in svc.schema()}
    assert items["scale"]["label"]
    assert items["scale"]["type"] == "float"
    assert items["scale"]["min"] == 0.2 and items["scale"]["max"] == 1.0
    assert items["renderer"]["options"] == ["sprite", "live2d"]
    assert items["renderer"]["restart"] is True
    assert items["model_api_key"]["type"] == "secret"


def test_model_config_goes_to_config_yaml(tmp_path):
    """模型配置要写进 config.yaml（跟主配置在一起，下次启动还是它）。"""
    import yaml
    svc, store = _service(tmp_path, project_root=tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"llm": {"model": "old"}, "other": 1}),
                   encoding="utf-8")

    svc.update({"model_name": "new-model", "model_temperature": 0.5})

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["llm"]["model"] == "new-model"
    assert data["llm"]["temperature"] == 0.5
    assert data["other"] == 1, "写 yaml 时把别的段弄丢了"


# ── 控制服务 ─────────────────────────────────────────────────────────────
def _start_control(**handlers):
    from app.core.pet_control import PetControlServer
    srv = PetControlServer(port=0, **handlers)
    port = srv.start()
    assert port, "控制服务没起来"
    return srv, port


def _get(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(port, path, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_control_server_serves_settings_and_actions():
    seen = {}

    def _update(changes):
        seen.update(changes)
        return {"ok": True, "applied": sorted(changes)}

    srv, port = _start_control(
        get_settings=lambda: {"scale": 0.6},
        get_schema=lambda: [{"key": "scale", "label": "缩放", "type": "float"}],
        update_settings=_update,
        get_appearance=lambda: {"available": False, "groups": []},
        list_avatars=lambda: [{"path": "a.png", "name": "a"}],
        do_action=lambda action, params: {"ok": True, "action": action,
                                          "params": params},
    )
    try:
        assert _get(port, "/health")["ok"] is True
        assert _get(port, "/settings")["values"] == {"scale": 0.6}
        assert _get(port, "/schema")["schema"][0]["key"] == "scale"
        assert _get(port, "/avatars")["avatars"][0]["name"] == "a"

        body = _post(port, "/settings", {"changes": {"scale": 0.8}})
        assert body["ok"] is True
        assert seen == {"scale": 0.8}

        body = _post(port, "/action", {"action": "live2d_reset",
                                       "params": {}})
        assert body["ok"] is True and body["action"] == "live2d_reset"
    finally:
        srv.stop()


def test_control_server_reports_bad_json():
    srv, port = _start_control()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/settings" % port, data=b"{not json",
            headers={"content-type": "application/json"}, method="POST")
        with pytest.raises(Exception):
            urllib.request.urlopen(req, timeout=10)
    finally:
        srv.stop()


def test_control_client_without_url_reports_not_connected():
    from app.web.pet_control_client import PetControlClient
    client = PetControlClient("")
    body = client.health()
    assert body["ok"] is False
    assert "没连上" in body["error"]


def test_control_client_calls_the_server():
    from app.web.pet_control_client import PetControlClient
    srv, port = _start_control(
        get_settings=lambda: {"scale": 0.6},
        update_settings=lambda changes: {"ok": True, "applied": list(changes)},
    )
    try:
        client = PetControlClient("http://127.0.0.1:%d" % port)
        assert client.health()["ok"] is True
        assert client.settings()["values"] == {"scale": 0.6}
        body = client.update_settings({"scale": 0.9})
        assert body["ok"] is True and body["applied"] == ["scale"]
    finally:
        srv.stop()


# ── 控制台路由 ───────────────────────────────────────────────────────────
def _panel(tmp_path, control_url=""):
    from app.web.dashboard import create_app
    app = create_app(tmp_path / "traces.db", "http://127.0.0.1:8788",
                     control_url or None)
    return app


def test_dashboard_settings_route_reports_not_connected(tmp_path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    with fastapi_testclient.TestClient(_panel(tmp_path)) as client:
        body = client.get("/api/settings").json()
    assert body["ok"] is True
    assert body["connected"] is False
    assert body["schema"] == []


def test_dashboard_settings_route_reads_the_control_server(tmp_path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    srv, port = _start_control(
        get_settings=lambda: {"scale": 0.6, "opacity": 1.0},
        get_schema=lambda: [{"key": "scale", "label": "缩放", "type": "float",
                             "group": "视觉"}],
        get_appearance=lambda: {"available": True, "groups": [
            {"id": "face", "label": "表情", "items": [["a", "开心"]]}],
            "active": ["a"]},
        list_avatars=lambda: [{"path": "a.png", "name": "a"}],
    )
    try:
        with fastapi_testclient.TestClient(
                _panel(tmp_path, "http://127.0.0.1:%d" % port)) as client:
            body = client.get("/api/settings").json()
        assert body["connected"] is True
        assert body["values"]["scale"] == 0.6
        assert body["schema"][0]["key"] == "scale"
        assert body["appearance"]["available"] is True
        assert body["avatars"][0]["name"] == "a"
    finally:
        srv.stop()


def test_dashboard_settings_update_forwards_to_control_server(tmp_path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    seen = {}
    srv, port = _start_control(
        update_settings=lambda changes: {"ok": True,
                                         "applied": sorted(changes),
                                         "restart": []},
        do_action=lambda action, params: {"ok": True, "action": action},
    )
    try:
        with fastapi_testclient.TestClient(
                _panel(tmp_path, "http://127.0.0.1:%d" % port)) as client:
            body = client.post("/api/settings",
                               json={"changes": {"scale": 0.9}}).json()
            assert body["ok"] is True
            act = client.post("/api/settings/action",
                              json={"action": "live2d_reset"}).json()
            assert act["action"] == "live2d_reset"
    finally:
        srv.stop()


def test_dashboard_page_has_settings_tab(tmp_path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    with fastapi_testclient.TestClient(_panel(tmp_path)) as client:
        html = client.get("/").text
    assert 'data-tab="settings"' in html
    assert 'id="view-settings"' in html
    assert 'id="settingsGroups"' in html
    assert 'id="settingsSave"' in html
    assert 'id="settingsAppearance"' in html
    assert 'id="settingsAvatars"' in html
    # 旧的设置面板入口不该在
    assert "设置面板" not in html


def test_dashboard_collects_unchecked_switches(tmp_path):
    """取消勾选的开关也要发出去 —— 只发 true 的话关掉的开关永远改不掉。"""
    src = (ROOT / "app" / "web" / "dashboard.py").read_text(encoding="utf-8")
    assert 'if(el.type==="checkbox"){ out[key]=el.checked; return; }' in src, (
        "复选框只发 true —— 主人关掉一个开关就改不掉"
    )


# ── Live2D 模型 / 场景 / 动作配置 ─────────────────────────────────────────
def test_list_models_finds_model_dirs(tmp_path):
    """扫模型根目录，找出所有含 *.model3.json 的目录。"""
    from app.core.live2d_config import list_models
    repo = tmp_path
    root = repo / "assets" / "live2d"
    (root / "cat" / "cat_model").mkdir(parents=True)
    (root / "cat" / "cat_model" / "cat.model3.json").write_text("{}", encoding="utf-8")
    (root / "dog" / "dog_model").mkdir(parents=True)
    (root / "dog" / "dog_model" / "dog.model3.json").write_text("{}", encoding="utf-8")
    # 不是模型的目录要被忽略
    (root / "empty").mkdir(parents=True)

    models = list_models(root, current="assets/live2d/dog/dog_model")
    names = sorted(m["name"] for m in models)
    assert names == ["cat_model", "dog_model"]
    current = [m for m in models if m["current"]]
    assert current and current[0]["name"] == "dog_model"
    # 路径是相对仓库根的（跟 config.yaml 里 model_dir 一个写法）
    assert current[0]["path"] == "assets/live2d/dog/dog_model"


def test_scene_catalog_covers_all_scene_groups():
    """场景清单要含聊天情绪 / 状态 / 互动三组 + 游戏场景。"""
    from app.core.live2d_config import scene_catalog
    from app.animation.live2d_scene import (SCENE_GROUP_CHAT, SCENE_GROUP_INTERACT,
                                            SCENE_GROUP_STATE)
    catalog = scene_catalog()
    groups = {c["group"] for c in catalog}
    assert SCENE_GROUP_CHAT in groups
    assert SCENE_GROUP_STATE in groups
    assert SCENE_GROUP_INTERACT in groups
    ids = {c["id"] for c in catalog}
    assert "touch_head" in ids and "chat_happy" in ids
    assert "game_win" in ids, "游戏场景也要能配"
    assert all(c["kind"] in ("persist", "transient") for c in catalog)


def test_hook_options_include_tool_and_model_hooks():
    """钩子 = 工具动作 + 模型自带动作（不同模型不一样）。"""
    from app.core.live2d_config import hook_options

    class _Profile:
        actions = {"my_special_move": object()}

    class _Renderer:
        profile = _Profile()

    hooks = hook_options(_Renderer())
    keys = [h["key"] for h in hooks]
    assert "" in keys, "要有「仅手动播放」"
    assert "eat" in keys, "工具动作没列出来"
    assert "my_special_move" in keys, "模型自带动作没列出来"


def test_bundle_roundtrip():
    """外观组合 dict ↔ SceneBundle 往返不丢东西。"""
    from app.core.live2d_config import bundle_to_dict, dict_to_bundle
    data = {"emotion": "脸红", "hairstyle": "小辫",
            "toggles": ["比心", "右喵喵拳"], "hold_ms": 1200}
    bundle = dict_to_bundle(data)
    assert bundle.emotion == "脸红"
    assert bundle.hairstyle == "小辫"
    assert bundle.toggles == ["比心", "右喵喵拳"]
    assert bundle.hold_ms == 1200
    assert bundle_to_dict(bundle) == data


def test_summarize_marks_natural_emotion():
    from app.core.live2d_config import dict_to_bundle, summarize
    out = summarize(dict_to_bundle({"emotion": "natural"}))
    assert "自然" in out
    assert summarize(dict_to_bundle({})) == "默认（不改变）"


def test_dashboard_has_live2d_config_sections(tmp_path):
    """控制台要有模型 / 场景 / 动作三个区。"""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    with fastapi_testclient.TestClient(_panel(tmp_path)) as client:
        html = client.get("/").text
    assert 'id="settingsModels"' in html
    assert 'id="settingsScenes"' in html
    assert 'id="settingsActions"' in html


def test_control_actions_exist_for_live2d_config():
    """主程序要认这些动作（模型切换 / 场景 / 动作配置）。"""
    src = (ROOT / "app" / "ui" / "ui_controller.py").read_text(encoding="utf-8")
    for action in ("live2d_models", "live2d_switch_model",
                  "live2d_scene_save", "live2d_scene_reset",
                  "live2d_action_save", "live2d_action_delete",
                  "live2d_action_play", "live2d_preview"):
        assert action in src, f"控制服务少了 {action} 动作"


def test_dashboard_avatar_preview_rejects_paths_outside_avatars(tmp_path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    with fastapi_testclient.TestClient(_panel(tmp_path)) as client:
        r = client.get("/api/avatar", params={"path": "C:/Windows/win.ini"})
        assert r.status_code == 400
        r = client.get("/api/avatar", params={"path": "../../etc/passwd"})
        assert r.status_code == 400


# ── 主线程调用 ───────────────────────────────────────────────────────────
def test_main_thread_invoker_runs_on_the_main_thread():
    """HTTP 线程不能碰 Qt 控件 —— 调用要投递回主线程。

    跨线程直接改桌宠窗口会崩（实测：改一次缩放桌宠就退出了）。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.core.qt_compat import QApplication
    from app.core.main_thread import MainThreadInvoker
    app = QApplication.instance() or QApplication([])
    main_thread = app.thread()

    invoker = MainThreadInvoker()
    seen = []
    result = invoker.call(lambda: seen.append(main_thread))
    assert result is None
    assert seen and seen[0] is main_thread, "调用没跑在主线程上"


def test_main_thread_invoker_propagates_errors():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.core.qt_compat import QApplication
    from app.core.main_thread import MainThreadInvoker
    QApplication.instance() or QApplication([])

    invoker = MainThreadInvoker()

    def _boom():
        raise ValueError("炸了")

    with pytest.raises(ValueError, match="炸了"):
        invoker.call(_boom)


def test_settings_update_can_be_split_into_prepare_and_apply(tmp_path):
    """落盘与「应用到桌宠」要能分开 —— 后者必须在主线程跑。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    store = SettingsStore(tmp_path / "settings.json")
    svc = SettingsService(store)
    applied = []
    svc.apply_hook = lambda values: applied.append(dict(values))

    prepared = svc.prepare({"scale": 0.7})
    assert prepared["accepted"] == {"scale": 0.7}
    assert store.get("scale") == 0.7, "prepare 阶段就该落盘"
    assert applied == [], "prepare 不该碰桌宠"

    svc.apply(prepared["accepted"])
    assert applied == [{"scale": 0.7}], "apply 阶段才该让桌宠生效"


def test_apply_settings_changed_has_no_broken_generators():
    """设置分发的每个 any(...) 都要绑好循环变量。

    漏一个 for k in changes 就是 NameError —— 改设置直接报错。
    """
    import ast
    src = (ROOT / "app" / "ui" / "ui_controller.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_apply_settings_changed")
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "any"):
            continue
        for gen in getattr(node, "generators", []):
            names = {n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name)}
            used = {n.id for n in ast.walk(gen.elt) if isinstance(n, ast.Name)}
            assert names & used, (
                "any(...) 的生成器里用了没绑定的循环变量："
                f"{ast.dump(gen)[:120]}"
            )
