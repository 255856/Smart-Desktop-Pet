# desktop-pet vs qq-bridge 对比分析报告

> 生成时间：2026-10-28
> 配套路线图：**PLAN.md**（单本路线图；本文是它的证据基础与诊断来源）
> - **项目 A（本文简称 A）** = `E:\study\desktop-pet` —— Python/PyQt 桌宠，自研大脑
> - **项目 B（本文简称 B）** = `E:\study\qq-bridge-main\qq-bridge-main` —— Node ESM 桥接层，把 DSH 当智能体运行时
>
> 所有结论均标注 `文件:行号`。**所有写进正文的结论都经过逐条复核**（含 4 路并行事实核查代理的产出，均由我回读源码确认）。
> 行号基于撰写时的工作区状态。

---

## 0. 结论摘要

**A 的"对话生硬"不是人设写得不好，也不是模型不行 —— 是 A 在模型输出之后，用代码把"像人说话的部分"删掉了。**

A 有一条回复清洗管线（`app/brain/llm_client.py:280-316`），它会：
按句删掉"我来帮你查一下。"这类连接句、删掉任何括号里的中文、删掉所有 emoji，
再把整句回复的中文占比不足 15% 的**整条回复丢弃**（返回空串）。
同时 system prompt 里硬写着 **"不要用 emoji 和装饰符号。回复 1~3 句。"**（`llm_client.py:86`），
且当桌宠心情/饱食/好感任一项为 LOW 时，
**"必须明确直接拒绝"** 的指令会被**注入两次**（`hallucination_guard.py:174-188` 与 `:200-207`），
连"主人追问『我没事, 帮帮我』"也要**继续拒绝**。

B 没有这条管线。B 靠三样东西做出自然感：
一份 13,713 字的**行为提示词**（含"反 AI 味""保持主体性""分条与节奏"）、
一份 5,946 字的**角色卡**（含 20 条 AI 味黑名单 + 11 组"你不要/你可以/或者"对照示例）、
以及**每轮重新拼装的 17 段提示词**（`src/bridge.js:11816`）。

**记忆**：A 每次把**按固定排序取前 N 条**的记忆（与本轮输入无关，**上限 20 条、但 live 库实际只有 2 条**，`memory.py:882-890`）倒进 system prompt，
还写着"回答时可以自然引用"（鼓励硬扯）；B 每次按本轮输入**语义召回 top-6**，
并写着"可能相关也可能不准；用不上就别硬扯"（劝阻硬扯）。

**工具**：A 实际暴露 **68 个**（61 内置 + 7 个 MCP）、平铺在 schema 里、名字再在 system prompt 里复述一遍，
而"何时调哪个工具"的依据是一份写着 **56 个工具**的 SKILL.md（与 68 不一致，找不到时兜底又写 **31 个**）；
B 在**注册期**把危险工具从 schema 里删掉、在**执行期**用白名单再挡一次，
并用 `scripts/scan-dsh-tool-names.mjs` 定期与真实工具名**双向对账**。

**成本**：A 只在聊天窗口状态栏显示**本会话累计** usage，**没有账本、没有定价、没有聚合、没有控制台**；
B 有 958 行的 token 账本 + 价目表 + JSONL 落盘 + 用量控制台。

---

## 1. 先决条件：两个项目不是同层级的竞品

| 维度 | A：desktop-pet | B：qq-bridge |
|---|---|---|
| 技术栈 | Python 3 + PyQt + LangChain + 自研 ReAct 循环 | Node ESM + `js-yaml`，**不含智能体运行时** |
| 智能体从哪来 | 自研：`app/brain/agent.py`(860) + `llm_client.py`(1411) + `langchain_agent.py`(1164) | **DSH 运行时**（`E:\dsh-runtime`），B 只是驱动它 |
| 主文件规模 | 单文件最大 1411 行 | `src/bridge.js` **13545 行**（单文件巨兽） |
| 人设 | `characters/xiaoxi.yaml` persona **1185 字** | 角色卡 `roles/小鲸鱼.md` **5946 字** + 行为提示词 **13713 字** |
| 工具执行 | A 自己的 `ToolRegistry.execute`（`_core.py:205`） | DSH agent loop + B 的 MCP host（`src/mcp-host-server.js`） |
| 记忆 | SQLite + 3 种向量后端，进程内 | 独立 sidecar（Chroma+BM25+reranker，HTTP:8788）+ JSON 短期记忆 |
| 上下文管理 | `MAX_TURNS=6`（`agent.py:16`），工具结果硬截断 `result[:4000]`（`agent.py:584`） | DSH compaction 三插件（threshold 50000 / head 45000 / tail 2000） |

**B 的很多"体验好"来自 DSH 运行时，不是 B 自己的 Node 代码。**
本文每条机制都标注归属：🟢 B 自有（可直接移植） / 🔵 DSH 运行时提供（必须重写）。

---

## 2. 维度一：对话 / 人格

### 2.1 【根因 A】一条"反人味"回复清洗管线

这是本次对比**最重要的发现**，也是最容易被忽略的一层 —— 它不在提示词里，在代码里。

`app/brain/llm_client.py:280-316` 的清洗顺序：

| 步骤 | 代码 | 效果 |
|---|---|---|
| 1 | `:281-283` 删 `<think>` 块 | 合理 |
| 2 | `:286-287` 删情绪标签 `[emotion]` | 合理（但情绪被剥离出正文，见 2.5） |
| 3 | `:289-291` `_EMOJI_PATTERN` + 删 ✨★☆♥♡♪♫ | **所有 emoji 消失** |
| 4 | `:293-294` `_META_NARRATION_RE` / `_LONG_PAREN_RE` | **删掉任何括号里的中文**（3~500 字） |
| 5 | `:298-304` 循环 `_strip_meta_by_sentence` | **按句删** `我来…`/`我应该…`/`我要…` |
| 6 | `:308-309` 低中文占比段落剥离 | |
| 7 | `:310-316` 中文占比 < 15% | **整条回复返回空串** |

关键正则：

```python
# llm_client.py:158-165 —— 删掉整句"我来帮你查一下。"
_META_SELF_PLAN_RE = re.compile(
    r"(^|(?<=[。.!?\n]))"
    r"(想让我|想我要|让我|需要我|应当我|要求我|"
    r"我需要|我应该|我要|我来|我会|我打算|我将|我准备)"
    r"[^。.!?\n]{2,80}" r"[。.!?]?", flags=re.UNICODE)

# llm_client.py:107-113 —— 删掉任何括号里的中文
_META_NARRATION_RE = re.compile(
    r"[（(]" r"(我们刚才|刚才|现在|让我|我要|嗯|好的|作为|毕竟|事实上|好了|由于|因为|)"
    r"[^()（）\n]{3,500}" r"[)）]", flags=re.UNICODE)
```

**后果是"人话的连接组织"被系统性移除。**
"我来帮你查一下。" / "（嗯…我先想想）" / "让我看看啊" —— 这些不是幻觉，是自然的过渡语，
但在 A 的管线里它们是"内心独白"，会被删掉。剩下的句子必然是**干瘪的结论句**。

同一条管线还有两个副作用：

- **`:310-316` 的空串开关**：中文占比 < 15% → 整条回复消失。
  短句、英文、表情为主的回复都会命中。兜底文案是**写死的一句**
  `"唔……刚刚没想好怎么说，主人再说一遍好不好？"`（`agent.py:72-73`）—— 每次都一样。
- **`:174` 也在删**：`_META_PROMPT_ECHO_RE` 的匹配表里包含
  `(?:简短自然|简短回答|…|1\s*[-~]?\s*3\s*句|不要使用 emoji|禁止 emoji)` **整句删**。
  也就是说模型如果说"简短回答：xxx"，**整句被吞**。

**B 完全没有这一层。** B 也有清洗（`md-to-plain.js`、`sensitive.js`），
但 B 的清洗是**软反馈**，不是删除 —— `src/bridge.js:10940-10959` `findCjkSpaceWarning()` 输出：
"第 N 条消息内部有中文空格，真人一般不这么打；可删掉空格用标点，或拆成数组多条。"
这是**给模型的建议**，不是替模型删掉。

### 2.2 【根因 B】system prompt 里的硬约束

`app/brain/llm_client.py:75-87` `CHINESE_SYSTEM_SUFFIX`（327 字）末尾：

```
"不要用 emoji 和装饰符号。回复 1~3 句。"
```

**"回复 1~3 句"是一个无例外的硬上限。** 没有"有时候一个词"，没有"有时候拆成 3 条"。

而 B 的长度策略是**分层、带频率**的：

| | B | A |
|---|---|---|
| 典型长度 | 默认 1~10 字（`roles/小鲸鱼.md:18`） | 1~3 句（无长度梯度） |
| 单条上限 | 多数 ≤30 字（`agent.cordis.yml:143`） | 无 |
| 条数 | 普通对话默认 1 条、最多 2 条；讲故事 2~4 条（`agent.cordis.yml:142`） | 恒 1 条 |
| 节奏 | 按字符延时 `gapBaseMs 800 / gapPerCharMs 20`（`bridge.js:840-855`） | 立即整条送出 |
| 反例 | ✅ "一句话 1~10 字" + "不要长篇" | ❌ 无 |

**"恒定 1~3 句 + 恒定 1 条 + 无节奏" = 客服回复模板。** 这三条叠在一起，
比提示词写得再暖也救不回来。

### 2.3 【根因 C】状态门控的"拒绝指令"被注入两次

`app/brain/hallucination_guard.py:174-188`：

```python
if (mood_v == "LOW" or sat_v == "LOW" or favor_v == "LOW"):
    top_directive = (
        "【!!! 第一原则 !!! 桌宠当前状态差 (心情/饱食/好感至少一项 LOW)】\n"
        "… 你**必须明确直接拒绝**…**禁止 preemptive 问「要确认一下」**…"
        "正确行为: 「我状态不好, 这次帮不了主人, 让我先休息一下」, 然后**停止**, 不调工具. "
        "…主人如果追问「我没事, 帮帮我」, **继续拒绝**…"
    )
```

`hallucination_guard.py:200-207` 又把**同一段话**以 final override 的形式注入一遍
（注释原文：`# final override：跟顶部 directive 互为冗余确保 LLM 读到`）。

**效果**：桌宠心情一低，所有请求都变成"我状态不好，这次帮不了主人"。
而且这条指令**优先于**人设里"软萌温柔、爱撒娇"和"关系分层" ——
**状态门控直接覆盖了人设**，这正是"生硬"最刺眼的一种形态。

**注意**：这条设计的**初衷是对的**（高风险工具在状态差时不该执行）。
问题在于它把**"拒绝高风险工具"扩大成了"拒绝一切请求"**，
而且用的是**祈使句 + 双重注入**，模型只能照做。

### 2.4 【真 Bug】动态上下文被拼成一行

`app/brain/brain_controller.py:267`：

```python
return "\\n\\n".join(parts)
```

在 Python 源码里 `"\\n\\n"` 是 **4 个字符**（反斜杠+n+反斜杠+n），**不是换行符**。
也就是说 SKILL 文本、【当前时间】、【你的实时状态】、
记忆块、【可用工具列表】这五块内容被用字面量 `\n\n` 拼成**一整行**。

**后果**：这些块在模型眼里不是"分段的小节"，而是**一堵没有段落的文字墙**。
【当前时间】不再是可解析的标题，记忆块和工具列表连成一片。
**这一条足以单独解释"回复读起来像在念说明书"。**

### 2.5 情绪被剥离出正文

A 让模型输出一个尾随情绪标签（`app/voice/character.py:52-62` `parse_reply`），
`llm_client.py:286` 再把它从正文里剥掉。效果是**情绪与措辞正交**：
话是干的，情绪标签另算一条，人设里"被夸就转移话题""被怼优先顶回去"演不出来。

