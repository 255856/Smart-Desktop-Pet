"""标准后端（LangChain）暴露的工具 schema 必须与手写的 tool.parameters 一致。

Stage 0.1 回归测试，覆盖三件事：
1. 每个注册工具：标准后端暴露的 schema == 手写 tool.parameters
   （属性集 / 属性描述 / 类型 / enum / min-max / required 逐项比）；
2. run_script 的参数叫 args（不是 pydantic 改名后的 v__args），且真调得通；
3. 曾经属性集变了的 4 个工具（run_script / generate_image / generate_video /
   generate_audio）属性集与手写一致。

不需要 API key / 网络 / GUI。

注册表怎么搭的（brief 要求说明）
--------------------------------
用 app.engine.tools.build_default_tools —— 就是主程序用的那个装配函数，
不是自己 import 一堆工具模块。state 传一个最小 stub（只要 get() /
stats_summary()），reminders / memory / items 传 None：
这三个只在**工具被调用时**才用到（reminder 要 ReminderStore、memory 要
MemoryStore、feed_self 要食物库），注册阶段不碰它们，所以不需要真的造这些对象。
"""
from __future__ import annotations

import pytest

from app.brain.langchain_agent import convert_tools
from app.engine.tools import build_default_tools


class _StubState:
    """最小 state stub：注册阶段用不到任何属性。"""

    def get(self, *a, **k):
        return None

    def stats_summary(self) -> str:
        return "stub"


def _build_registry():
    return build_default_tools(
        state=_StubState(), reminders=None, memory=None, items=None
    )


@pytest.fixture(scope="module")
def registry():
    return _build_registry()


@pytest.fixture(scope="module")
def lc_tools(registry):
    """{工具名: StructuredTool} —— 标准后端暴露给模型的那一份。"""
    return {t.name: t for t in convert_tools(registry)}


def _normalize(schema: dict) -> dict:
    """JSON Schema / pydantic schema → 归一化形状（只留要比的字段）。

    pydantic 会多出两个键，这里都不比：
      - title：pydantic 按字段名自动生成的，手写 schema 里没有，无语义；
      - default：手写 schema 多数属性没写 default，pydantic 会补一个
        （非必填属性的运行时默认值来源见 app/brain/tool_schema.py）。
    """
    props = schema.get("properties") or {}
    out: dict = {}
    for pname, p in props.items():
        p = p or {}
        out[pname] = {
            "type": p.get("type"),
            "description": p.get("description"),
            "enum": p.get("enum"),
            "minimum": p.get("minimum"),
            "maximum": p.get("maximum"),
        }
    return {
        "properties": out,
        "required": sorted(schema.get("required") or []),
    }


def test_registry_not_empty(registry):
    """空注册表会让下面的逐条比对白跑一条，先确认工具确实装上了。"""
    assert len(registry._tools) >= 50, "工具注册表空了？"


def test_every_tool_schema_matches_handwritten(registry, lc_tools):
    """每个工具：标准后端暴露的 schema == 手写 tool.parameters。"""
    assert set(lc_tools) == set(registry._tools)
    problems = []
    for name, tool in registry._tools.items():
        hand = _normalize(tool.parameters or {})
        derived = _normalize(lc_tools[name].args_schema.model_json_schema())
        if hand == derived:
            continue
        detail = []
        for pname in sorted(set(hand["properties"]) | set(derived["properties"])):
            h = hand["properties"].get(pname)
            d = derived["properties"].get(pname)
            if h != d:
                detail.append(f"    {pname}: 手写={h} 派生={d}")
        if hand["required"] != derived["required"]:
            detail.append(
                f"    required: 手写={hand['required']} 派生={derived['required']}"
            )
        problems.append(f"  {name}\n" + "\n".join(detail))
    assert not problems, "schema 不一致的工具：\n" + "\n".join(problems)


