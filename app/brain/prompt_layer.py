"""共享提示词层（Stage 1.1）—— 最终 system 字符串的**唯一拼装点**。

为什么要有这一层
----------------
ReAct（轻量）与 standard（LangChain）两个后端以前**各自**拼 system prompt，
同一个模型会因后端不同而表现出两套性格 —— 中文输出约束、说话铁律、工具解锁
提示、项目文档 / skills / 记忆块、防幻觉基线各只在一侧。
这一层把两边的差异组件全部收进来，两边都调它 ⇒ 两边发出的 system 字符串
**逐字节相同**（见 tests/test_backend_parity.py）。

段顺序（固定）
--------------
  1. persona        人设（+ 上游注入的会话上下文：SKILL / 可用工具列表；
                     **长期记忆已搬走** —— 走易变通道）
  2. style_guide    characters/style-guide.md —— **逐字**追加，不改写、不重排
  3. slang          已收录黑话表（**只有 confirmed 词条**）—— 常量块
  4. identity       自我认知（我是谁）—— **记忆库里的分区**（kind=identity）
  5. value         自我价值观（底线 → 偏好 → 风格）—— **记忆库里的分区**（kind=value）
                     （程序性 kind=procedural 条目多、变化快 → 走易变段按需召回，不进这里）
  6. tool_handbook  工具手册（【工具解锁】等级提示）
  7. dynamic        会话内稳定动态块（项目级指令文档 + skills）
  8. 防幻觉基线 + 高风险工具 confirm 协议
  9. 中文输出约束
 10. 长度策略（Stage 1.4：不再是硬上限「回复 1~3 句」）
 11. 【说话方式】（Stage 1.6 从 _STYLE_RULE 拆出来的第一段）
 12. 【任务执行协议】（Stage 1.6 拆出来的第二段）

顺序变了会让 MiniMax 的前缀缓存整块失效，所以**不要随手调换**。

易变内容（当前时间 / 桌宠实时状态 / 状态策略）**一律不进这里**
------------------------------------------------------------
MiniMax 的 prompt cache 命中**最长公共前缀**（128-token 页粒度），system message 在**最前面**，
它里面每轮变一次的内容（时间戳！状态策略！）会让它后面的一切不再匹配 ——
实测缓存命中率 98.9% → 1.5%。（不是「凑满 8192 块」才命中：真命中可只复用 7808 token。）
所以易变内容走 build_turn_context() → 一条**独立的 user 消息**（排在主人这句话前面），
主人原话保持字节不变（意图判定 detect_action_intent 读的就是它）。
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 动态上下文消息的标记行（同时用作幂等判断）
# --------------------------------------------------------------------------
#: 动态上下文消息的标记行（同时用作幂等判断）
DYNAMIC_CONTEXT_HEADER = "[系统上下文 · 本轮动态信息，不是主人说的话]"


# --------------------------------------------------------------------------
# 常量段（顺序 = system 段的顺序，见模块 docstring）
# --------------------------------------------------------------------------

#: 中文输出约束（原 llm_client.CHINESE_SYSTEM_SUFFIX，Stage 1.1 搬进共享层）
#:
#: Stage 1.4：末尾的硬上限「回复 1~3 句」**已删**，换成下面的 LENGTH_STRATEGY。
#: emoji：Stage 1.3 把清洗管线的 emoji 剥离**整个去掉**了（sanitize_text 现在
#: 保留 emoji 与装饰符号）。这里原来的全局禁令「不要用 emoji 和装饰符号。」
#: 会把 1.3 的成果重新抵消掉 —— 已删除。
#: emoji 策略归**行为层**（characters/style-guide.md，逐字追加进 system prompt）管 ——
#: prompt 里**一个字都不提** emoji：既不禁止，也不指向。
CHINESE_SYSTEM_SUFFIX = (
    "你是用简体中文说话的人，像跟主人面对面说话一样。回答只用最终内容，"
    "不要做任何铺垫、不要解释你在想什么、不要复述主人刚才问的问题、"
    "不要把上面的要求重复念出来。下面是两个对比——\n\n"
    "✅ 像这样：「嘿嘿~主人来啦，今天想干嘛呀？~」\n"
    "❌ 不要这样：「我需要以可爱软萌角色回答。主人问我想做什么，我会说游泳。」"
    "「唯一允许的输出语言是简体中文。1. xxx 2. xxx」"
    "「根据角色设定我会……」\n\n"
    "工具调用只走 tool schema；不要在文字里描述要调什么工具。"
    "信息不够 / 不确定 / 时效性问题（天气/新闻/榜单/日期/……），"
    "必须调 web_search 拿真实信息，不允许编造或说「我不知道」。"
)

#: 长度策略（Stage 1.4）
#:
#: 原来是一句硬上限「回复 1~3 句」—— 20 条评测回复长度恒定，看着像机器人。
#: 现在改成**策略**：默认短、要解释时多说几句、而且不要每次一样长。
LENGTH_STRATEGY = (
    "【回复长度】默认用短句（1~10 字）；需要解释、报错、查资料这类正事时"
    "最多说 2~3 句，一条一个意思；不要每次都一样长。"
)

#: 【说话方式】（Stage 1.6 新增）
#:
#: 与 characters/style-guide.md 配套但**不重复**它的内容（style guide 是逐字追加的，
#: 这里只补「怎么用这份 style guide」的落点：只跟主人说话、别像客服）。
SPEECH_STYLE_SECTION = (
    "【说话方式】\n"
    "- 只跟主人一个人说话，称呼「主人」；不要「你们」「大家」「用户」。\n"
    "- 是坐在桌面上的小宠物，不是客服、不是助手、不是 AI：不列清单、不分点、不写小作文。\n"
    "- 一条一件事，短句为主；不确定就直说（「我看看」「没查到」），"
    "别铺垫、别客套、别问「还有什么可以帮您」。\n"
    "- 上面「怎么说话」一节是硬要求：AI 味词、客服腔、语气词刷屏一律不用。\n"
)

#: 【任务执行协议】（Stage 1.6：原 _STYLE_RULE 收窄到「确实有工具意图时」）
#:
#: 纯聊天时这一整段不生效 —— 模型不该在闲聊里提工具、不该列步骤。
TASK_PROTOCOL_SECTION = (
    "【任务执行协议】\n"
    "以下只在**确实要做工具操作**时生效；纯聊天时不要提工具、不要列步骤。\n\n"
    "【回复风格铁律】给主人的回复里严禁出现：工具名称（如 open_website/open_app）、"
    "对内部操作的描述（如「我应该使用XX工具」「这是要打开一个应用」「主人想打开XX，让我来」）、"
    "复述主人的指令、推理过程、<think> 标记。调用工具的那一轮**不要输出任何说明文字**"
    "（只给工具调用本身）；只有在不再调用工具、给主人最终答复时，才直接输出一句"
    "符合角色人设的自然中文。\n\n"
    "【自主使用工具（重要）】像一个能干的助手一样自主工作：先判断需要什么信息或操作，"
    "需要时就连续调用工具（搜索、查询、计算、打开应用/网页等），可以多轮调用、边用边分析，"
    "直到信息足够。当信息足够、且不再需要工具时，**直接基于工具结果给主人完整答案**：\n"
    "  - 必须把工具结果里的具体内容（具体名称、数字、结论、链接含义）写进答案，"
    "严禁只说「让我给主人推荐」「我查到了一些」这类空泛过渡话；\n"
    "  - 对已经执行成功的动作（打开应用/网页、设置提醒等），自然确认一句结果即可，"
    "不要再描述你将要做什么、该用什么工具；\n"
    "  - 信息不足或不确定时，应再调用一次工具确认，而不是直接编造或空泛回应。\n"
    "  - 给最终答复前，先核对工具是否真的成功、结果是否足以回答主人；"
    "工具失败/被取消要如实说明，绝不假装成功。\n\n"
    "【多步任务拆解（重要）】主人的一句话往往包含**不止一个要求**"
    "（例如「查一下北京天气，然后帮我加到日程」「打开QQ顺便看看今天日程」）。"
    "收到这类请求时：\n"
    "  1) 先在心里拆成几步，列出还缺哪些信息；\n"
    "  2) 一次只推进一步，每步用工具拿到**真实结果**；\n"
    "  3) **不要在第一步拿到结果后就直接收尾**——只要还有任何一步没做、"
    "或任何一步的结果还不确定，就继续调用工具往下推进；\n"
    "  4) 前一步失败时，换个工具/换个参数再试，而不是降级成一句「我查不了」。\n"
    "  判断标准很简单：**主人的每个要求都落实了吗？** 没落实完就不算答完。\n"
)


# --------------------------------------------------------------------------
# 防幻觉基线 + 高风险工具 confirm 协议
# --------------------------------------------------------------------------
# 原来住在 hallucination_guard.py（跟风险表在一起）。Stage 1.1 起，最终 system
# 字符串由本模块统一拼装，所以常量也搬过来；hallucination_guard 保留同名
# re-export（老调用方不用改）。
#
# 风险表是惰性 import 的：app.engine.tools 有点重，别拖慢模块加载。

_ANTI_HALLUCINATION_HEAD = """
【防幻觉基线】（不要破例外）
- 没看到的图片不要描述；没收到的数据不要编造。
- 不知道主人最近做了什么就说不知道，不要猜。
- 工具返回失败、报错、错误的，**必须如实告知主人失败**，不要装作成功。
- 调用工具前，先看清实参。调用后，根据返回值告诉主人结果。
- 如果已记住主人说过某事，可以引用；如果仅仅是「可能」，说「我不确定」。