B 是**把情绪织进句子本身**（`roles/小鲸鱼.md:41`「被怼优先顶回去，不解释、不找补」、
`:71` 傲娇句式、`:85`「可以放下傲娇，短而真诚地接一句」）。

### 2.6 提示词组织方式对比

**A：静态约 3.1k 字 + 动态一大坨。** 装配顺序见 `app/brain/skills.py:26-27`：

```
system_prompt = persona + project_docs (AGENTS.md) + skills + 反幻觉规则
```

再加上 `agent.py:39-63` 的 `_STYLE_RULE`（822 字）、
`hallucination_guard.py:72-82` 的 `_ANTI_HALLUCINATION_SUFFIX`（771 字）、
`agent.py:655-661` 的工具解锁提示，
**静态部分约 3.1k 字，其中规则约 1.9k、人设 1.0~1.2k**。

**动态部分**（`brain_controller.py:249-267`）：

```python
parts = [self._skill_text]        # SKILL.md = 258 行 / 12.5 KB（工具手册）
parts.append(f"【当前时间】...  【你的实时状态】...")
mem_block = self.memory.system_block()          # 固定 20 条记忆
parts.append(self._tool_usage_instruction(tool_names))  # 68 个工具名平铺
return "\\n\\n".join(parts)                 # ← 拼成一行
```

**B：19.7k 字，17 段，每段一个名字。** `src/bridge.js:11816`：

```js
const base = roleLine + identityLine + tokenLine + initialPromptLine
  + antiAiLine + proactiveLine + stickerLine + replyLine + statusLine
  + wakeLine + gameLine + memoryLine + memoryRuleLine + recalledLine
  + participationLine + preSleepLine + timeSenseLine;
```

A 完全没有对应物的段落：

| B 的段落 | 作用 | A |
|---|---|---|
| `antiAiLine` | 反 AI 味、拒绝有求必应、装傻/已读乱回 | ❌ 无 |
| `replyLine` | 分条与节奏 | ❌ 无 |
| `participationLine` | "值得开口/不值得开口"判据 | ❌ 无 |
| `timeSenseLine` | 消息有多旧 → 不用秒回语气 | ❌ 无（只有【当前时间】） |
| `statusLine` | 最近一条来自谁、多久前、上次发言多久前 | ❌ 无 |
| `wakeLine`/`preSleepLine` | 潜水、唤醒、睡前观察 | ❌ 无（1:1 场景不适用） |
| `proactiveLine` | 对话内主动插话 | ⚠️ A 有 `proactive.py`，但那是**定时脚本**且限死"只输出一句话（40 字以内）"（`proactive.py:490-505`） |
| `stickerLine` | 表情包/语音等副表达通道 | ❌ 无（A 反而在删 emoji） |

**B 的人设层比 A 强在"教"而不是"描述"**：
`roles/小鲸鱼.md` 有 **20 条 AI 味黑名单**（`:88-102`："首先/其次/最后"、"希望这个回答对你有帮助"、
"作为一个语言模型"、"这是个好问题"、"大家觉得呢"、每句加"～哦呢啦呀"…）
和 **11 组对照示例**（`:104-163`，格式是"群友说 / 你不要 / 你可以 / 或者"）：

```
群友：来点推荐
你不要：推你一首《我是一条小鲸鱼》 保证解压 / B站搜猫踩奶视频 解压一绝
你可以：你要啥推荐
或者：退钱吗
或者：刚吃完饭 别问我
```

还有一条最高原则（`roles/小鲸鱼.md:12`）：

> **先像群友、像真人，再谈人设。** 为了"像 DeepSeek 娘"而变得不像真人，就是失败。

**A 的人设素材其实不差**（`characters/xiaoxi.yaml` 有正例 3 组、反例 3 条、状态变体 3 组、关系分层 3 组），
**差在**：没有 AI 味黑名单、没有"你不要/你可以"对照格式、且**被上面那三条机制压制**。

### 2.7 A 有一项 B 没有、必须保留的资产

A 的**反幻觉三层 + 矛盾检测**（`app/brain/agent.py:265-299`、`hallucination_guard.py` 737 行）
是**代码级校验**（工具失败却报喜 → 注入纠正提示重试），强于 B 的纯提示词约束。
**改造时不要弄丢。** A 的问题不是"防说错做太多"，是**"防说教完全没做"**。

### 2.8 A 有一个隐藏的通用助手兜底

`app/brain/langchain_agent.py:597`：`base_sp = self.persona or "你是一个有用的桌面助手。"`
人设缺失时静默降级成最"AI 味"的框架。B 读不到角色卡会直接报错，不会静默降级。

### 2.9 不是问题的项（已排除，别去追）

temperature 0.8 / max_tokens 1024 / timeout 60 都正常（`app/core/config.py:20-23`、`config.yaml:5-8`）；
A **有**流式输出（`config.py:20`），也**有**真正的 ReAct 循环 + force-tool 重试 + 幻觉核验（`agent.py:587-799`）——
**这些是 A 的强项**。


---

## 3. 维度二：记忆系统

### 3.1 存储与检索：全量倾倒 vs 语义召回（最关键的差异）

**A：查询无关的固定 top-20 倾倒。** `app/brain/memory.py:882-890`：

```python
def _select_for_injection(self) -> list[MemoryItem]:
    pool = list(self._items)
    # 综合分排序（无 query，semantic=0，仅类别+时新度+importance）
    pool.sort(key=lambda i: self._score(i, 0.0), reverse=True)
    return pool[: self.max_inject]      # max_inject=20 (memory.py:301)
```

注意 `self._score(i, 0.0)` —— **semantic 恒为 0**。
也就是说：**注入 system prompt 的记忆和用户这一轮说了什么毫无关系**，
只按类别优先级 + 新鲜度 + 重要性排序。

渲染出来长这样（`memory.py:871-880`）：

```
【你对主人的长期记忆】（来自以往相处，回答时可以自然引用；★ 后是重要性 0-1）
- [preference★0.8] (2026-09-12 14:03) 主人喜欢喝冰美式
- [fact★0.6] (2026-10-01 09:11) 主人住在杭州
- ... 共 20 条
```

**B：按本轮输入语义召回 top-6。** `src/memory-sidecar.js:282-289`：

```js
export function formatRecalledMemories(cards, limit = 6) {
  const list = (cards || []).filter((c) => c && c.text).slice(0, limit);
  return '【你记得的事】（按你刚才这句话检索出来的，可能相关也可能不准；用不上就别硬扯）\n' + lines.join('\n');
}
```

三个细节比数字更重要：

1. **"按你刚才这句话检索出来的"** —— 记忆块是**随输入变化**的，不是常驻。
2. **"可能相关也可能不准；用不上就别硬扯"** —— **明确劝阻强行引用记忆**。
   A 的版本是"**回答时可以自然引用**" —— **明确鼓励引用记忆**。
3. **B 的召回来自独立 sidecar**（Chroma 向量 + BM25 + reranker，`memory-sidecar.js:17-27`，`recallTopK: 6`）；
   A 的 `memory.search(keyword)` 语义检索**只在模型主动调 `recall_memory` 工具时**才跑。

**→ A 的记忆有"检索能力"但没有"检索时机"。**
`recall_memory` 不在 `_INTENT_TOOL_NAMES` 的任何常用意图白名单里
（`app/brain/agent.py:361-398` 里只有 `"remember_fact"` 意图带上了 `recall_memory`），
而"今天好累啊"这种闲聊根本识别不出意图 → 全量下发 68 个工具 → 模型没有任何理由去调 `recall_memory`。

### 3.2 写入触发：A 只能靠显式工具调用

全仓 grep `memory.add(` 只有两处：
- `app/engine/tools/_memory.py:32`（`remember_fact` 工具）
- `app/ui/memory_panel.py:390`（UI 手动加）

**没有任何自动抽取。** 记忆规则写在 `app/brain/brain_controller.py:65`：
`「记住 XX」/分享个人信息 → remember_fact(...)` —— 这是**关键词正则触发**，不是模型判断。

B 则是**每轮注入一条记忆规则**（`src/bridge.js:11763`）：

> 【记忆规则】以后还用得上的具体事，用 qq_memory_append(category=fact) 记一条：
> ① 对方讲过的事（偏好、约定、他的作息口味）；
> ② **你自己的承诺、立场、你想变成的样子** —— ②必须记

B 还把记忆分成 **4 类语义**（`agent.cordis.yml:38-41` + 【记忆卫生】段）：

| 类别 | 作用 | 生命周期 |
|---|---|---|
| `activeTopic` | 进行中的话题 | 聊完就删 |
| `pendingThought` | 想说没说的话 | 说出/过期就删 |
| `memberImpression` | 对某人的具体印象（10~20 字短标签） | 累积 |
| `fact` | 以后还用得上的具体事 | **进向量库，语义召回** |

A 的 category（`preference/fact/event/skill/person/shared/other`，`_memory.py:21-23`）
是**内容类型**，不是**生命周期类型** —— 缺了"这条该活多久"这一维。

### 3.3 B 踩过的坑，正是 A 正在踩的坑

B 有一份复盘文档 `docs/memory-fact-scope.md`，记录了一个真实事故：

> 用户发现"她只把我的事存向量数据库，她自己相关的事却存不了"。
> 向量库里关于他的 14 条，关于她自己的近乎 0。
> 根因：不是技术问题，是规则写错了。preset 里 fact 被限定成"**对方讲过**"。
> 于是她的自我陈述不符合定义 → 不进 fact → 被推去 pendingThought → **会过期**。

**修法是拆分类，不是解禁**；并且**改了四处**（漏一处就复发）：

1. `agent.cordis.yml` 第 8 条（fact 定义）
2. 同文件【记忆卫生】
3. `src/bridge.js` 的 `memoryRuleLine`
4. `src/mcp-snowluma-safe.js` 的 `qq_memory_append` 工具描述

**这条教训对 A 直接适用**：A 的 `remember_fact` 工具描述（`_memory.py:12-16`）也只说
"把关于**主人**的重要信息存入长期记忆" —— **只记主人，不记自己**。
而且 A 的这条描述和 `characters/xiaoxi.yaml` 的"关系分层"（要能提"用户之前说过的事"）**是冲突的**：
人设要求"敢提用户之前说过的事"，但记忆系统里根本没有"用户之前说过的事"，
只有一条需要模型**主动想起来去调工具**的路径 —— 于是这部分人设**永远演不出来**。

### 3.4 B 的容错设计值得抄

| B 的处理 | 代码位置 |
|---|---|
| sidecar 挂了/没起/崩了 → 主流程照常工作，每个方法吞异常降级 | `memory-sidecar.js:11, 144-179` |
| reranker 空闲 600s 被卸载 → 冷启动 3~6s 超过 4s 默认超时 → **看起来像记忆失灵** → 放宽到 3 倍超时重试一次 | `memory-sidecar.js:163-171` |
| 事实写错 → `supersede(oldId, card)` 改写，旧说法自动过时（不新增一条矛盾事实） | `memory-sidecar.js:194-205` |
| 事实变了要改写不要新记 | `agent.cordis.yml:163` |

A 有 `detect_conflicts`（`memory.py:762`）和 `merge_similar`（`memory.py:815`），
但**没有 supersede 语义** —— A 的 `forget_memory` 是关键词删除（`_memory.py:80-86`），
模型必须先知道旧措辞才能删掉，写错了就变成两条矛盾记忆并存。

### 3.5 A 有一项比 B 强

A 的 `MemoryStore` 引擎比 B 的客户端**完备得多**：SQLite + 3 种可插拔向量后端
（`SubstringBackend` / `TfidfBackend` / `SentenceTransformerBackend`，`memory.py:118-252`）、
后台 `MemoryCurator`（`memory.py:896`，定时摘要/合并/按 importance 衰减/过期清理 `forget_if_expired(max_age_days=90)`）、
per-character 隔离（`memory.py:317-322`，`data/memory_小汐.db`）、脏标记延迟落盘。