def test_descriptions_and_constraints_preserved(registry, lc_tools):
    """属性描述 / enum / min-max 一条都不能丢（修之前标准后端全丢）。"""
    n_desc = n_enum = n_bound = 0
    for name, tool in registry._tools.items():
        derived = (lc_tools[name].args_schema.model_json_schema().get("properties")
                   or {})
        for pname, p in ((tool.parameters or {}).get("properties") or {}).items():
            d = derived.get(pname) or {}
            if p.get("description"):
                n_desc += 1
                assert d.get("description") == p["description"], f"{name}.{pname}"
            if "enum" in p:
                n_enum += 1
                assert d.get("enum") == p["enum"], f"{name}.{pname}"
            for key in ("minimum", "maximum"):
                if key in p:
                    n_bound += 1
                    assert d.get(key) == p[key], f"{name}.{pname}.{key}"
    assert n_desc > 0 and n_enum > 0 and n_bound > 0


def test_run_script_parameter_is_args(registry, lc_tools):
    """run_script 的参数必须叫 args（撞 BaseTool.args 被改名成 v__args 的坑）。"""
    props = lc_tools["run_script"].args_schema.model_json_schema()["properties"]
    assert "args" in props, "run_script 的 args 参数不见了"
    assert props["args"]["type"] == "string"
    assert "v__args" not in props, "args 又被 pydantic 改名了"
    hand = set((registry.get("run_script").parameters or {})["properties"])
    assert set(props) == hand


def test_four_tools_property_set(registry, lc_tools):
    """这 4 个工具以前属性集直接变了（多了 kwargs / 少了手写属性）。"""
    expected = {
        "run_script": {"script_path", "timeout_s", "args"},
        "generate_image": {"prompt", "size"},
        "generate_video": {"prompt", "duration_seconds"},
        "generate_audio": {"prompt"},
    }
    for name, want in expected.items():
        got = set(lc_tools[name].args_schema.model_json_schema()["properties"])
        assert got == want, f"{name}: 派生={sorted(got)} 手写={sorted(want)}"


def test_run_script_invocation_with_args(tmp_path, monkeypatch):
    """A2：run_script 在标准后端真调得通（args 不再被改名、不再类型错）。

    沙箱限定家目录，所以把 _runner._HOME 指到临时目录再放脚本进去。
    """
    from app.engine.tools import _runner

    monkeypatch.setattr(_runner, "_HOME", tmp_path)
    script = tmp_path / "echo_args.py"
    script.write_text(
        "import sys\nprint('ARGS=' + '|'.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )

    lc = {t.name: t for t in convert_tools(_build_registry())}
    out = lc["run_script"].invoke(
        {"script_path": str(script), "args": "--flag hello world"}
    )
    assert "ARGS=--flag|hello|world" in out, out


def test_optional_param_uses_fn_default(registry, lc_tools):
    """非必填参数没给值时，走 fn 自己的默认值（跟轻量后端行为一致）。

    例如 run_script 的 timeout_s 默认 60、args 默认 ""；date_info 的
    date_str 默认 ""（今天）。标准后端不能因为换了 schema 就把默认值弄丢。
    """
    props = lc_tools["run_script"].args_schema.model_json_schema()["properties"]
    assert props["timeout_s"]["default"] == 60
    assert props["args"]["default"] == ""

    out = lc_tools["date_info"].invoke({})
    assert out and not out.startswith("错误"), out


def test_tool_call_schema_keeps_constraints(registry, lc_tools):
    """发给模型的那份 schema（tool_call_schema）也得带 enum / min-max。

    LangChain 生成 tool_call_schema 时会**重建**每个字段，只搬 description /
    default / metadata —— json_schema_extra 会被丢掉，metadata 不会。
    所以约束必须挂在 metadata 上（见 app/brain/tool_schema.py 的 _SchemaExtra）。
    """
    tcs = lc_tools["run_script"].tool_call_schema.model_json_schema()
    props = tcs["properties"]
    assert props["timeout_s"]["minimum"] == 1
    assert props["timeout_s"]["maximum"] == 300
    assert props["args"]["description"] == "传给脚本的参数，shlex 切分"

    cat = lc_tools["remember_fact"].tool_call_schema.model_json_schema()["properties"]
    hand = registry.get("remember_fact").parameters["properties"]["category"]
    assert cat["category"]["enum"] == hand["enum"]