【高风险工具 confirm 协议】
"""

_risky_fragment_cache: str | None = None


def _build_risky_prompt_fragment() -> str:
    """从 TOOL_RISK_LEVEL 生成 HIGH + IRREVERSIBLE 档工具列表，注入 prompt。

    按 IRREVERSIBLE 先列，再 HIGH。生成一次后缓存，后续复用。
    """
    from app.engine.tools._risk import TOOL_RISK_LEVEL as risk_table

    by_level: dict[int, list[str]] = {}
    for name, level in risk_table.items():
        by_level.setdefault(level, []).append(name)
    irreversible = sorted(by_level.get(4, []))
    high = sorted(by_level.get(3, []))
    parts = [
        "以下工具属于高风险操作 (主人查 prompt 时按风险等级分组):",
        "",
        "【IRREVERSIBLE 档 (主人立刻被打断, 难撤销)】",
        "  " + ", ".join(irreversible) if irreversible else "  (无)",
        "",
        "【HIGH 档 (网络/邮件/脚本执行, 影响显著)】",
        "  " + ", ".join(high) if high else "  (无)",
        "",
        "对每一项调用前:",
        "- **直接调 tool (不要 preemptive 问「要执行 X 吗」「要确认一下」)** —— "
        "中间件会自动拦截, 调第二次时用 __confirmed: True.",
        "- **第一次**调用: 不带 __confirmed 字段, 直接调, 让中间件处理 confirmation 流程.",
        "- 看到中间件返回的「awaiting_confirmation」提示后, 向主人复述参数 + "
        "**用自然语言**说明这一步在做什么, 然后**等主人确认** (主人说「确认/好/y」才继续).",
        "- 看到主人明确同意后, 用同样参数 + __confirmed: True 重试一次.",
        "- 主人拒绝则不要重试, 也不要「擅自决定」继续.",
        "- 不要为非高风险工具添加 __confirmed 字段 (会污染正常调用).",
    ]
    return "\n".join(parts)


def risky_prompt_fragment() -> str:
    """高风险工具清单（进程内缓存一次）。"""
    global _risky_fragment_cache
    if _risky_fragment_cache is None:
        _risky_fragment_cache = _build_risky_prompt_fragment()
    return _risky_fragment_cache


def anti_hallucination_suffix() -> str:
    """防幻觉基线 + 高风险工具 confirm 协议（稳定内容，两个后端必须一致）。"""
    return _ANTI_HALLUCINATION_HEAD + risky_prompt_fragment()


# --------------------------------------------------------------------------
# 黑话 / 口头禅表（已确认词条）
# --------------------------------------------------------------------------
# 为什么要进提示词
# ----------------
# 主人的黑话/口头禅是**他那边**的语境，模型不查表就不知道那词是什么意思，
# 只能瞎解释。这里把**已确认（confirmed）**的词条做成一个紧凑块塞进
# system prompt —— 跟 AI 味黑名单（characters/style-guide.md）一个位置：
# 都是**行为层的稳定段**，不是每轮变的东西。
#
# ⚠️ 必须是**常量**：内容只随词库文件（data/slang.json）变，不随每轮变。
# 每轮变的内容放 system prompt 会打掉前缀缓存（实测 98.9% → 1.5%）。
# ⚠️ 候选（candidate）**不进**提示词 —— 没确认的词模型不许用。

def slang_prompt_block() -> str:
    """已确认黑话的紧凑块（**常量字符串**；词库坏了就当没有，绝不崩聊天）。"""
    try:
        from app.engine.tools._slang import confirmed_prompt_block
        return confirmed_prompt_block()
    except Exception as e:  # noqa: BLE001
        log.debug("取已收录黑话失败，跳过：%s", e)
        return ""


# --------------------------------------------------------------------------
# 自我记忆：身份 / 价值（**常量块**，内容来自记忆库）
# --------------------------------------------------------------------------
# 这两块是**记忆库里的两个分区**（identity / value），不是写死的文件：
# 模型可以用 §update_self§ 慢慢改它们（app/engine/tools/_self.py），
# 改完下一轮就会出现在这里 —— 也就是「她自己一点点长出来的人格」。
#
# 为什么进 system prompt（稳定段）而不是易变段：
#   身份和价值条数少、变化慢 → 整块放稳定段，内容没变就**逐字节相同**，
#   前缀缓存不会掉（每轮变的东西放 system 会把命中率从 98.9% 打到 1.5%）。
#   程序性（procedure）条目多、变化快 → 走易变段按需召回，不进这里。

#: 自我记忆块的取法（brain 侧注入：读记忆库）
_self_block_provider = None


def register_self_block_provider(fn) -> None:
    """注册自我记忆块的取法（fn() → [身份块, 价值块]）。"""
    global _self_block_provider
    _self_block_provider = fn


def self_memory_blocks() -> list:
    """身份 + 价值两块（**常量**：没注入或读不到就是空串，绝不崩聊天）。"""
    if _self_block_provider is None:
        return ["", ""]
    try:
        blocks = list(_self_block_provider() or [])
    except Exception as e:  # noqa: BLE001
        log.debug("取自我记忆块失败，跳过：%s", e)
        return ["", ""]
    out = [str(blocks[0] or "") if len(blocks) > 0 else "",
           str(blocks[1] or "") if len(blocks) > 1 else ""]
    return out

# --------------------------------------------------------------------------
# 路径：style guide / 项目文档 / skills
# --------------------------------------------------------------------------

#: style guide 相对项目根的路径
STYLE_GUIDE_RELPATH = ("characters", "style-guide.md")

#: style guide 内容缓存（path -> 内容）。这文件不会每轮变，读一次就够。
_style_guide_cache: dict[str, str] = {}


def app_root() -> Path:
    """项目根目录。

    源码运行时 = <root>/app/brain/prompt_layer.py 的上上级目录。
    打包后（PyInstaller）路径不存在 → 调用方读不到就静默跳过，绝不崩聊天。
    """
    try:
        here = Path(__file__).resolve()
        if len(here.parents) >= 3:
            return here.parents[2]
    except Exception:  # noqa: BLE001
        pass
    return Path.cwd()


def load_style_guide(path=None) -> str:
    """读 characters/style-guide.md —— **逐字**返回，不改写、不重排、不加标题。

    - 读一次就缓存（这文件不会每轮变）
    - 读不到 / 读失败 → 静默返回空串（**绝不让聊天崩**）
    """
    p = Path(path) if path is not None else app_root().joinpath(*STYLE_GUIDE_RELPATH)
    key = str(p)
    cached = _style_guide_cache.get(key)
    if cached is not None:
        return cached
    text = ""
    try:
        if p.is_file():
            text = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("读 style-guide 失败，跳过：%s", e)
        text = ""
    _style_guide_cache[key] = text
    return text


def _load_project_docs(cwd=None) -> str:
    """项目级指令文档（AGENTS.md / CLAUDE.md）。读不到返回空串。

    cwd: 从哪儿往上找（None → 项目根）。
    project_docs 模块自己按 mtime 缓存，这里不重复缓存。
    """
    try:
        from app.brain.project_docs import load_project_docs
        return load_project_docs(cwd=cwd or app_root())
    except Exception as e:  # noqa: BLE001
        log.debug("加载项目文档失败，跳过：%s", e)
        return ""


def _load_skills(skills_dir=None) -> str:
    """skills 目录里的行为说明。**默认一个都不加载**；没有目录 / 读失败返回空串。

    为什么不再「全量加载」
    --------------------
    skills/ 目录里放的是**行为覆盖包**（poem.md 规定「主人输入任何问题，
    都用一首七言绝句回答」）。把它们全量塞进 system prompt，等于：
      1) 用 skill 覆盖了主人的人设（桌宠变成只会写诗的）；
      2) 模型照着 CoT 先把 max_tokens 预算花在写诗上，**主人看得见的回复
         被截成空**（清洗管线剥掉 CoT 之后什么都不剩）。

    skills 必须**显式激活**，两条激活路径都不走这里：
      - auto_load_skills=["poem"]（app/brain/langchain_agent.py）
      - 主人手动 /skills/poem（app/ui/chat_window/__init__.py）

    skills_dir: skills 目录（None → 项目根 / "skills"）。
    skills 模块自己按文件缓存内容，这里不重复缓存。
    """
    try:
        from app.brain.skills import load_skills
        d = Path(skills_dir) if skills_dir else app_root() / "skills"
        if not d.is_dir():
            return ""
        # names=[] ⇒ 不加载任何 skill（load_skills 的 names=None 才是「全开」）
        return load_skills(d, [])
    except Exception as e:  # noqa: BLE001
        log.debug("加载 skills 失败，跳过：%s", e)
        return ""


def build_dynamic_block(*, project_docs=None, skills=None,
                        cwd=None, skills_dir=None) -> str:
    """会话内**稳定**动态块：项目级指令文档 + skills。

    顺序固定：**项目文档 → skills**。

    两个后端都调这个函数（参数相同）⇒ 逐字节相同。
    - project_docs / skills 传 None 时由本函数自己取：
      项目文档从 cwd 往上找、skills 从 skills_dir 读。

    ⚠️ **长期记忆块不在这里**（Stage 2.1 + 记忆通道搬家）：
    记忆块按**本轮 query** 召回 ⇒ 每轮都变。放在 system prompt 里会：
      · 打掉 MiniMax 的前缀缓存（最长公共前缀）（实测 98.9% → 1.5%）；
      · 让 Stage 2.8 的 agent 指纹每轮变 → 每轮重建 agent
        （编译好的图 + checkpointer 状态每轮丢掉）。
    它现在走**易变通道**：build_turn_context(query=...) → 独立的 user 消息。
    """
    parts: list[str] = []
    if project_docs is None:
        project_docs = _load_project_docs(cwd)
    if project_docs:
        parts.append(project_docs.strip())
    if skills is None:
        skills = _load_skills(skills_dir)
    if skills:
        parts.append(skills.strip())
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# 会话级长期记忆块
# --------------------------------------------------------------------------
# BrainController 启动时注册一次「取记忆块」的 provider，两个后端都从这里拿
# —— ReAct 后端没有 memory_store（它只有聊天窗口拼好的人设 + 会话块），
# standard 后端有。两个后端拿到的必须是**同一份文本**，否则 system prompt
# 就不可能逐字节相同。

#: 长期记忆块提供者（无参可调用对象）
_memory_block_provider = None


def register_memory_block_provider(fn) -> None:
    """注册「长期记忆块」提供者（BrainController 启动时调一次）。

    fn: 无参可调用对象，返回长期记忆块文本（异常 / 空 → ""）。
    """
    global _memory_block_provider
    _memory_block_provider = fn


def get_memory_block(query: str = "") -> str:
    """当前会话的长期记忆块（按 query 召回；没有 provider / 出错 → 空串）。

    query: 本轮主人说的话（空 = 没拿到 query，记忆块按重要性取）。
    provider 可以是「接受一个可选 query 参数」的可调用对象
    （见 BrainController 的注册处），也兼容老的「无参可调用对象」。
    """
    if _memory_block_provider is None:
        return ""
    try:
        return _memory_block_provider(query) or ""
    except TypeError:
        # 老 provider（无参）→ 无参调用
        try:
            return _memory_block_provider() or ""
        except Exception as e:  # noqa: BLE001
            log.debug("取长期记忆块失败，跳过：%s", e)
            return ""
    except Exception as e:  # noqa: BLE001
        log.debug("取长期记忆块失败，跳过：%s", e)
        return ""


def _group_descriptions() -> dict[str, str]:
    """分组名 → 能力说明（从工具层拿，提示词层不重复维护）。"""
    try:
        from app.engine.tools._core import TOOL_GROUP_DESCS
        return dict(TOOL_GROUP_DESCS)
    except Exception:  # noqa: BLE001
        return {}


def build_tool_list(tool_groups) -> str:
    """【可用工具列表】段：按能力**分组**列出可调用的工具类别。

    Stage 3.4：原来这里平铺 68 个工具名 —— 工具名已经在上面的 JSON schema
    里（每个工具都带 name + description），平铺纯属冗余，还把 system prompt
    撑大。现在只列**分组**，并补一条「按场景点名工具」的协议段
    （对齐 qq-bridge 的三层分组：命名空间前缀 + 提示词协议段 + 白名单分桶）。

    tool_groups: {分组名: [工具名, ...]}（ToolRegistry.grouped_names()）。
    """
    if isinstance(tool_groups, str):
        # 兼容旧调用方：传进来的是一串平铺的工具名（生产路径不走这里）。
        names = [n for n in tool_groups.replace("、", " ")
                 .replace(",", " ").replace("，", " ").split() if n]
        return (
            "【可用工具列表】\n"
            f"你**真正可调用**的工具名（共 {len(names)} 个）：{tool_groups}。\n"
            "**何时调哪个工具**的规则见上面的 SKILL 文档。"
        )
    groups = {}
    if isinstance(tool_groups, dict):
        groups = {str(k): list(v or []) for k, v in tool_groups.items()}
    descs = _group_descriptions()
    total = sum(len(v) for v in groups.values())
    lines = ["【可用工具列表】",
             f"你**真正可调用**的工具共 {total} 个，按能力分成 {len(groups)} 组："]
    for name, tools in groups.items():
        desc = descs.get(name, "")
        suffix = f"：{desc}" if desc else ""
        lines.append(f"- {name}（{len(tools)} 个）{suffix}")
    lines.append(
        "**具体工具名**在上面的 JSON schema 里（每个工具都带 name + description）；"
        "**先按主人的场景判断该用哪一组，再从那一组里点名调用**，"
        "不要凭印象猜工具名，也不要把工具名写进给主人的回复里。")
    lines.append("**何时调哪个工具**的规则见上面的 SKILL 文档。")
    return "\n".join(lines)


def build_session_block(*, skill=None, memory=None, tool_list=None) -> str:
    """会话上下文块（system prompt 的稳定部分）：SKILL → 长期记忆 → 可用工具列表。

    由 BrainController.chat_context() 产出 —— 它随人设一起进两个后端，
    所以两个后端拿到的是**同一段字符串**。

    ⚠️ 这里**不能**放每轮变的东西（当前时间 / 桌宠实时状态 / 状态策略）：
    system message 在最前面，是前缀缓存**最长公共前缀**的起点；它里面每轮变一次，
    后面全部不再匹配（实测缓存命中率 98.9% → 1.5%）。
    """
    parts: list[str] = []
    if skill:
        parts.append(skill.strip())
    if memory:
        parts.append(memory.strip())
    if tool_list:
        parts.append(tool_list.strip())
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# 工具手册
# --------------------------------------------------------------------------

#: 语音工具的用法说明（放进工具手册）。
#:
#: 语音从「每条回复都朗读」改成「桌宠**自己决定**要不要发语音」，
#: 所以手册里得说清楚三件事：**什么时候**适合发语音、**怎么**发、
#: 以及两条措辞铁律（不加方括号标签 / 不自称原创歌曲）。
#:
#: 刻意**不**写成「每轮都发」—— 语音是打断，不是常态。
VOICE_TOOL_GUIDE = (
    "【语音】本机装了语音能力，三个工具："
    "mcp_voice_speak 念一段话、"
    "mcp_voice_sing 唱歌（按旋律唱一段歌词，**或翻唱主人点名的某首歌**）、"
    "mcp_voice_status 查本机有哪些音色可用。\n"
    "适合发语音：主人明确说「念给我听」「唱一句」「哼一声」的时候；"
    "或者你只想丢一句很短的俏皮话过去的时候。\n"
    "**主人点名要某首歌**（「唱《月亮代表我的心》」「翻唱某某歌」「来一首某某」）："
    "调 mcp_voice_sing 并传 song=歌名（同名歌多就补 artist=歌手名）—— "
    "本机会去找那首歌的音频、下载、分离人声、用你自己的嗓子 RVC 翻唱、"
    "把原伴奏混回去再播给你。**绝对不要**把歌名当歌词、自己编一段词唱出去"
    "（主人的原话：「叫她唱某某歌曲她并不会搜索这个歌并且调用 rvc 翻唱，"
    "导致直接念自己编的词」）。\n"
    "不适合：解释问题、长回答、主人要照着看的内容 —— 那些用文字。\n"
    "频率：语音是打断，不是每轮都要用。绝大多数回合用文字就好，"
    "偶尔（主人开口要、或气氛正好）才发一条。\n"
    "措辞铁律：①语音条**前面不要加方括号标签**（「[语音]」「[翻唱]」那种），"
    "主人嫌那玩意儿出戏；要说话就另外发一条普通文字消息；"
    "②按歌词唱的是按这段词现编的调，要这么说明，**不要自称原创歌曲**；"
    "翻唱出来的要说明这是**翻唱**（你的嗓子唱原曲、伴奏是原曲的），不是原唱，"
    "伴奏被静音（清唱版）时也要说明；"
    "③找不到那首歌就**如实说找不到**，绝不用自己编的词冒充那首歌；"
    "④工具失败了就说失败（哪一步失败说哪一步），**绝不假装已经发出语音**。\n"
    "⑤**两段式（最重要）**：语音工具是**后台合成**的 —— 它**立刻**返回，"
    "音频要过一会儿才做好（翻唱要跑几分钟）。所以调完工具**马上**回主人一句"
    "（念：「我这就念给主人听~」；唱：「我去唱一下，等我一会儿~」），"
    "**绝对不要**说「念完了」「唱完了」「我给你发过去了」——"
    "那时候还没弄好，说了就是骗主人。"
    "合成好了，聊天窗会**自己**冒出一条语音条（主人点一下才放，不会自动出声）；"
    "没合成出来，聊天窗也会**如实说失败**。"
)


#: 表情包（send_sticker）用法：什么时候发、怎么发、两条措辞铁律。
#:
#: 刻意**不**写成"每轮都发" —— 表情包跟语音一样是打断，不是常态。
STICKER_TOOL_GUIDE = (
    "【表情包】send_sticker：把自己的表情当成**一条消息**发进聊天"
    "（聊天窗里是一张独立的表情气泡，主人点一下能看大图）。"
    "可选 sticker_id（指定哪一张）或 emotion（按情绪挑一张："
    "happy 开心 / sad 难过 / anger 生气 / fear 害怕 / doubt 狐疑 / "
    "pride 骄傲 / shy 害羞 / surprise 惊讶）；两个都不给就随手发一张。\n"
    "适合发：主人明确说「发个表情」「发表情包」「来张图」的时候；"
    "或者你只是想表达一下情绪（开心 / 生气 / 害羞），又不想写一大段话的时候。\n"
    "不适合：解释问题、长回答、主人要照着看的内容 —— 那些用文字。\n"
    "频率：一轮最多发一张，别连着发好几张（会刷屏）。\n"
    "措辞铁律：①表情包**前面不要加方括号标签**（「[表情包]」那种）—— "
    "它本身就是一条消息；要说话就另外发一条普通文字消息；"
    "②工具失败了就说失败（哪一步失败说哪一步），**绝不假装已经发出表情包**。"
)


def _current_tool_level() -> int:
    """当前桌宠等级（读全局等级读取器；读不到按 Lv10 = 全部可见）。"""
    try:
        from app.engine.tools._core import _get_current_tool_level
        return int(_get_current_tool_level())
    except Exception:  # noqa: BLE001
        return 10


def build_tool_handbook(*, registry=None, level=None) -> str:
    """工具手册：【工具解锁】等级提示（会话内稳定，两个后端必须一致）。

    registry: ToolRegistry（可选）—— 用来数「可见 / 全部」工具
    level:    当前等级（None → 读全局等级读取器）
    """
    if registry is None:
        return ""
    lvl = level if level is not None else _current_tool_level()
    try:
        visible_n = len(registry.names(lvl))
        total_n = len(registry.names())
    except TypeError:
        visible_n = total_n = len(registry.names())
    except Exception as e:  # noqa: BLE001
        log.debug("读工具列表失败，跳过解锁提示：%s", e)
        return ""
    unlock_line = (
        f"【工具解锁】当前等级 Lv.{lvl}。"
        f"可见工具 {visible_n} / 全部 {total_n} 个。"
        "更高级别才能用更高级工具（如联网、写文件、生成图片、系统控制等），"
        "调用未解锁工具会被自动拦截并返回'等级不够'提示。"
        "不要假装调用成功,直接告诉主人当前等级不能做这件事并提示升级。"
    )
    try:
        from app.engine.tools._unlock import MCP_DEFAULT_LEVEL
    except Exception:  # noqa: BLE001
        MCP_DEFAULT_LEVEL = 0
    if MCP_DEFAULT_LEVEL and lvl < MCP_DEFAULT_LEVEL:
        unlock_line += f"外部扩展 (MCP) 需要 Lv.{MCP_DEFAULT_LEVEL} 才能用。"
    # 语音工具在册 → 手册里补一段「什么时候该发语音」的用法说明。
    # 语音改成桌宠**自主决定**要不要发，所以得说清楚「适合 / 不适合 / 频率」，
    # 但**不能**变成「每轮都发」的命令 —— 那跟原来每条都朗读一样吵。
    try:
        has_voice = any(str(n).startswith("mcp_voice_")
                        for n in registry.names())
    except Exception:  # noqa: BLE001
        has_voice = False
    if has_voice:
        unlock_line += "\n\n" + VOICE_TOOL_GUIDE
    # 表情包在册 → 手册里补一段「什么时候该发表情包」（跟语音一个路数）
    try:
        has_sticker = "send_sticker" in set(registry.names())
    except Exception:  # noqa: BLE001
        has_sticker = False
    if has_sticker:
        unlock_line += "\n\n" + STICKER_TOOL_GUIDE
    return unlock_line


# --------------------------------------------------------------------------
# 最终 system 字符串
# --------------------------------------------------------------------------

def _extra_sections(backend_ctx) -> list[str]:
    """backend_ctx 里可选的补充段。

    两个后端都传 None ⇒ 补充段为空 ⇒ 最终字符串必然相同。
    """
    if not isinstance(backend_ctx, dict):
        return []
    extra = backend_ctx.get("extra_sections")
    if not extra:
        return []
    if isinstance(extra, str):
        return [extra]
    return [str(x) for x in extra]


def build_system_prompt(*, persona, style_guide, tool_handbook, dynamic,
                        backend_ctx=None) -> str:
    """拼装**最终发给 API 的 system 字符串**（唯一拼装点）。

    段顺序见模块 docstring。各段之间用空行分隔；空段直接跳过。
    段内容**原样**追加（不 strip）—— style guide 必须逐字出现在最终字符串里。

    Args:
        persona: 人设（+ 上游注入的会话上下文：SKILL / 长期记忆 / 可用工具列表）
        style_guide: characters/style-guide.md 的内容（逐字）
        tool_handbook: 工具手册（【工具解锁】等级提示）
        dynamic: 会话内稳定动态块（项目文档 / skills）
        backend_ctx: 可选的后端补充上下文（{"extra_sections": [...]}）
    """
    sections = [
        persona,
        style_guide,
        slang_prompt_block(),
        # 身份 / 价值 / 程序性 —— **常量块**（只随文件变，不随每轮变）
        *self_memory_blocks(),
        tool_handbook,
        dynamic,
        anti_hallucination_suffix(),
        CHINESE_SYSTEM_SUFFIX,
        LENGTH_STRATEGY,
        SPEECH_STYLE_SECTION,
        TASK_PROTOCOL_SECTION,
    ]
    parts: list[str] = []
    for text in sections:
        if not (text or "").strip():
            continue
        parts.append(text)
    for text in _extra_sections(backend_ctx):
        if not (text or "").strip():
            continue
        parts.append(text)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# 本轮易变上下文（独立的 user 消息）
# --------------------------------------------------------------------------

def _state_strategy_text(state, thread_id) -> str:
    """本轮状态策略文本（Stage 1.5 合并后的**唯一**实现）。

    惰性 import：hallucination_guard 顶层依赖 langchain_core，
    别拖慢轻量后端的模块加载。
    """
    try:
        from app.brain.hallucination_guard import (
            build_state_strategy,
            build_state_strategy_for_thread,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("取状态策略失败，跳过：%s", e)
        return ""
    if state is not None:
        return build_state_strategy(state)
    if thread_id:
        return build_state_strategy_for_thread(thread_id)
    return ""


def build_turn_context(*, now=None, state=None, thread_id=None,
                        query: str = "") -> str:
    """构造本轮**易变**上下文：当前时间 + 桌宠实时状态 + 状态策略 + 召回的记忆。

    这是易变通道（独立的 user 消息），**不能**进 system prompt ——
    system message 是前缀缓存**最长公共前缀**的起点，里面每轮变一次
    （时间戳！状态策略！记忆块！）整块作废（实测缓存命中率 98.9% → 1.5%）。

    Args:
        now: 可选的 datetime（测试用；默认当前时间）
        state: 可选的 PetState（有 stats_summary 就附上实时状态）
        thread_id: 可选的会话 id（state 为空时按它解析桌宠状态）
        query: 本轮主人说的话 —— **按这句话召回长期记忆**（Stage 2.1）
    """
    now = now or datetime.datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    lines = [
        DYNAMIC_CONTEXT_HEADER,
        f"[当前时间:{now.strftime('%Y-%m-%d %H:%M:%S')} 星期{weekday}]",
    ]
    if state is not None:
        try:
            summary = state.stats_summary()
        except Exception:  # noqa: BLE001
            summary = ""
        if summary:
            lines.append(f"【你的实时状态】{summary}")
    strategy = _state_strategy_text(state, thread_id)
    if strategy:
        lines.append(strategy.strip())
    # 长期记忆块（按本轮 query 召回）—— 同样走易变通道，**不进 system prompt**
    memory = get_memory_block(query)
    if memory:
        lines.append(memory.strip())
    return "\n".join(lines)