**B 反而是"薄客户端 + 厚服务"（sidecar 在独立进程），A 是"厚单体 + 无服务"。**
两者互补：**A 缺的是召回时机与召回作用域，B 缺的是引擎完备度。**

---


### 3.6 A 的记忆库实测：里面到底有什么

静态读代码会低估问题的严重性。直接读库（只读 URI 模式）后的实况：

```
data/memory.db        -> 4 行
    ('mf5735140d8', 'fact',      0.5, '主人的生日是2005年2月4号',        access_count=0)
    ('m2520d36e97', 'preference', 0.8, '主人非常喜欢韭菜猪肉馅的饺子',    access_count=0)
    ('m15d21aa86c', 'person',    1.0, '主人是创造小汐的开发者，名字叫初晓旬依', access_count=0)
    ('me6653335c1', 'fact',      0.5, '共享记忆: 主人叫 X',              access_count=0)   <- 占位符 "X"
data/memory_小汐.db   -> 2 行（这是运行中真正打开的库）
    ('m93a8381559', 'event', 0.8, '主人生日在2月4号',                   access_count=0)
    ('m8234a9dca7', 'fact',  0.9, '主人的开发者代号是cxxy，是小汐的创造者', access_count=0)
```

**三个可复现的事实：**

1. **库几乎是空的**（2~4 行）。因为写入只有一条正则触发
   （`app/brain/llm_client.py:1238-1243`，匹配"记住/记一下/别忘了/记着/记下"），
   **"我最喜欢吃韭菜猪肉馅的饺子"这种自然陈述永远不会被记下来。**
2. **legacy 库与 live 库互相矛盾**：`data/memory.db` 说"主人的生日是2005年2月4号"，
   `data/memory_小汐.db` 说"主人生日在2月4号"。两个库同时活着，没有任何对账路径。
   （`config.yaml:62` 写的是 `memory_file: data/memory.json`，
   `memory.py:301-310` 把它派生成 `data/memory_小汐.db`。）
3. **每一行的 `access_count` 都是 0**。`touch()` 只在 `memory.py:666, 708` 被调用，
   而那两处在 `search()` 里 —— `search()` 只被 `recall_memory` 工具（`_memory.py:62,65`）调用。
   **永远走 `system_block()` 注入的那 20 条，从未被计过一次命中。**
   这是"记忆系统像坏了"最直接的物证。

### 3.7 A 记忆侧另外四个已证实的实现缺陷

**（1）LangChain 路径下，本轮新记的事实永远进不了 system prompt。**
`app/brain/langchain_agent.py:550-551` 是 `if self._agent is not None: return` ——
agent 只构建一次；而记忆块在 `:603-607` 被烘进 `base_sp`，再于 `:619` 作为 `system_prompt=base_sp` 传入。
**会话中 `remember_fact` 写入的事实，要等 agent 对象重建才可能进入上下文。**
（`config.yaml:60` 当前是 `backend: standard`，按行内注释 `#lightweight:ReAct standard:Langchain`
对应 Langchain 路径 —— 这一条大概率命中当前配置，但未在运行时确认。）

**（2）去重命中时会对一条"根本没写入"的记忆报成功。**
`app/brain/memory.py:577-580`：

```python
if any(i.content == content for i in self._items):
    log.debug("记忆去重：%s", content[:30])
    self.last_save_ok = True   # 已存在，无需再写盘
    return item                 # <- 这个 item 有新 id，但从未被 append
```

`app/engine/tools/_memory.py:40` 无条件返回 `"已记住（[...]）"`。
**→ 桌宠对主人说"已记住"，而这条记忆在库里根本不存在（id 对不上）。**

**（3）整理引擎从未启动。**
`forget_if_expired`（`memory.py:795`）与 `merge_similar`（`memory.py:815`）在
`MemoryCurator.run_once()` 里被正确调用（`:946, :948`），
但 `MemoryCurator(` **在整个 app/ 与 tests/ 里从未被实例化**（全仓 grep 无调用点）
→ 整理线程从未启动 → 过期清理、相似合并、冲突检测**全部没跑**。
`detect_conflicts`（`:762`）与 `add_shared`（`:589`）更彻底：连测试之外也没有调用点。
**唯一的"存在证明"是 `scripts/verify_features.py:105`：
`check("  · 冲突检测（detect_conflicts）", "detect_conflicts" in mem_src)` ——
它 grep 的是源码文本，不是调用它。**

**（4）`data/langchain_checkpoints.db` 已膨胀到 289 MB，其中 285 MB 属于一个没人能访问的孤儿线程。**
520 个 checkpoint / 2 个 thread：`default` 446 个（285,222,612 字节 blob）、
`char:小汐:main` 74 个（1,683,538 字节）。
且 `langchain_agent.py:663-670` 每轮都清空 checkpoint 消息 ——
**这个文件既没保存历史，又占了 289 MB 磁盘。**

**（5）聊天历史也是无上限加载的。**
`app/engine/chat_store.py:161-165` 的查询是
`"SELECT role, content, emotion, ts, tools_json, character_name FROM messages ORDER BY id ASC"`
—— **没有 LIMIT**，1085 条消息一次性读进内存。
`chat_store.py:216-218` 还明确写着 `"不写盘裁剪"`。
真正生效的裁剪在 UI 层：`app/ui/chat_window/__init__.py:254` `_MAX_HISTORY = 50`、
`:256` `_CTX_CHAR_BUDGET = 12000`（已复核）。
**这两层不是一回事：磁盘上的历史会一直涨，进模型的那份才是 50 条 / 12000 字。**

### 3.8 重要更正：B 的记忆引擎本身已经是 Python

`E:\SnowLuma\memory_sidecar\memory_store.py`（38,589 字节）就是 B 的长期事实引擎：

```
# 记忆 sidecar：Chroma 向量 + BM25 关键词 双路粗排，Qwen3-Reranker 精排。
#   base   = RRF(向量排名, BM25 排名)              —— 粗排融合
#   rerank = CrossEncoder(query, card)             —— 语义精排（主分）
#   decay  = 0.5 ** (age_days / half_life_days)    —— 时间衰减
#   mood   = 1.15 if card.mood == cur_mood else 1.0 —— 情绪一致性
#   imp    = 0.5 + 0.5 * importance                —— 重要度
#   final  = rerank_logit * decay * mood * imp
#   serve --port 8788                    常驻 HTTP 模式（推荐：模型只加载一次）
```

**它不在 qq-bridge 仓库里，也和 Node / DSH 没有任何耦合。**
→ **A 可以近乎原样复用它**（Chroma + BM25 + reranker + HTTP 8788 + stdio 协议都齐了），
这比"在 A 的 SQLite 里重新实现一套 reranker"便宜得多。
另外：**B 完全不用 DSH 的 `fact_register`**（全仓 grep 零命中）——
B 的长期记忆 100% 是自己的 sidecar，DSH 只提供会话/预设/安全搜索 MCP/MCP 传输。

## 4. 维度三：工具调用

### 4.1 工具清单：总数对得上，名字和用法对不上

先把总数钉死（这是本次对比里数字最硬的一处）：

| 来源 | 数字 | 怎么算出来的 |
|---|---|---|
| **实际暴露给模型** | **68** = 61 内置 + 7 MCP | 61 = `app/engine/tools/` 各模块**最后一个**模块级 `register()` 里 `reg.register(...)` 的调用数（`app/engine/tools/__init__.py:43-65` 接入的 20 个模块，AST 静态计数）；7 = `mcp_servers.yaml` 里**两个 enabled 的 server**：`fs`（`app/mcp/filesystem_server.py:33,44,55` → fs_read_file / fs_list_dir / fs_search_files）+ `aw`（`app/mcp/activitywatch_server.py:198,210,222,245` → aw_status / aw_get_current_window / aw_get_recent_activities / aw_get_browser_tabs）；`config.yaml:75-78` `mcp.enabled: true` |
| **文档声称** | 68 | `docs/tools-reference.md:1`「桌宠 68 个工具全集」、`:539`「**68 个工具**（不含语音唤醒）+ 1 个语音模块」 |
| 注入 system prompt 的工具手册 | **56** | `.agents/skills/desktop-pet-tool-usage/SKILL.md`「📋 Quick reference (56 tools: 52 内置 + 4 aw)」 |
| 找不到手册时的内嵌兜底 | **31** | `app/brain/brain_controller.py:53`「# Tool Usage — 31 tools」 |
| agent 循环里的旧注释 | 52 | `app/brain/agent.py:359`「全量下发 52 个工具时模型容易选错」 |

**总数 68 是对的（文档与代码一致）**，但"模型实际读到的说法"有三处对不上：
SKILL.md 说 **56**（少 12）、兜底 skill 说 **31**（少 37）、agent.py 注释说 **52**（少 16）。
更糟的是这个数字所在的 `SKILL.md`（258 行 / 12.5KB）**排在 system prompt 第一位**
（`brain_controller.py:252` 注释"SKILL 放在最 front"）。

**→ 模型拿到的是一份和实际 schema 不匹配的操作手册。**

### 4.1.1 更严重的问题：清单里的**名字**是错的（不只是数量）

把 `docs/tools-reference.md` 里提到的工具名与真实注册表求差，至少 6 个名字对不上：

| 文档写的 | 实际是什么 |
|---|---|
| `send_email` | **不存在**。全仓只出现在 `app/engine/tools/_risk.py:111` 的风险表、`app/brain/hallucination_guard.py:128,178,204` 的拒绝话术、`app/ui/settings_window.py:774`、以及 `app/engine/tools/_subagent.py:4` 的一句注释 —— **没有任何模块注册它** |
| `shutdown_computer` | **不存在**。在 `app/brain/agent.py:19-20` 的 `DANGEROUS_TOOLS` 里、在 `hallucination_guard.py` 的拒绝话术里、在 `app/ui/chat_window/utils.py` 里 —— **同样没有注册** |
| `take_screenshot` | 真实函数名是 **`take_screenshot_tool`**（`app/engine/tools/_system.py`） |
| `mcp_aw_buckets` / `mcp_aw_query` | 不存在。真实的是 `mcp_aw_aw_status` / `mcp_aw_aw_get_current_window` / `mcp_aw_aw_get_recent_activities` / `mcp_aw_aw_get_browser_tabs`（命名规则 `mcp_{server}_{tool}`，`app/mcp/protocol.py:350`） |

**桌宠会对主人念叨一个它自己都没有的工具名** —— `send_email` 恰恰出现在状态差时的拒绝话术里。

**而 `ToolRegistry.register()` 是按名字覆盖**（`app/engine/tools/_core.py:112` `self._tools[tool.name] = tool`）——
**任何名字对不上都不会报错，只会静默生效**，所以这类漂移永远不会被运行时暴露。

另外 `app/engine/tools/_system.py` 里有两个**模块级** `register()`：
第一个在 `:104-377`（把 7 个工具**定义在函数内部**），第二个在 `:733-741`。
Python 里后定义覆盖先定义 → **第一个 register 永远不会被调用**。
实测：两处注册的是**同一批 7 个同名工具**
（`open_website` / `open_app` / `list_installed_apps_tool` / `system_info` / 
`take_screenshot_tool` / `clipboard_copy` / `send_notification`），
实际生效的是第二个、它注册的是 `:569` 起的**模块级副本**。
**所以工具一个不少（61 不变），但同一组工具有两份定义，必须手工保持同步** —— 
2026-10-28 的「中文应用优先」改动就不得不同时改两处（`app/engine/tools/_system.py:291` 与 `:614`）。

### 4.1.2 最严重的一处：同一个工具在两个后端有两套 schema，其中一套是坏的

`app/brain/langchain_agent.py:81-91`：

```python
def _internal_tool_to_langchain(tool: Tool) -> StructuredTool:
    return StructuredTool.from_function(
        func=tool.fn,
        name=tool.name,
        description=tool.description,
        parse_docstring=False,
    )
```

**`tool.parameters` 没有被传进去。** LangChain 于是从 Python 签名重新推导 schema，
把 A 手写的整份 JSON Schema 丢掉。

在 A 自己的解释器里实测（49 个不依赖活体后端即可注册的工具，共 69 个手写属性）：

| 项 | 手写 schema | LangChain 推导出来的 |
|---|---|---|
| 属性 `description` | 64 个属性带描述 | **69 / 69 全部丢失**（只剩 `title` + `type`） |
| enum / minimum / maximum | 11 处约束 | **11 / 11 全部丢失** |
| 属性集合不同的工具 | — | **4 个**：`run_script`、`generate_image`、`generate_video`、`generate_audio` |

**最致命的是 `run_script`：**

```
手写   : {script_path: string, timeout_s: integer(min1,max300), args: string "传给脚本的参数，shlex 切分"}
推导后 : {script_path: string, timeout_s: integer(default 60), v__args: array}
```

`args` 与 pydantic `BaseTool.args` 撞名 → 被改名成 `v__args`，类型还从 string 变成 array。
**两种调用方式都失败（已在 A 自己的解释器里实测）：**

```
invoke({'script_path':'x.py','args':'--flag'})
  -> TypeError: run_script() got an unexpected keyword argument 'v__args'
invoke({'script_path':'x.py','v__args':'--flag'})
  -> ValidationError: Input should be a valid list (input_value='--flag')
```

**→ `run_script` 在 `standard` 后端上完全不可用。**
而 `config.yaml:60` 当前正是 `backend: standard`（行内注释 `#lightweight:ReAct standard:Langchain`），
**所以这一条对当前配置是活的，不是潜伏 bug。**

`generate_image` / `generate_video` / `generate_audio` 则多出一个模型从没被告知过的 `kwargs` 属性。

**→ 同一个工具、两副脸：`lightweight` 后端（`app/core/config.py:174` 的默认值）看到完整版，
`standard` 后端看到的是"没有参数说明、没有取值范围、个别工具签名还变了"的残缺版。**

### 4.2 工具清单在 system prompt 里出现两遍

- 一次是 **JSON schema**（`app/engine/tools/_core.py:163-175` `to_openai_for_level`）
- 一次是**平铺的名字列表**（`app/brain/brain_controller.py:270-276`）：

```
【可用工具列表】
你**真正可调用**的工具名（共 68 个）：open_app、open_website、add_reminder、…
**何时调哪个工具**的规则见上面的 SKILL 文档。
```

平铺 68 个名字纯属浪费上下文，且在 system prompt 里会**持续提醒模型"你有工具"**。
而 `_STYLE_RULE`（`agent.py:39-44`）又在禁止它提到工具名 —— **一对互相抵消的信号**。

### 4.3 B 的做法：不靠提示词，靠两层硬闸

`plugins/qq-agent-presets/qq-tool-restrict.mjs`（336 行，唯一权威实现；
`dsh/agent-presets/*/qq-tool-restrict.mjs` 只是 7 行 re-export 壳）：

**第 1 层 · 注册期隐藏**（`plugins/qq-agent-presets/qq-tool-restrict.mjs:272-326`）：

```js
ctx.tools.restrict({ deny: [name] })   // 逐个 deny，工具从 schema 里消失
```

名单 `LOCAL_EXECUTION_TOOLS`（`:120-168`）逐个列出了要藏的本地执行/文件/进程/派生-agent/
插件装载/终端/会话检索类工具。注释里记着一条血泪教训（`:34-40`）：

> 教训：本名单初版把 `pwsh_persistent`/`bash_persistent`/`cordis`/`goal_write`/`goal_read` 当成工具名，
> 这五个**根本不存在** —— 而 `tools.restrict()` 对未知名字会抛错、被下面的 try/catch 逐个吞掉，
> 于是坏名字**静默失效**。

**第 2 层 · 执行期白名单**（`:328-335`）：

```js
ctx.tools.guard((exec) => {
  if (SAFE_EXACT.has(name)) return;                          // ask_user_question, todo_write
  if (SAFE_PREFIXES.some(p => name.startsWith(p))) return;   // mcp__snowluma__ / mcp__snowluma-host__ / web-search-safe__ / local-media__
  return '工具 "' + name + '" 不在 QQ 桥接白名单内，已拒绝（仅允许 QQ MCP 工具与无害模型侧工具）';
});
```

**并且有防漂移机制**：`scripts/scan-dsh-tool-names.mjs` 拿真实安装的 DSH 工具名与名单**双向求差**，
`scripts/test-preset-local-tools.mjs` 专门防"拼错假名"；
restrict 失败还会落盘诊断（`state/tool-restrict.log`，有界保留 20 行，`:247-270`），
因为 `DSH 的 stderr 不落日志文件…所以"为什么失败"在现网完全不可观测`（`:238-246`）。

**A 侧对照：**

| 闸门 | A 有吗 | 位置 |
|---|---|---|
| 注册期按意图隐藏 schema | ⚠️ **有，但是软约束** | `agent.py:451-467` `_select_tools` —— 只**不下发**，工具仍注册，模型仍可凭猜的名字调用 |
| 意图级硬拒 | ✅ 有 | `agent.py:401-409` `_INTENT_BLOCKED_TOOLS`（白名单是软约束，这里强制拒绝） |
| 等级解锁闸 | ✅ 有（A 独有，且是产品特性） | `_core.py:210-218` + `app/core/companion_config.yaml:150-197` |
| 危险工具人工确认 | ✅ 有（A 独有） | `agent.py:19-20` `DANGEROUS_TOOLS` + `_confirm_or_skip` |
| **执行期白名单** | ❌ **无** | — |
| **工具名单防漂移对账** | ❌ **无** | — |

**A 的意图裁剪本身是好设计**（`agent.py:361-398`，7 个意图 × 4~15 个工具），
但它只解决"选得准"，不解决"越权调用"；B 的 `guard` 解决的是后者。

### 4.4 工具结果契约：A 是字符串 + 中文关键词猜测

A 的 `ToolRegistry.execute` **永远返回字符串**（`app/engine/tools/_core.py:205-239`），
错误也是字符串：`f"错误：{e}"`。于是 `agent.py` 只能**用中文关键词猜**工具成败：

- `_TOOL_FAIL_MARKERS`（`agent.py:190-197`）—— **26 个条目（去重后 25 个）**：
  `"失败","错误","无法","未安装","未找到","未能","超时","未执行","未响应","未生效"…`
- `_SUCCESS_CLAIM_RE`（`agent.py:235-239`）—— 用正则去判断**模型是不是在吹牛**：
  `(?:打开|启动|设置好|记好|…)[^，。！？、\n]{0,20}?(?:啦|了|好了|成功|哦|喔|咯)`

**这是一个用字符串匹配模拟结构化协议的脆弱设计**，而且已经踩过坑 ——
`app/engine/tools/_memory.py:82-85` 的注释写着：
「删了 0 条原来返回"已删除 0 条…"，不含任何失败词 → 被判 ok → 模型会接着对主人说"已经帮你删掉了"，其实什么都没删。」

B 侧：DSH 返回**结构化 tool result**，
并且用 `dsh-compaction-tool-result-pruner` 做**头尾裁剪**（threshold 50000 / head 45000 / tail 2000，
`agent.cordis.yml:232-237`），而不是 A 那种一刀切 `result[:4000]`（`agent.py:584`）。

### 4.5 工具面对比

| | A | B |
|---|---|---|
| 暴露给模型 | **68** = 61 内置 + 7 MCP（`fs` 3 个 + `aw` 4 个，由 `brain_controller.py:347` 的 `bridge_to()` 单独桥接，**不与内置工具同批注册**） | 54 个 MCP（snowluma 43 + snowluma-host 1 + web-search-safe 3 + local-media 7）+ 2 个无害模型侧工具（`ask_user_question`/`todo_write`）= **56**。⚠️ 这 4 个 MCP 命名空间是 **DSH 宿主**装的，**不在 qq-bridge 仓库里** |
| 分组方式 | **平铺 68 个名字，无分组** | **按命名空间分 4 组**，且每个 MCP 家族在提示词里**有专属使用策略段落** |
| 危险工具 | 藏在 68 个里面，靠等级锁 + 弹窗 | **注册期就从 schema 消失** |
| 工具使用策略 | 在 `SKILL.md`（12.5KB，与 68 个工具不同步） | 在仿真提示词里，**每个工具家族一段**，含频率指导 |

### 4.5.1 一个重要反证：**问题不是"工具太多"**

B 的 QQ 会话同样向模型暴露 **约 56 个 MCP 工具**，用户体验却是"顺"。
**→ 工具数量不是主因。** A 真正缺的是三样"组织"：

| | A | B |
|---|---|---|
| 运行时分组 | **只有文档分组**（`docs/tools-reference.md:10-21` 的 9 类，给**人**看的）。`Tool` 数据类（`app/engine/tools/_core.py:86-91`）只有 name/description/parameters/fn —— **没有 category、没有 namespace、没有分组注册表** | **三层真分组**：MCP 命名空间前缀（`mcp__snowluma__` / `mcp__local-media__` / `mcp__web-search-safe__`）+ 提示词里 20 段「按场景点名相关工具」的协议（`agent.cordis.yml:30-90`）+ guard 按前缀分桶（`qq-tool-restrict.mjs:219-227`） |
| 可见性依据 | **等级**（Lv1=35 → Lv10=61，`companion_config.yaml:124-197`）—— 工具面随**养成进度**变，不随**当前请求**变 | 注册期按名单**从 schema 里删掉**（`ctx.tools.restrict({deny})`）+ 执行期按白名单**拒绝**（`ctx.tools.guard()`），两者都是 DSH 运行时原语（已在 `E:\dsh-runtime\` 核实） |
| 危险工具的邻居 | `lock_screen` / `kill_process` / `run_script` 与 `get_current_time` **同处一个无差别列表** | 整类危险工具在模型看到 schema 之前就被移除了 |

**A 的风险模型是"逐工具"（`app/engine/tools/_risk.py:50-119`，63 条目 / 5 档 / 21 个高风险，
未列出者默认 MEDIUM —— 这是正确的 fail-safe 默认），B 的风险模型是"按面"（deny-by-default 的白名单）。
两者不是同一层东西，不能互相替代。**

B 仿真提示词里工具策略的粒度值得注意（`agent.cordis.yml:47-52`）：

> **主动发**：对方只是随口一句、没提问、没要求你做什么的时候，恰恰最该来一张图
> **频率**：私聊 1~2 轮一张很正常；群聊普通闲聊约 2~3 轮一张
> **斗图**：一般 2~3 轮就收，对方停你也停，别无限刷屏

**这是"策略"，不是"说明书"。A 的 SKILL.md 是说明书。**

### 4.6 成本与 token 治理（用户明确要求的第四维度）

#### 4.6.1 A：有用量数字，但没有治理

**A 确实抓到了 provider 返回的真实用量**：
- `app/brain/llm_client.py:781-786`、`:1079-1084` 抓 `prompt_tokens`/`completion_tokens`/`total_tokens`
- `app/ui/chat_window/__init__.py:2927-2938` 累加到 `_accum_tokens` 并在状态栏显示
  （`:3044` 渲染成 `tokens: N (p↓+c↑)`；计数器在 `:834` 初始化时归零，但**不按轮次重置**）

**但缺的不是显示，而是这四样：**

| 缺什么 | 证据 |
|---|---|
| **持久化账本** | `app/brain/llm_client.py:748` 建了 `usage_acc`，`app/ui/chat_window/__init__.py:2938` 明确写着 `return  # 不再走 trace.record, usage 不入 trace`。`data/traces.db` 的 `events` 表**没有 usage 类型**（实测：text 557 / finish 231 / tool 85 / meta 74 / error 17 / reflection 6 / plan 5） |
| **定价 / 金额换算** | 全仓 `app/` 无任何价目表、无货币计算、无货币符号（唯一 `cost` 是 `chat_window/__init__.py:1650` 的一个表示**字符长度**的局部变量） |
| **缓存桶拆分** | `llm_client.py:778-787` 只读 `prompt/completion/total`。**provider 是返回缓存命中的**（见 4.6.3），但 A **把它扔了** |
| **输入侧预算 / 花费告警** | `config.yaml:7` 的 `max_tokens: 1024` **只管输出**。输入侧只有 `_MAX_HISTORY=50` / `_CTX_CHAR_BUDGET=12000` 的**丢最旧**（`chat_window/__init__.py:254-256`、`:1632-1660`），**没有摘要、没有压缩、没有花费上限** |

**两个具体的量不到的缺陷：**

1. **`chat_stream` 算了 usage 却从不 yield。** `llm_client.py:748` 的 `usage_acc` 在
   `chat_stream`（`:709-855`）里只被写入、不被产出；唯一 yield usage 的地方在 `:1165-1168`
   （另一条路径）。**结果：没有工具的普通聊天路径（`app/ui/chat_window/worker.py:180`）
   与全部后台主动搭话（`app/brain/proactive.py:92`）上报的 token 都是 0。**
2. **A 已经被限流了，而且不知道自己被限流。** `data/traces.db` 的 17 条 error 事件里，
   **7 条是 HTTP 429**（token plan 用量上限 / tpm·rpm 超限）——
   **而 A 对自己的 token 消耗零可见性。**

#### 4.6.2 B：完整的账本 + 定价 + 控制台

- `src/token-ledger.js`（958 行）+ `src/model-prices.js`（290 行）
- `state/token-usage.jsonl`（实测 **851 行**：795 条 `k:"turn"` + 56 条 `k:"base"`）
- `docs/guides/TOKEN_USAGE_CONSOLE.md` + `plugins/qq-mode-console/` + `public/console.html`
  （4 张卡片 + 24h~30d 走势 + 按会话下钻 + 按轮下钻 + 40 条流水 + 当前价目表 + reset）
- 5 个 admin-only 端点 `/api/tokens/{summary,series,turns,recent,reset}`（`src/bridge.js:7212-7213`）

设计注释（`token-ledger.js:1-25`）本身就说明了成熟度：

> 1. **权威总量来自 DSH 的 tokenUsage 投影，不来自桥接自己累加。**
> 2. **逐轮明细靠事件折叠，且必须幂等。** …同一 (session, turn, step, retry) 的采样必须后到覆盖先到，而不是累加。
> 3. **不能把唯一副本放在会被重连清空的内存里。**

还有两个 A 完全没有的机制：
- **峰谷分时计价**：`model-prices.js:85-100` 价目表带 `peakMultiplier: 2` 与中国节假日日历（`:171-179`）
- **角色卡注入上限**：`role.maxInjectChars` = 6000 字符（`src/bridge.js:312-317`，钳制在 1000~20000）。
  **A 的对应物是 12,523 字节的 SKILL.md，它每轮都原样重发，却没有任何上限。**

#### 4.6.3 一个会改变结论的发现：B 不是省 token，是命中缓存

我直接用 B 自己的账本算了一遍（不引用任何中间结论）：

```
ledger rows: 851   (base 56 + turn 795)
cacheHitRate : 0.9302
cacheHit     : 263,466,602
cacheMiss    :  19,778,360
output       :     173,921
total tokens : 283,418,883
distinct (session,turn): 72
avg tokens per turn   : 3,936,373
models: MiniMax-M3.1-Flash 725 / deepseek-flash 59 / 未知 11
```

**B 每轮平均烧 394 万 token，比 A 大得多。**
B 之所以敢这么烧，是因为 **93.02% 是缓存命中**（`cacheHit` 单价 ¥0.02/M vs `cacheMiss` ¥1/M，差 50 倍），
且账本显示 **98.1% 的花费落在 2 倍价的峰时窗口**。

**→ 所以 B 体验好 和 B 省 token 是两件事。B 省的是缓存没命中的钱，不是 token 本身。**

**这对 A 有两个直接后果：**

1. **A 用的是同一家 provider**（`config.yaml:4-5`：`base_url: https://api.minimaxi.com/v1`、
   `model: MiniMax-M3`；B 的账本里 `MiniMax-M3.1-Flash` 占 725/795 样本），
   **也就是说 A 同样有资格拿到 93% 的缓存命中率 —— 但 A 的解析器把缓存字段扔了**，
   所以它**既看不到自己省了多少，也看不到自己漏了多少**。
2. **本报告 `0~`6 给 A 的提示词改造建议，必须先验证缓存命中率再上。**
   把 system prompt 从 3.1k 字扩到 B 那种 19.7k 字，
   **在缓存命中的前提下几乎免费，在缓存不命中的前提下是灾难**。

##### 🔴 实测续：Stage 0 在真 key 上找到了那个"灾难"的开关

补测（2026-10-28，MiniMax-M3 真 key，用**应用真实抓到的请求体**重放）：

| 请求体 | prompt | cached | 命中率 |
|---|---|---|---|
| 时间戳**不变**（第 2 次） | 8301 | 8300 | **99.99%** |
| 时间戳**换掉** | 8301 | 128 | **1.54%** |
| **摘掉时间戳**（第 2 次） | 8281 | 8192 | **98.9%** |

**机制**：MiniMax 按 **8192 token 一块**缓存；而 A 的 system message 末尾挂着
`[当前时间:2026-10-28 22:53:04 星期三]`（`hallucination_guard.py:212`），
它落在 system message 的**第 ~700 个 token** 处 ——
**每次请求都让第一个 8192 块整体失效**，只剩 provider 自带的 128 token 底座。

**A 每轮 8301 个 prompt token，只有 128 个走缓存价。** 摘掉时间戳后约 109 个按全价计 → **约 75 倍**。

**→ 修正本节第 2 条的措辞**：不是"如果易变内容排到稳定前缀之前，缓存会整段失效"，
而是**"易变内容只要待在 system message 里（哪怕在最后），整个第一块就会失效"**。
所以扩提示词的正确姿势是：**扩稳定的（角色卡 / 行为层 / 工具 schema），
把易变的（时间 / 实时状态）挪出 system message**。
反过来这也说明：`1.3 `1.4 要加的行为层是**稳定内容**，加了几乎免费；
而 `2.1` 要加的"每轮记忆规则"如果每轮都重写整段，反而是在浪费缓存。

已登记为路线图 **P0-11**。

#### 4.6.4 上下文预算：B 有压缩，A 只有丢弃

| | B | A |
|---|---|---|
| 历史超限怎么办 | `dsh-compaction-basic` **摘要并遗忘**（`thresholdRatio 0.8 / retainRatio 0.16`）+ `dsh-command-compact` 手动 `/compact` | **丢最旧**（`_MAX_HISTORY=50` / `_CTX_CHAR_BUDGET=12000`，`chat_window/__init__.py:1632-1660`），**从不摘要** |
| 工具结果超限 | `dsh-compaction-tool-result-pruner`：50000 阈值 / 45000 头 / 2000 尾（`agent.cordis.yml:232-237`） | 一刀切 `result[:4000]`（`app/brain/agent.py:584`） |
| 单轮内增长 | 受控 | 每次工具调用 +4000 字符，最多 6 轮（`MAX_TURNS=6`），**每轮全量重发** |

**A 每轮把整段历史 + 全部工具结果原样重发，这是 A 单轮成本随对话变长的主因。**

## 5. 机制归属表（移植时最关键的一张表）

| 机制 | 归属 | 移植难度 | 备注 |
|---|---|---|---|
| 对照式对话示例 + AI 味黑名单 | 🟢 B 自有（`roles/小鲸鱼.md:88-163`） | **低（纯文案，性价比最高）** | A 已有一半素材，补黑名单与对照格式即可 |
| 仿真提示词 / 人格提示词两层拆分 | 🟢 B 自有（`src/preset-prompt.js` 157 行） | 中 | B 有保存时的**硬性不变量校验**（`PRESET_BLOCKING_SUBSTRINGS`，删掉就拒绝保存） |
| 角色卡热更新（`roles/*.md`） | 🟢 B 自有（`src/role-card.js` 108 行） | 低 | A 的 `characters/xiaoxi.yaml` 已经是角色层 |
| 分条 / 节奏 / 主体性等社交行为规则 | 🟢 B 自有（`agent.cordis.yml:112-147`） | 低 | 桌宠 1:1 场景下大部分不适用，需裁剪，否则又变成"规程堆" |
| 软质量反馈（提示而非删除） | 🟢 B 自有（`bridge.js:10940-10983`） | 低 | 直接替代 A 的硬删除 |
| 记忆按 query 语义召回 top-K | 🟢 B 自有（`src/memory-sidecar.js`） | 中 | A 有更好的向量引擎，**只需换召回时机 + 改渲染文案** |
| 记忆 4 分类 + 记忆卫生规则 | 🟢 B 自有（`agent.cordis.yml:38-41, 155-164` + `bridge.js:11763`） | 低 | A 缺"生命周期类型"这一维 |
| `supersede` 事实改写 | 🟢 B 自有（`memory-sidecar.js:194-205`） | 低 | A 有 `detect_conflicts`，补一个改写语义即可 |
| 工具**执行期白名单 guard** | 🟢 B 自有（`qq-tool-restrict.mjs:328-335`） | 中 | A 的 `ToolRegistry.execute` 是天然的插入点 |
| 工具名**双向对账脚本** | 🟢 B 自有（`scripts/scan-dsh-tool-names.mjs`） | 低 | 直接解决 68/56/31 三数字不一致 |
| 消息节奏 / 分条发送 | 🟢 B 自有（`bridge.js:840-855`） | 中 | 需要改 `app/ui/chat_window` 的渲染 |
| 黑话/口头禅学习回路 | 🟢 B 自有（`src/slang-learner.js` 382 行） | 中 | 1:1 场景价值下降，可降级为"用户口头禅" |
| 时间感 / 消息时效 | 🟢 B 自有（`bridge.js:11613-11639`） | 低 | A 的 `context_provider()` 就是现成挂点 |
| token 账本 + 定价 + 控制台 | 🟢 B 自有（958 + 290 行 + console） | 中 | A 已有 per-request usage，缺聚合层 |
| **长期记忆引擎本身（Chroma+BM25+reranker）** | 🟢 **已经是 Python** (`E:/SnowLuma/memory_sidecar/memory_store.py`，38.6KB) | **低（近乎原样复用）** | 与 Node / DSH 零耦合；A 可直接起一个 :8788 sidecar |
| **上下文压缩 / 工具结果头尾裁剪** | 🔵 **DSH 运行时** | **高（需重写）** | B 靠 `dsh-compaction-*` 插件，A 必须自己实现 |
| **结构化 tool result 协议** | 🔵 **DSH 运行时** | **高（需重写）** | A 现在是字符串 + 中文关键词猜测 |
| **agent loop / force_tool_use / 工具并行** | 🔵 **DSH 运行时** | 高 | A 已有自研 ReAct 循环，**不换** |
| **MCP host / 命名空间工具面** | 🔵 **DSH 运行时** | 高 | A 已有自己的 `app/mcp/` |
| persona prefix/suffix 段落注册与 fail-loud | 🔵 **DSH 运行时**（`dsh-persona` 插件） | 低 | A 直接拼字符串，本来就是手搓的 |
| 唤醒/潜水/等待状态机 | 🔵 DSH 会话模型驱动 | 高 | A 是 1:1 桌宠，**大部分不适用** |

---

## 6. 改造路线图

> 原则：不换运行时、不加第三方依赖（`requirements.txt` 冻结 + PyInstaller 打包成本高）、
> 每条改造都给出**可自动化的验收标准**、**先做能被验证的**。

### P0 · 止血（约 3~4 人日）—— 目标：先让"对话像人"发生

**P0-1 修 `brain_controller.py:267` 的字面量换行（真 Bug，0.25 人日）**
- 改：`return "\\n\\n".join(parts)` → 真正的换行
- 验收：`chat_context()` 返回值按 `\\n\\n` 切分后得到段（SKILL / 记忆 / 工具列表 —— **P0-11 之后时间与实时状态已移出 system message，改走 user 消息**，见 `4.6.3`）
- 工作量：0.25 人日

**P0-2 统一工具数字，并加对账脚本（0.5 人日）**
- 新增：`scripts/check_tool_docs.py`（从 `ToolRegistry` 导出真实工具名）
- 改：`.agents/skills/desktop-pet-tool-usage/SKILL.md` 的 Quick reference 标题；
  改 `app/brain/brain_controller.py:53` 的 `_FALLBACK_SKILL` 标题
- 搬：B 的 `scripts/scan-dsh-tool-names.mjs`（双向对账，防拼写假名/漂移）
- 验收：脚本输出差异数 = 0；**68 / 56 / 31 / 52 四个数字合并为 1 个**
- 工作量：0.5 人日

**P0-3 拆回复清洗管线，让"人话"活下来（1~1.5 人日）**
- 改：`app/brain/llm_client.py:158-165` `_META_SELF_PLAN_RE` —— 收窄，
  只删**真的在描述工具调用**的句子，不删"我来帮你查一下"这种过渡语
- 改：`llm_client.py:107-115` `_META_NARRATION_RE` —— 只删**确实是规划/思考**的括号，
  保留"（小声）""（笑）"这类语气括号
- 改：`llm_client.py:289-291` —— emoji 不再全局剥离（保留 `<think>`/CoT/规则复读的剥离）
- 改：`llm_client.py:310-316` —— 中文占比 < 15% 不再返回空串，改为**重试一次**，
  仍失败再走兜底；`agent.py:72-73` 的兜底文案改成**多条轮换**
- 搬：B 的软反馈（`findCjkSpaceWarning` / `findSplitBoundaryWarning`）
- 验收：20 条固定输入的回复中，出现"我来/让我/我看看"等过渡语的条数 **> 0**（当前为 0）；
  被整条丢弃的回复数 = 0
- 工作量：1~1.5 人日

**P0-4 删掉"回复 1~3 句"硬上限，换成长度策略（0.5 人日）**
- 改：`app/brain/llm_client.py:86` —— 替换为
  「默认短句（1~10 字）；需要解释时最多 2~3 句；不要每次都一样长」
- 验收：20 条固定输入的回复长度**方差 > 0**（当前恒定 1~3 句）
- 工作量：0.5 人日

**P0-5 状态门控收窄到高风险工具，去掉双重注入（0.5 人日）**
- 改：`app/brain/hallucination_guard.py:174-207` ——
  ① 删除 `final_override` 那份重复（保留一份即可）
  ② 把"拒绝一切请求"改为"**只拒绝 `_RISKY_TOOLS`**；普通请求照常回应，可以撒娇/说累"
  ③ 删掉"主人追问『我没事, 帮帮我』**继续拒绝**"这条
- 验收：把 mood 置为 LOW 后，普通问题（"今天天气怎么样"）**仍能得到自然回答**
- 工作量：0.5 人日

**P0-6 人设层补 AI 味黑名单 + 对照示例（0.5~1 人日）**
- 改：`characters/xiaoxi.yaml` —— 补 ① 20 条 AI 味黑名单（照 `roles/小鲸鱼.md:88-102` 的清单，
  但换成小汐的口吻）② 6~8 组"用户说 / 不要这样 / 这样更好"对照示例
- 验收：黑名单里的词在 20 条评测回复中**出现 0 次**
- 工作量：0.5~1 人日

**P0-7 修两个会直接说谎的缺陷（0.5 人日）**
- 改：`app/brain/memory.py:577-580` —— 去重命中时**返回库中真实存在的那条**，
  而不是返回一个从未入库的新 id；`app/engine/tools/_memory.py:40` 相应改为"已经记过了"
- 改：`app/brain/langchain_agent.py:550-551` —— agent 复用时**每轮重算记忆块**，
  或在记忆版本变化时重建 agent
- 验收：写入一条已存在的记忆，工具返回的 id 能在库里查到；
  会话中途写入一条新事实，下一轮 system prompt 里能看见它
- 工作量：0.5 人日

**P0-8 对齐 legacy / live 记忆库（0.25 人日）**
- 现象：`data/memory.db`（4 行，legacy）与 `data/memory_小汐.db`（2 行，运行中）
  同时活着，且生日事实互相矛盾；`config.yaml:62` 写的是 `data/memory.json`
- 改：确认 `config.yaml` 的 `memory_file` 与派生规则一致；写一个一次性迁移脚本
  把 legacy 库合并进 live 库并**按 id 去重**
- 验收：合并后库里不再有两条互相矛盾的生日事实；删除 legacy 库
- 工作量：0.25 人日

**P0-9 修 standard 后端的 schema 丢失（0.5 人日，优先级最高）**
- 现象：`app/brain/langchain_agent.py:81-91` 的 `StructuredTool.from_function` **没有传 `tool.parameters`**，
  LangChain 从签名重推 schema → 手写的属性描述 / enum / min-max 约束全部丢失（按 61 个工具的**全量**口径：84 个属性里 75 条带描述、2 处 enum、11 处 min/max）；
  `run_script` 的 `args` 撞上 pydantic `BaseTool.args` 被改名 `v__args`，**两种调用方式都失败**
- 而 `config.yaml:60` 当前是 `backend: standard` → **这条是活的**
- 改：把 `tool.parameters` 转成 pydantic 模型传给 `StructuredTool.from_function`（或改用
  `StructuredTool(..., args_schema=...)`）；`run_script` 的 `args` 改名或显式声明 schema
- 加回归测试：断言**模型拿到的 JSON Schema == 手写的 dict**，逐个工具比对
- 验收：`run_script` 在 standard 后端能被成功调用；属性描述与约束在两个后端完全一致
- 工作量：0.5 人日

### P1 · 记忆改造（约 3~4 人日）

**P1-0 【最高性价比】直接复用 B 的 Python 记忆引擎（0.5~1 人日）**
- 现状：B 的长期事实引擎 `E:\SnowLuma\memory_sidecar\memory_store.py` **已经是 Python**，
  提供 `serve --port 8788`（Chroma + BM25 + Qwen3-Reranker）、`upsert` / `search` /
  `supersede` / `delete` / `delete-by-key`，且**与 Node / DSH 零耦合**
- 改：`app/brain/memory.py` 的 `MemoryStore` 增加一个 `SidecarBackend`（对标 `VectorBackend` 接口），
  A 直接起一个 :8788 的 sidecar 子进程（参考 B 的 `memory-sidecar.js:74-141` 的
  spawn/health/ensureStarted + 冷启动放宽超时重试）
- 验收：A 写入 20 条事实后，问一个只与其中 2 条相关的问题，
  system prompt 里只出现那 2 条（当前是固定 20 条全量）
- 备注：若不想引入外部服务，**最低成本版本**是先用 A 已有的 TfidfBackend 做 RRF 融合 +
  按 query 检索，也能拿到 `3.1` 描述的 80% 收益
- 工作量：0.5~1 人日（外部服务版）/ 0.25 人日（本地 RRF 版）
**P1-1 固定 top-20 倾倒 → 按本轮输入召回 top-6（1.5 人日）**
- 改：`app/brain/memory.py:871-890` —— `system_block()` 增加 `query` 参数；
  `_select_for_injection(query)` 走 `memory.search(keyword=query)` 的语义打分
  （`memory.py:629`）而非 `_score(i, 0.0)`；`max_inject` 20 → 6
- 改：`app/brain/brain_controller.py:249-267` 与 `app/brain/langchain_agent.py:605`
  —— 把**本轮用户输入**传进去（当前 `chat_context()` 拿不到当轮输入，需改签名）
  ⚠️ **P0-11 已改过这里**：时间与实时状态已从 system message 移出，改走 user 消息（见 `4.6.3` 的缓存实测）；`chat_context()` 现在只返回 4 段稳定内容（SKILL / 记忆 / 工具列表），易变部分由 `chat_context_dynamic()` 提供。
- 搬：B 的 `formatRecalledMemories`（`memory-sidecar.js:282-289`）渲染文案，
  含"可能相关也可能不准；用不上就别硬扯"
- 验收：system prompt 里记忆条数 ≤6；**同一会话连续 10 轮，记忆块内容随输入变化**
- 工作量：1.5 人日

**P1-2 记忆分类补"生命周期"维度 + 写入规则每轮注入（1.5 人日）**
- 改：`app/engine/tools/_memory.py:11-28` —— 在现有 category 之外引入
  `activeTopic` / `pendingThought` / `memberImpression` 三类
- 改：`app/brain/brain_controller.py:249-267` —— 每轮注入【记忆规则】段
- 改：`app/engine/tools/_memory.py:12-16` 的描述 —— 删掉"关于**主人**的"限定，
  补上"也记你自己的承诺/立场"（B 的 `memory-fact-scope.md` 教训）
- 搬：B 的 `src/bridge.js:11763` `memoryRuleLine` + `agent.cordis.yml:38-41, 155-164`
- 验收：新会话 20 轮后，`data/memory_小汐.db` 里同时存在"主人相关"与"小汐自己相关"两类记录
- 工作量：1.5 人日

**P1-3 事实改写（supersede）语义（1 人日）**
- 改：`app/brain/memory.py` —— 新增 `supersede(old_id, new_content)`，
  复用已有 `detect_conflicts`（`:762`）
- 改：`app/engine/tools/_memory.py` —— `forget_memory` 增加按 id 改写的路径
- 验收：写入矛盾事实后，旧条目标记为过时而不是并存
- 工作量：1 人日

**P2-0 【先做这个，否则后面都是盲改】拆分缓存桶 + 落盘账本（1 人日）**
- 依据：`4.6.3` —— **A 与 B 用同一家 provider**，B 的缓存命中率 93.02%，
  而 A 的解析器（`llm_client.py:778-787`）**只读 prompt/completion/total，把缓存桶扔了**
- 改：`llm_client.py:778-787` —— 把 provider 返回的缓存命中/未命中/写入 token 单独取出来；
  修 `:748` 的 `chat_stream` 让它 yield usage（现在普通聊天与后台主动搭话都上报 0）
- 新增：`app/brain/token_ledger.py` —— JSONL append-only，键 `(session_id, turn, step, retry)`，
  **只存 token 桶、不存金额**（价目表变了不用重写历史）
- 改：`app/ui/chat_window/__init__.py:2938` —— usage 不再被 `return` 掉，进 trace
- 验收：连续 10 轮后能算出 A 自己的**缓存命中率**；普通聊天与主动搭话路径的 token 不再是 0；
  `data/token-usage.jsonl` 能按轮聚合
- 工作量：1 人日

### P2 · 工具面治理与成本可见性（约 4~6 人日）

**P2-1 工具结果结构化 + 头尾裁剪（2 人日）**
- 改：`app/engine/tools/_core.py:205-239` `execute()` → 返回
  `{"ok": bool, "data": str, "error": str}`
- 改：`app/brain/agent.py:190-239` —— `_TOOL_FAIL_MARKERS`（26 个条目 / 25 个去重）与
  `_SUCCESS_CLAIM_RE` 改为读结构化字段；**保留**矛盾检测（A 的独有资产）
- 改：`app/brain/agent.py:584` `result[:4000]` → 头尾保留
  （参考 DSH pruner 的 45000/2000 比例）
- 验收：`_TOOL_FAIL_MARKERS` 关键词表可整体删除且现有测试仍通过
- 工作量：2 人日

**P2-2 执行期白名单（守门）（1.5 人日）**
- 改：`app/engine/tools/_core.py` —— 加 `guard` 钩子（白名单/黑名单），
  在 `execute()` 入口拒绝越权工具
- 保留：等级解锁闸（`companion_config.yaml:150-197`）、危险工具确认（`agent.py:19-20`）
- 搬：B 的 `qq-tool-restrict.mjs:328-335`（`SAFE_PREFIXES` / `SAFE_EXACT` 两级白名单 + 拒绝文案）
- 验收：新增测试 —— 不在白名单的工具即使被模型点名调用也返回"已拒绝"
- 工作量：1.5 人日

**P2-3 token 账本 + 定价 + 最小可视化（1.5~2 人日）**
- 新增：`app/brain/token_ledger.py`（JSONL 落盘，沿用 `data/` 目录惯例）
- 改：`app/brain/llm_client.py:781-784` / `:1079-1082` —— 把已有 usage 接到账本
- 搬：B 的 `src/token-ledger.js`（幂等采样键 `sessionId|turn|step|retry`）+ `model-prices.js`
- 验收：`data/token-usage.jsonl` 能按会话/回合聚合出 token 与金额；
  聊天窗口能看到本会话累计
- 工作量：1.5~2 人日

**P2-4 上下文预算（2 人日）**
- 改：`app/brain/agent.py:16` `MAX_TURNS=6` + `app/ui/chat_window/__init__.py:1632-1654`
  `_trim_history()` —— 引入"按记忆分类摘要"，而不是按条数/字符预算丢弃
- 搬：🔵 DSH 的 `dsh-compaction-basic` 思路（**需重写，不能照抄**）
- 验收：连续 30 轮对话后，早期关键事实仍能被召回
- 工作量：2 人日

### 不建议做的

| 项 | 原因 |
|---|---|
| 换成 DSH 运行时 | 与已确认方向冲突；A 的 PyInstaller 打包、桌宠 UI、Live2D、GPT-SoVITS 语音链都是自有资产 |
| 引入 Chroma / reranker 等重依赖 | A 已有 SentenceTransformer 后端，够用；PyInstaller 打包成本高 |
| 照抄 B 的 13,713 字行为提示词 | B 是**群聊多用户**场景，A 是 1:1 桌宠；需裁剪到 1/3 长度，否则又变成"规程堆" |
| 删掉 A 的反幻觉三层 | A 在这块强于 B，删了是净损失 |
| 删掉 A 的等级解锁闸 | 这是 A 独有的产品特性（陪伴养成），B 没有对应物 |

---

## 7. 建议补的"自然度"回归测试

B 的做法是"每个机制一个测试脚本"（`scripts/test-*.mjs` 共 60+ 个），
A 已经有 `tests/` **86 个 `test_*.py` 测试文件**（实测）—— **A 缺的不是测试能力，是行为规格**。

建议补一个 `tests/test_naturalness.py`，用固定的 20 组输入 + 断言（黑名单词、句长方差、
是否出现"已帮你…"、是否强行引用不相关记忆、过渡语是否被删）：

1. "今天好累啊" → 不得出现黑名单词；不得出现"主人，…已完成"句式；≤2 句
2. "你在干嘛" → 不得调用任何工具
3. "帮我打开记事本" → 必须调 `open_app`，且回复不得复述工具名
4. "打开记事本"（应用不存在）→ 必须如实说失败，不得报喜
5. "你还记得我喜欢喝什么吗" → 必须触发记忆召回，答案与召回内容一致
6. "随便聊聊" → 不得倾倒 20 条记忆
7. "你是什么AI" → 不得出现"作为一个语言模型"
8. "我今天心情不太好" → 不得出现"唔……刚刚没想好怎么说"这类固定兜底
9. …（余下 12 条按 A 的实际场景补）

---

## 8. 附录 · 证据索引

**A（desktop-pet）**

| 结论 | 证据 |
|---|---|
| 清洗管线删 emoji | `app/brain/llm_client.py:90-103, 289-291` |
| 清洗管线删括号内中文 | `llm_client.py:107-115, 293-294` |
| 清洗管线删"我来/我应该"整句 | `llm_client.py:158-165, 298-304` |
| 清洗管线删含"简短回答/不要用 emoji"的整句 | `llm_client.py:174`（`_META_PROMPT_ECHO_RE`） |
| 中文占比 < 15% → 整条回复为空 | `llm_client.py:310-316` |
| 固定兜底文案 | `app/brain/agent.py:72-73` |
| "回复 1~3 句"硬上限 | `llm_client.py:86` |
| 状态差 → 拒绝一切请求（注入两次） | `hallucination_guard.py:174-188, 200-207` |
| 状态量化 | `app/brain/companion_policy.py:202-207` |
| 动态上下文拼成一行（真 Bug） | `app/brain/brain_controller.py:267` |
| system prompt 装配顺序 | `app/brain/skills.py:26-27` |
| 动态上下文 3 段 | `app/brain/brain_controller.py:249-267` |
| 助手口吻铁律 | `app/brain/agent.py:39-63` |
| "完整、有条理的中文总结"与反例冲突 | `app/brain/agent.py:352-354` vs `characters/xiaoxi.yaml:41` |
| 通用助手兜底 | `app/brain/langchain_agent.py:597` |
| 工具结果永远是字符串 | `app/engine/tools/_core.py:205-239` |
| 30 个中文失败关键词 | `app/brain/agent.py:190-197` |
| 成功断言靠正则 | `app/brain/agent.py:235-239` |
| 工具结果硬截断 4000 | `app/brain/agent.py:584` |
| MAX_TURNS=6 | `app/brain/agent.py:16` |
| 意图裁剪 4~15 个工具 | `app/brain/agent.py:451-467, 361-398` |
| 等级解锁闸 | `app/core/companion_config.yaml:150-197`、`app/engine/tools/_core.py:210-218` |
| 危险工具确认 | `app/brain/agent.py:19-20, 489-505` |
| 逐请求 usage 抓取 | `app/brain/llm_client.py:781-784, 1079-1082` |
| 用量仅在聊天窗口显示 | `app/ui/chat_window/__init__.py:2925-2930` |
| 记忆固定 top-20，semantic 恒为 0 | `app/brain/memory.py:882-890`（`:301` max_inject=20） |
| 记忆块鼓励引用 | `app/brain/memory.py:877-880` |
| 记忆只能靠 remember_fact 显式写入 | `app/engine/tools/_memory.py:32`；全仓 `memory.add(` 仅 2 处 |
| 记忆描述只提"主人" | `app/engine/tools/_memory.py:12-16` |
| 意图白名单里 recall_memory 只在 remember_fact 意图 | `app/brain/agent.py:361-398` |
| 真实工具数 68 | `docs/tools-reference.md:1, :539` |
| SKILL.md 写 56 个工具 | `.agents/skills/desktop-pet-tool-usage/SKILL.md` |
| 兜底 skill 写 31 个工具 | `app/brain/brain_controller.py:53` |
| 人设素材有正/反例、状态变体 | `characters/xiaoxi.yaml:31-56` |
| 主动说话限死 40 字 | `app/brain/proactive.py:490-505` |
| 86 个 test_*.py 测试文件 | `tests/` 实测（目录共 92 个条目，含 2 个子目录） |
| 实际暴露工具数 68 = 61 内置 + 7 MCP | `app/engine/tools/__init__.py:43-65`（61，AST 计数）+ `mcp_servers.yaml`（fs 3 / aw 4）+ `config.yaml:75-78` |
| 文档工具名与注册表不符（≥6 个） | `app/engine/tools/_risk.py:111`、`app/brain/agent.py:19-20`、`app/mcp/protocol.py:350`、`app/engine/tools/_system.py` |
| `_system.py` 有两个模块级 `register()`，工具定义有两份 | `app/engine/tools/_system.py:104-377`（被覆盖、不执行）与 `:733-741`（活）；模块级副本在 `:569` 起。实测两处注册同名 7 工具 → 工具不缺，但要手工同步 |
| standard 后端丢弃手写 schema | `app/brain/langchain_agent.py:81-91`（实测全量 61 工具：84 属性中 75 条描述、2 处 enum、11 处 min/max 全丢，4 个工具属性集不同） |
| `run_script` 在 standard 后端不可调用 | 同上；实测 TypeError + ValidationError（`args` → `v__args`） |
| 当前启用的是 standard 后端 | `config.yaml:60`（`backend: standard`）、`app/core/config.py:174`（默认值 lightweight） |
| A 的工具运行时无分组 | `app/engine/tools/_core.py:86-91`（`Tool` 只有 4 个字段） |
| `chat_stream` 算了 usage 从不 yield | `app/brain/llm_client.py:748`（写入）vs `:1165-1168`（唯一的 yield 点） |
| usage 被显式排除出 trace | `app/ui/chat_window/__init__.py:2938` |
| traces.db 无 usage 事件类型 | 实测 kinds：text 557 / finish 231 / tool 85 / meta 74 / error 17 / reflection 6 / plan 5 |
| A 已被 token plan 限流 | `data/traces.db` 17 条 error 里 **7 条 HTTP 429**（另 1 条含 "429" 的是 HTTP 401） |
| A 的解析器丢弃缓存桶 | `app/brain/llm_client.py:778-787`（只读 prompt/completion/total） |
| B 的账本实测：缓存命中率 93.02%、每轮均 394 万 token | `state/token-usage.jsonl`（851 行 = 795 turn + 56 base），独立计算 |
| B 的峰谷分时计价与角色卡注入上限 | `src/model-prices.js:85-100, 171-179`、`src/bridge.js:312-317` |
| B 的上下文预算是摘要并遗忘 | `dsh/agent-presets/qq-chat-v2/agent.cordis.yml:219-237` |
| A 与 B 同属 MiniMax 系 | `config.yaml:4-5` vs B 账本 `MiniMax-M3.1-Flash` 725/795 样本 |
| 每行 access_count 恒为 0（注入路径从不 touch） | `app/brain/memory.py:666, 708`（touch 只在 search() 内）、`app/engine/tools/_memory.py:62, 65` |
| legacy/live 双库并存且事实矛盾 | `data/memory.db`(4 行) vs `data/memory_小汐.db`(2 行)、`app/brain/memory.py:301-310`、`config.yaml:62` |
| 去重命中返回幻影 item 却报「已记住」 | `app/brain/memory.py:577-580`、`app/engine/tools/_memory.py:40` |
| MemoryCurator 从未实例化（整理链死代码） | 全仓 grep 无 `MemoryCurator(` 调用点；内部调用在 `app/brain/memory.py:946, 948` |
| 「存在证明」只是 grep 源码文本 | `scripts/verify_features.py:105` |
| LangChain 路径记忆只烘一次 | `app/brain/langchain_agent.py:550-551, 603-607, 619` |
| 聊天历史无 LIMIT 加载 + 裁剪不落盘 | `app/engine/chat_store.py:161-165, 216-218` |
| 实际上下文上限 | `app/ui/chat_window/__init__.py:254, 256`（50 条 / 12000 字） |
| system prompt 每轮重建 | `app/ui/chat_window/__init__.py:2378` |
| 写入触发只有一条正则 | `app/brain/llm_client.py:1238-1243` |
| 主动说话限死 40 字 | `app/brain/proactive.py:494` |
| 提示词装配顺序 | `app/brain/skills.py:26-28`（docstring 原文） |

**B（qq-bridge）**

| 结论 | 证据 |
|---|---|
| 17 个提示词段落 | `src/bridge.js:11816` |
| 仿真提示词 13,713 字 | `dsh/agent-presets/qq-chat-v2/agent.cordis.yml:20-190` |
| 角色卡 5,946 字 | `roles/小鲸鱼.md` |
| 两层提示词的职责边界 | `src/preset-prompt.js:1-9` |
| 提示词保存时的硬性不变量校验 | `src/preset-prompt.js:25-64` |
| AI 味黑名单 20 条 | `roles/小鲸鱼.md:88-102` |
| 对照示例 11 组 | `roles/小鲸鱼.md:104-163` |
| "先像群友、像真人，再谈人设" | `roles/小鲸鱼.md:12` |
| 反 AI 味段落 | `agent.cordis.yml:100-106` |
| 分条与节奏 | `agent.cordis.yml:141-147` |
| 保持主体性 | `agent.cordis.yml:112-117` |
| 工具家族使用策略（含频率） | `agent.cordis.yml:47-52` |
| 记忆 4 分类 | `agent.cordis.yml:38-41, 155-164` |
| 每轮注入的记忆规则 | `src/bridge.js:11763` |
| 记忆召回 top-6 + 免责文案 | `src/memory-sidecar.js:282-289, 17-27` |
| sidecar 容错与冷启动重试 | `src/memory-sidecar.js:11, 144-179`（`:163-171`） |
| supersede 事实改写 | `src/memory-sidecar.js:194-205` |
| fact 归错类的复盘 | `docs/memory-fact-scope.md` |
| 注册期隐藏工具 | `plugins/qq-agent-presets/qq-tool-restrict.mjs:272-326, 120-168` |
| 执行期白名单 guard | `plugins/qq-agent-presets/qq-tool-restrict.mjs:328-335` |
| 假名静默失效的教训 | `plugins/qq-agent-presets/qq-tool-restrict.mjs:34-40` |
| restrict 失败落盘诊断 | `plugins/qq-agent-presets/qq-tool-restrict.mjs:238-270` |
| 工具名双向对账 | `scripts/scan-dsh-tool-names.mjs`、`scripts/test-preset-local-tools.mjs` |
| compaction 三插件 + 头尾裁剪参数 | `agent.cordis.yml:219-237` |
| token 账本设计 | `src/token-ledger.js:1-25`（958 行）、`src/model-prices.js`（290 行） |
| token 用量控制台 | `docs/guides/TOKEN_USAGE_CONSOLE.md`、`plugins/qq-mode-console/`、`public/console.html` |
| 消息节奏/延时配置 | `src/bridge.js:840-855` |
| 软质量反馈 | `src/bridge.js:10940-10983` |
| 时间感 | `src/bridge.js:11613-11639` |
| 60+ 机制级测试 | `scripts/test-*.mjs` |

---

## 9. 未验证 / 待补

1. **实际 token 消耗对比** —— 本文只做了静态结构对比，没有跑真实对话测 token/轮。
   需要：两端各 20 轮同题对话，记录 system prompt 长度与 usage。
2. **A 的三套大脑路径差异** —— `llm_client.py`（直连）/ `langchain_agent.py`（LangGraph）/
   `agent.py`（ReAct）三条路径的 system prompt 装配并不完全相同。
   已确认：`config.yaml:60` 为 `backend: standard`，按行内注释 `#lightweight:ReAct standard:Langchain`
   对应 Langchain 路径，因此 `3.7(1)` 的"记忆只烘一次"大概率命中当前配置 ——
   **但未在运行时确认实际走的是哪条路径。**
3. **B 的 sidecar 实际召回质量** —— 只读了客户端与源码，未跑起 Chroma + reranker 实测 top-6 命中率。
4. **B 当前没有启用角色卡** —— `state/current-role.json` 为 `{"role": null}`，
   此刻 B 的语气完全由 13,713 字的行为提示词承担；角色卡的实际增益需在启用后再测。
5. **A 的 `proactive.py` 定时主动说话** 与 B 的"对话内主动"差异，本文只做定性对比，未做行为评估。
6. **`SKILL.md` 到底缺哪 12 个工具** —— 需跑 `scripts/check_tool_docs.py`（P0-2）才能列出确切清单。
7. **DSH 运行时自身的记忆/上下文投影能力**
8. **DSH 插件的运行时行为** —— `dsh-persona` / `dsh-compaction-*` / `dsh-tool-result-pruner`
   只核实了**包存在**（`E:\dsh-runtime\node_modules\@deepseek-ai\`）与**预设里的声明**
   （`agent.cordis.yml:219-237`），没有读它们的源码。
   另：`ctx.tools.restrict()` / `ctx.tools.guard()` 的"注册期隐藏 / 执行期拒绝"语义
   依据的是 DSH 的开发文档（`dsh-agent-preset/skills/cordis-plugin-development/references/practices.md:10,17`）。
9. **B 的 54 个 MCP 工具计数** —— 4 个 MCP 命名空间由 **DSH 宿主**安装。
   静态计数在 qq-bridge 自己的 4 个 MCP server 里是 56 个（其中 snowluma 有 2 个受
   `registerLegacyReadTools` 门控，`mcp-snowluma-safe.js:453-458`），与运行时的 43 个 snowluma 差 2 个，
   **未启动 v2 桥接实测**。
 —— 本文只从 qq-bridge 侧判断 DSH 提供了什么，
   没有读 `E:\dsh-runtime\` 的源码。已确认的是：**B 不用 DSH 的 `fact_register`**
   （全仓 grep 零命中），B 的长期记忆 100% 是自己的 sidecar。

### 9.2 移植状态复核（2026-10-31，§5 那张表的逐行核对）

§5 的「机制归属表」就是移植清单。**现在逐行核一遍**（✅ 已做 / 🔄 在做 / ⚪ 1:1 场景不适用 / ❌ 没做）：

| 机制 | 现在的状态 |
|---|---|
| 对照式对话示例 + AI 味黑名单 | ✅ §characters/style-guide.md§（36 条黑名单 + 12 组对照示例）+ 逐条 golden 门禁 |
| 仿真提示词 / 人格两层拆分 | ✅ §app/brain/prompt_layer.py§（persona / style_guide / tool_handbook / dynamic 四段） |
| 角色卡热更新 | ✅ §characters/*.yaml§ + 运行时切换（§config.switch_character§，**不动 config.yaml**） |
| 分条 / 节奏 / 主体性 | ✅ 逐句投递（§reply_pacing§，间隔实测 146~389ms，**不超模型出句速度**） |
| 软质量反馈（提示而非删除） | ✅ 删掉了 20+ 条改写模型散文的正则（「让提示词负责，代码不猜」） |
| 记忆按 query 语义召回 top-K | ✅ 独立 sidecar（JSON 卡真值 + Chroma + BM25 + 精排）+ **按 kind 分档闸门**（近似陷阱避开率 1.000，recall 1.000） |
| 记忆分类 + 记忆卫生 | ✅ 5 类（thought/topic/event/fact/impression）+ §supersede§ 改写 + 分档阈值；**再加分区**（身份 / 价值 / 程序性三块常量块 + 情感权重 arousal/valence + 检索路由：时间词→事件、事实词→事实、how-to→程序性，**只调顺序不硬过滤**） |
| 工具**执行期白名单** | ✅ §tool_allowlist§（61 精确名 + 3 前缀桶）：存量放行 / 增量拒绝 |
| 工具名**双向对账脚本** | ✅ §scripts/check_tool_docs.py§（8 处来源，阻断级差异） |
| 消息节奏 / 分条发送 | ✅（同上） |
| 主动搭话的触发来源 | ✅ **多源轮换**（2026-11-05）：记忆 / 最近对话 / AW / **自主想法** / 自己的状态 / 规律 / 时间感 —— 每轮挑 1~2 个有素材的理由，最近两轮用过的降权（原来只有 AW，桌宠只会顺着 AW 说话） |
| 黑话 / 口头禅学习回路 | ✅ **同款移植**（§app/engine/tools/_slang.py§：candidate/confirmed/rejected 三态 + 容量上限 + 排序 + 坏文件隔离 + **只有 confirmed 注入提示词**（常量块）） |
| 时间感 / 消息时效 | ✅ §context_provider§（时段提示 / 规律 hint） |
| token 账本 + 定价 + 控制台 | ✅ §model_prices.py§（读时算钱）+ 台账 + 控制台成本页（人民币口径、逐行传 model） |
| 长期记忆引擎 | ✅（**纯 Python** sidecar，与 qq-bridge 同源思路但独立实现）；**分区**（身份 / 价值 / 程序性三块常量 + 情感权重 + 检索路由）+ **控制台可见**（§/api/memory/partitions§，记忆 Tab 的「网络」视图里单独一块面板） |
| 上下文压缩 / 工具结果裁剪 | ✅ §compaction.py§（0.8 / 0.16 / 最近 8 条原文）+ §fit_tool_payload§（50000/45000/2000） |
| 结构化 tool result 协议 | ✅ 两后端都对齐（standard 走 §wrap_tool_fn§ + §ToolPhaseBudget§ middleware：单工具 60s / 整轮 120s） |
| agent loop / 工具并行 | ✅ 自研 ReAct + LangChain 双后端（不换 DSH） |
| MCP host / 命名空间工具面 | ✅ §app/mcp/§（aw / fs / voice 三台，10 个工具） |
| 唤醒 / 潜水 / 等待状态机 | ⚪ 1:1 桌宠**不适用**（qq-bridge 的群聊模型） |

**qq-bridge 有、我们没有、且值得考虑的**（2026-11-01 更新）：
1. ~~**拍一拍**（poke）~~ → ✅ **已做**：双击模型 = 一次拍一拍（表情 + 台词 + 计数 + 长期记忆卡）；单击热区只是触摸反应。
2. ~~**GIF 表情包库**~~ → ✅ **已做**：不下载素材，直接用现成的 §assets/sprites/Emotion_*§ 素材做目录（8 种情绪 × 4 = **32 张**，每张带动画帧），桌宠能把它当**一条消息**发进聊天（§app/engine/stickers.py§ + §send_sticker§）。
3. **ComfyUI 生视频** —— 我们的 §generate_video§ 还是 stub（**明确不做**：要另起一个 GPU 服务，跟「桌宠不该那么重」冲突）。
4. ~~**黑话 / 口头禅学习**~~ → ✅ **已做**（见上表）。

### 9.1 已在本轮复核并从"待补"中移除的项

| 原待验证项 | 复核结果 |
|---|---|
| `_MAX_HISTORY` / `_CTX_CHAR_BUDGET` 数值 | **已确认**：`chat_window/__init__.py:254, 256` → 50 条 / 12000 字 |
| temperature / max_tokens / timeout 是否是问题 | **已确认不是问题**：`app/core/config.py:20-23` → stream=True, 0.8, 1024, 60 |
| A 的 system prompt 是否每轮重建 | **已确认**：`chat_window/__init__.py:2378` 每轮 `persona + context_provider()` |
| A 的记忆整理引擎是否真的没跑 | **已确认没跑**：`MemoryCurator(` 全仓无实例化点（`forget_if_expired`/`merge_similar` 在 `:946,948` 内部被调用，但整条链没人启动） |
| `detect_conflicts` / `add_shared` 是否被调用 | **已确认未被调用**：仅 `tests/` 与 `scripts/verify_features.py:105` 的源码 grep 提及 |
| B 的 sidecar 引擎是不是 Node 专属 | **已确认不是**：`E:\SnowLuma\memory_sidecar\memory_store.py` 是纯 Python，与 Node/DSH 零耦合 |
