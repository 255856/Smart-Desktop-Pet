# 桌宠项目性能审计报告

审计日期：2026-10-20
代码基线：`E:\study\desktop-pet`（app/ 下 168 个 Python 文件，42,297 行）
方法：静态调用链追踪 + 实机测量（导入剖析、cProfile 式基准、SQLite/HTTP 计时、FastAPI 并发实测）
工具：4 路并行子系统深挖（GUI/动画、脑/LLM、工具/IO、语音/Web/游戏）+ 主会话直接测量，所有结论均已回源码复核

---

## 总览

项目整体架构质量不错：工具层已用 `asyncio.to_thread` 隔离、Live2D/精灵渲染有节流与隐藏即暂停、
Werewolf 多 agent 已用 `asyncio.gather` + 信号量限流、配置读取有缓存。**问题不在渲染层，
而在「LLM 流式输出 → 聊天窗 → 桌宠气泡」这条主链路，以及若干「定时器/后台任务直接跑在 Qt 主线程」的点。**

核心症结有三个：

1. **Qt 主线程被阻塞**：桌面宠物的动画、气泡、托盘全部挂在同一个 GUI 线程上，任何同步 I/O 都会让整只宠物僵死。
2. **O(n²) 放大**：流式输出的每个 token 都要重扫一遍「到目前为止的全部文本」，回复越长越卡。
3. **状态无限增长**：LangGraph checkpoint 每轮重复写入整段历史，实测已膨胀到 275 MB。

严重度分布：CRITICAL 5 · HIGH 9 · MEDIUM 11 · LOW 7（合计 32 项，下文列出 24 项主要问题）

---

## CRITICAL

### C1. LangGraph checkpoint 每轮重复写入整段历史，DB 已膨胀到 275 MB

**位置**：`app/brain/langchain_agent.py:672-682` + `app/ui/chat_window/__init__.py:2483-2499`

```python
history = self._to_lc_history(messages[:-1] if messages else [])   # langchain_agent.py:672
lc_msgs: list = [*history]                                        # :678
input_payload: dict[str, Any] = {"messages": lc_msgs}             # :682
config = {"configurable": {"thread_id": self.thread_id}}           # :683
```

`_build_clean_messages_for_llm` 每轮返回**完整的本地 `self.history`**（上限 50 条 / 12,000 字符），
LangGraph 的 `add_messages` reducer 是**追加**语义，因此这些消息被再次追加进持久化的 checkpoint。
本地消息没有 `id`，去重逻辑不生效。

**实测证据**（`data/langchain_checkpoints.db`）：

| 指标 | 实测值 |
|---|---|
| DB 体积 | **275.8 MB** |
| checkpoint 条数 | 510（总 273.4 MB） |
| 单条 > 100 KB 的 checkpoint | **413 条** |
| 最大单条 checkpoint | 1.26 MB，**4,393 条消息**（human 2,396 / ai 1,935） |
| 最大 checkpoint 内消息总字符 | 192,239 |

**历史重复次数**（同一句话被写入 checkpoint 的次数）：

```
   267x  '/image 橘猫'
   213x  '帮我锁屏'
   200x  '帮我打开 Notepad'
   196x  '写一首诗'
   177x  '主人~锁屏是危险操作，要确认一下：真的要锁屏吗？...'
   136x  '/image 猫'
```

**代价**：每轮模型调用重发整段状态（`default` 线程 ≈ 19 万字符 ≈ 远超上下文窗口），
每个 LangGraph 步都要把完整状态重新序列化并写回 SQLite（1.26 MB/次）。
`_trim_history`（`chat_window/__init__.py:1595`）**确实被调用**，但它只裁剪本地列表，裁不动 checkpoint。

**修复**：`LangChainAgent.run` 只传本轮新消息，让 checkpointer 持有历史：

```python
input_payload = {"messages": [HumanMessage(content=last_user)]}
# 仅当 thread 尚无 checkpoint 时才回退到 messages[:-1]
```

并用 `RemoveMessage(id=REMOVE_ALL_MESSAGES)` 定期裁剪 checkpoint，或直接调用现成的
`_clear_thread_checkpoint()`（`langchain_agent.py:977`）。

---

### C2. 象棋 AI 的 alpha-beta 搜索跑在 Qt 主线程，每步棋冻结 1–14 秒

**位置**：`app/ui/xiangqi_window.py:930`（`QTimer.timeout` 槽 → GUI 线程）+ `app/games/xiangqi.py:598`

```python
self._ai_timer.timeout.connect(self._ai_move)   # xiangqi_window.py:714
mv = self.ai.choose_move(self.game, self.rng)   # xiangqi_window.py:930
```

深度表 `app/games/xiangqi.py:45`：`DIFF_DEPTH = {"easy": 1, "normal": 3, "hard": 4}`

**实测**（中局局面）：

| 深度 | 节点数 | 耗时 |
|---|---|---|
| 2 | 126 | 200 ms |
| 3（normal） | 1,781 | **1,045 ms** |
| 4（hard） | 9,767 | **13,972 ms** |

每个节点都调 `game.legal_moves(side)` → `cchess.create_moves()` + `is_legal()` 全量过滤，
**无置换表**、**无节点/时间预算**。`xiangqi_window.py:834` 的 `delay`（250/420/620 ms）是前置延迟，不覆盖计算。

**修复**：搜索移到 `QThreadPool`/`QThread`，结果用 signal 回主线程；
同时给 `XiangqiAI._search` 加节点数/时间预算，超限返回当前最优解，让 hard 难度优雅降级。

---

### C3. `_wait_all_sentence_workers` 在 GUI 线程忙等，最长 120 秒

**位置**：`app/ui/chat_window/__init__.py:3110-3127`，由 `_on_done` 调用（`:3172`）

```python
def _wait_all_sentence_workers(self, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    while self._sentence_workers and time.monotonic() < deadline:
        QApplication = _safe_qapp()
        if QApplication is not None:
            QApplication.processEvents()
        time.sleep(0.05)
```

`_on_done` 是 `self._worker.done.connect(self._on_done)`（`:2461`）的槽，在 GUI 线程执行。
代码注释自述 GPT-SoVITS 合成需「3-5 秒」，硬超时却是 **120 秒**。

**代价**：每次回复结束，桌宠动画/气泡/托盘/输入框全部冻结最长 120 秒；
且 `processEvents()` 会重入事件循环，导致 `_on_chunk` / `_on_typing_tick` 在 `_on_done` 执行途中插入。

**修复**：不要阻塞。文字先入 history/chat_view，`_on_sentence_ready` 就绪时再补对应句子
（`_pending_speak` / `_tts_seq_drain` 队列本就是为此设计）。
若必须保留「文字等语音」，把自旋换成 `QTimer`/`QEventLoop` 且硬上限压到 ~2 s。

---

### C4. 主动发言的 ActivityWatch 抓取在 GUI 线程做同步 HTTP，最长 24 秒

**位置**：`app/brain/proactive.py:217` → `:175-178` → `app/integrations/activitywatch.py:37-83`

```python
# proactive.py:217  (QTimer.timeout 槽 → 主线程)
aw_context = self._build_aw_context()
# proactive.py:175-178
current = get_current_window_one_liner(server_url=server_url)
recent = get_recent_activities_summary(minutes=lookback, limit=n_max, server_url=server_url)
```

每个函数内部是 3 次串行阻塞 `urllib.request.urlopen`，超时依次为 **2 s / 4 s / 6 s**：

```python
# activitywatch.py:51   is_available  → timeout=2.0
# activitywatch.py:82   _query_events → timeout=6.0
```

**代价**：2 × (2+4+6) = **最长 24 秒 GUI 冻结**，每 5–10 分钟一次（`config.yaml` 设 proactive 5–10 分钟）。
LLM 调用本身已正确放进 `_ChatOnceWorker` QThread，唯独 AW 抓取漏了出来。

**修复**：把 `_build_aw_context()` 移进 `_ChatOnceWorker.run()`，作为构造参数传进去。

---

### C5. GPT-SoVITS 自愈竞态：并发起 N 个 api_v2 进程

**位置**：`app/voice/gptsovits_tts.py:84-95` + `app/main.py:404`

```python
except (httpx.ConnectError, httpx.ReadError, OSError) as e:
    if attempt == 0:
        start_tts_api_subprocess(...)   # 每个失败的句子线程都调一次
        await asyncio.sleep(8)
        continue
```

`chat_window/__init__.py:3070` **每个句子起一个 QThread**。api_v2 挂掉时，N 个线程在同一毫秒窗口内
全部撞上 `ConnectError`，各自独立调用 `start_tts_api_subprocess`。
`app/main.py:404` 的 `_port_listening` 守卫是 TOCTOU：新起的服务要 ~30 s 才加载完模型，
期间端口一直是关的，于是 **N 个 api_v2.py 进程同时启动**，每个都加载数 GB 的模型，
且以 `DETACHED_PROCESS` 启动（`app/main.py:415`），父进程无法回收。

这还是**常态路径**：`app/main.py:551` 只在启动时引擎就是 gptsovits 才拉服务，
运行时从设置面板切到 gptsovits（`ui_controller.py:1298`）走的 `_build_tts` 根本不启动服务，
只能依赖这条竞态自愈。

**修复**：给自愈加进程级 `threading.Lock` + `_tts_api_starting` 标志，只允许一个线程拉起服务，
其余线程改为等端口；同时限制并发合成数（见 M9）。

---

## HIGH

### H1. 每个 token 都新建 SQLite 连接并 commit，且在 GUI 线程

**位置**：`app/ui/chat_window/__init__.py:3003-3007` → `app/brain/trace.py:136-152`

```python
# chat_window/__init__.py:3003
self.trace.record(self._trace_run_id, "text", {"delta": tok, "accumulated": self._streaming_raw[:500]})
# trace.py:145
with self._lock, self._conn() as c:     # _conn() 每次 sqlite3.connect() + commit()
    c.execute("INSERT INTO events ...")
```

`trace.py:64-76` 的 `_conn` 是「每次操作一个独立连接，退出时关闭」。

**实测**（本机）：**6.50 ms/call**

```
  40 tok/s  → 260 ms/s 的 GUI 线程停顿
  200 token 回复 → 合计 1,300 ms GUI 线程占用
```

`TraceRecorder` 在 `ui_controller.py:1063` 永远被创建，所以这条路径**一直在线**。

**修复**：复用 `TraceRecorder` 上的一条长连接（`check_same_thread=False` + 锁），
或把 per-token 的 `"text"` 事件**聚合**成一条，在 `_on_done` 写一次（`kind="tool"` 保持逐条）。

---

### H2. 流式输出每个 token 重扫全部累积文本（O(n²)，GUI 线程）

**位置**：`app/ui/chat_window/__init__.py:2994-2997`

```python
self._streaming_raw = (self._streaming_raw or "") + tok
sanitized = sanitize_text(self._streaming_raw).strip()
```

`sanitize_text` 默认 `is_final=True`（`llm_client.py:268`），跑的是完整管线：
`<think>` 剥离、情绪标签、emoji 清理、工具废话子句、编号规则项、`_strip_internal_checklist`，
外加一个 `while prev_text != text:` 的 fixpoint 循环 + CJK 占比段落扫描。

**实测**（本机，累积缓冲区逐 token 增长）：

| 回复长度 | token 数 | 当前实现（GUI 线程） | 只清一次 | 倍数 |
|---|---|---|---|---|
| 522 字符 | 54 | 13.1 ms | 0.43 ms | 31x |
| 2,088 字符 | 216 | **199.4 ms** | 2.04 ms | 98x |
| 4,176 字符 | 432 | **785.3 ms** | 3.53 ms | 223x |

**连带放大**：`_on_chunk` 里 `self.streaming_chunk.emit(sanitized)` 发的是**累积全文**（信号注释即
「已累积的完整文本」），下游 `ui_controller.py:785` → `pet_window.show_streaming_bubble()`
（`pet_window.py:391`）又对全文跑一次 `re.sub(r'\s+', '', text)` 再 `setText()` + `_place_bubble()`。
即每 token 触发 **3 次全文正则 + 2 次 Qt 重新布局**。

**修复**：流式期间只清洗增量（`sanitize_text(chunk, is_final=False)`），
最终 `is_final=True` 的那一次留到 `_on_done`；
`streaming_chunk` 改发增量或做节流（每 50 ms 一次），气泡只重排尾部。

---

### H3. Dashboard `/api/companion` 是 async def 却做同步全量解析，阻塞事件循环

**位置**：`app/web/dashboard.py:823-887`（服务端）+ `:612`/`:726`（客户端 5 秒轮询）

```python
@app.get("/api/companion")
async def companion(decision_limit: int = 30, outcome_limit: int = 30):   # :824  ← async def
    ...
    for line in log_path.read_text(encoding="utf-8").splitlines():          # :875
        rec = json.loads(line)                                             # :878
```

`companion_log.txt` 是**只增不减**的 JSON-line 文件，现已 **2.87 MB / 12,872 行**。
每 5 秒轮询一次，全文读 + 12,872 次 `json.loads` + 两次全量排序，最后才切片取前 200/200 条。

**实测（FastAPI ASGI 实测）**：

```
single /api/companion : 69.5 ms  (status 200, 81586 bytes)
2 concurrent requests  : 146.1 ms   ratio=2.10x      ← 完全串行 = 事件循环被堵
/api/stats            : 13.7 ms
/api/runs?limit=100   : 21.5 ms
```

`ratio=2.10x` 是铁证：若走线程池应约 1.0x。**整个 Dashboard 服务每 5 秒失联 69.5 ms，且随文件增长线性恶化。**

**修复**：
1. 端点改成普通 `def`（FastAPI 自动丢线程池），或内部 `await anyio.to_thread.run_sync(...)`；
2. 按 `st_mtime`/`st_size` 缓存解析结果，只增量解析新增尾巴；
3. 或用 seek 只读文件尾部 N KB，并反向扫描代替全量 sort。

---

### H4. TTS 缓存清理只匹配 `*.mp3`，GPT-SoVITS 的 `.wav` 永不清理

**位置**：`app/voice/voice.py:206`、`:213` vs `app/voice/gptsovits_tts.py:16`

```python
# voice.py:206
for f in self.cache_dir.glob("*.mp3"):
# voice.py:213
files = sorted(self.cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
# gptsovits_tts.py:16
cache_ext: str = ".wav"
```

**实测** `assets/tts_cache/`：**733 个 `.wav`，合计 238.61 MB**；`.mp3` 仅 1 个。
`voice.py:197` 设的 `max_total_mb=80` 预算已超 3 倍，`app/main.py:568` 的每小时
`max_age_days=3` 清理对 wav 是**永久空操作**。每句新台词新增一个永不回收的 ~330 KB 文件。

**修复**：`glob(f"*{self.cache_ext}")` 替换字面量 `"*.mp3"`（两处），让每个引擎清自己的文件。

---

### H5. 启动时急切导入 LangChain，冷启动多花 ~1.1 秒

**位置**：`app/ui/chat_window/__init__.py:69` + `app/brain/langchain_agent.py:20-35`

```python
# chat_window/__init__.py:69  ← 模块级
from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
# langchain_agent.py:21/28/33  ← 模块级
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
```

**实测**（本机，warm .pyc）：

```
TOTAL import app.main                        : 2.64 s
langchain_openai 单独                          : 0.84 s
langchain.agents 单独                          : 1.00 s
langgraph.checkpoint.sqlite.aio                : 0.01 s
阻塞这三个后的 app.main                        : 1.55 s   ← 省下 ~1.1 s
```

`config.py:174` 的默认 backend 是 `"lightweight"`（手写 ReAct，不用 LangChain），
但 `chat_window/__init__.py:69` 是模块级 import，**无论 backend 都要付这 1.1 s**。
`ui_controller.py:1064` 反而已经是函数内延迟 import 了。

**修复**：把 `langchain_agent.py` 里那三个重量级 import 改成函数内延迟 import（或模块级 `__getattr__`），
`create_agent`/`ChatOpenAI`/`AsyncSqliteSaver` 只在真正构建 agent 时才需要。

---

### H6. Werewolf 的 `_tool_call` 绕过全局并发信号量，且无超时无退避

**位置**：`app/games/werewolf_agents.py:303-326`（对比 `_llm_chat` `:133-168`）

```python
# werewolf_agents.py:313  ← 不取信号量
async for ev, data in self.client.chat_stream_events(
        messages, tools=tools, force_tool_use=True):
```

导演的意图写在注释里（`werewolf_director.py:166-167`）：
「全局 LLM 并发限流…避免竞选/投票阶段 8 路 NPC 同时请求触发服务端 529 过载」，`LLM_CONCURRENCY = 4`。
但投票/竞选/发言这些**并行阶段全部走 `_tool_call`**——8 个 NPC 同时发流式请求，
正好是限流想避免的 8 路突发。且 `_tool_call` 相比 `_llm_chat` 缺三样保护：
`asyncio.wait_for` 超时、529/429 指数退避、`CancelledError` 透传。

**代价**：一次 529 就让 8 个 agent 同时退化成脚本台词，游戏质量在服务端过载时**恰好崩塌**。
阶段倒计时只有 120 s（`NPC_PHASE_TIMEOUT`）。

**修复**：把 `_llm_chat` 里的「信号量 + wait_for + 退避」抽成共用 helper，`_tool_call` 也走它。

---

### H7. 四个网络工具每次调用新建 `httpx.Client`，连接池完全浪费

**位置**：`app/engine/tools/_webfetch.py:75`、`_search.py:118`、`_weather.py:40` 与 `:113`、`app/voice/minimax_tts.py:109`

```python
with httpx.Client(timeout=8.0) as c:     # _weather.py:40
    r = c.get(url)
```

每次调用付一次全新 TCP + TLS 握手，池被直接丢弃。`get_weather` 一次调用建**两个** client
（地理编码 + 预报）。项目里已有正确范式：`app/brain/llm_client.py:585` 的长生命周期 `httpx.AsyncClient`。

**修复**：各工具模块内建模块级复用 client（配 `httpx.Limits(max_keepalive_connections=...)`），
`_weather.py` 至少让地理编码和预报共用一个。

---

### H8. 已安装应用缓存的 TTL 是死代码，永远不刷新；`open_app` 重复扫描两遍

**位置**：`app/core/installed_apps.py:56-60`（`_is_fresh`）、`:98-120`（`get_all`）、`app/engine/tools/_system.py:428`/`:437`

```python
# installed_apps.py:56  ← 全仓库无人调用
def _is_fresh(self) -> bool:
    return (self._result.apps and (time.time() - self._result.scanned_at) < self._ttl)
# installed_apps.py:102  ← 只判空，不判新鲜度
if not self._result.apps and not self._scanning:
```

grep 确认：`_is_fresh` 只有定义、`force_refresh` 只有定义，**均无调用点**。
应用列表进程内扫描一次后永远返回陈旧数据（`_SCAN_TIMEOUT = 10` 同样是死常量）。

同时 `open_app` 把同一次相似度扫描跑两遍：
`_system.py:428` `registry.resolve(name)`（内部 `find_similar_apps`，`wait=True, timeout=3.0`）
+ `_system.py:437` `_collect_suggestions(name)`（又一次 `find_similar_apps`）。
冷缓存时最多先阻塞 3 s、再阻塞 3 s。

**修复**：`get_all` 里把「判空」换成 `if not self._is_fresh(): self._maybe_start_scan()`；
`open_app` 只调一次 `find_similar_apps`，把结果同时喂给「解析路径」和「候选建议」。

---

### H9. 非流式 fallback 重复发同一次请求，且丢掉 system prompt

**位置**：`app/brain/llm_client.py:800-805`（`chat_stream`）+ `:1078-1085`（`_fetch_non_streaming`）

```python
if not saw_content:
    full = await self._fetch_non_streaming(messages)            # :803  ← 第二次计费请求
# :1080-1082
full_text = await self._fetch_non_streaming(
    [ChatMessage(role=m["role"], content=m.get("content") or "")
     for m in payload["messages"][1:]])                         # ← [1:] 把 system 丢了
```

服务器已经处理过第一遍；fallback 是同一轮的**第二次计费调用**。
更糟：`payload["messages"][1:]` 把 system 消息切掉，于是这次重试**没有人设、没有中文约束、没有工具规则**。
而这正是推理模型只吐 `reasoning_content` 不吐 `content` 时的常态路径。

同一文件 `:692-698` 已经为「`cfg.stream == False`」修过这个问题（「同一个请求发两遍（费钱且慢）」），漏掉了这一处。

**修复**：复用已流式收到的 reasoning；万不得已要重发时保留 `payload["messages"]` 全文，并复用同一个 `self._client`。

---

## MEDIUM

### M1. 异步 LangChain 路径里塞同步 urllib HTTP
`app/brain/langchain_agent.py:601-610`（`_call_aw`，async def）直接调 `get_recent_activities_summary`，
内部是 3 次阻塞 `urlopen`（2+4+6 s）。它跑在共享持久 loop（`langchain_agent.py:82-154`）上，
aw-server 一慢，**该 loop 上所有 agent 操作、工具调用、流式输出一起等**（最长 ~12 s）。
工具层有 `asyncio.to_thread`（`agent.py:554`），这里漏了。
**修复**：`await asyncio.to_thread(...)`，或把 `activitywatch.py` 换成 `httpx.AsyncClient`。

### M2. AgentLoop 单轮最多重复发同一请求 18 次
`app/brain/agent.py:709-728` 的 `force_retry` 无上限重试，`MAX_TURNS = 6`（`agent.py:16`）
× `_TRANSIENT_MAX_ATTEMPTS = 3`（`llm_client.py:21`）= 最多 18 次 HTTP 请求，每次重发完整历史。
第 2..6 次不带新信息（唯一变化是 tool_choice 降级，而该降级按模型名缓存在 `_TOOL_CHOICE_IGNORED`）。
项目自己的注释已记录症状：`chat_window/__init__.py:2373-2375`「实测带历史时可达 5 轮 / 12 秒」。
**修复**：无工具重试上限设为 1，且重试时在 `messages` 里追加明确指令让两次请求真正不同。

### M3. 每轮新建 `httpx.AsyncClient` 且从不关闭
`app/brain/llm_client.py:585` 每次 `LLMClient.__init__` 建一个池，而 `LLMClient` 每轮新建
（`chat_window/__init__.py:2364`）→ 每轮丢一次 TLS 复用。全仓库无任何 `LLMClient.close()` 调用点，
`worker.py:169-172` 只关了 event loop，没关 `client._client`。
standard backend 下这个 client（`chat_window:2364`）**根本用不到**，纯属每轮白分配。
**修复**：池提到模块级按 `(base_url, timeout)` 复用；或每个 ChatWindow 持一个 LLMClient、只换 system_prompt。

### M4. MemoryStore 每次 add 都重写整表 + 全量重建向量索引
`app/brain/memory.py:483-484`：`add()` 调 `_save()`（`DELETE FROM memories` + 全量 re-INSERT）
再 `_refit_backend()`（重新分词 + 重建 N×V 稠密向量）。
**实测**（tfidf 后端）：累积 0.39 s@50 → 2.01 s@200 → **5.37 s@400**，边际成本 7.8→16.8 ms/add（超线性）。
`TfidfBackend.similarity` 是纯 Python O(N·V) 点积：**N=400 时 4.5 ms**。
**修复**：只 INSERT 新行；索引标脏后由 `MemoryCurator` 后台线程重建。

### M5. 设置面板每敲一个键重写 settings.json 7 次
`app/ui/settings_window.py:1557/1563` 把 `textChanged` 连到 `_on_model_config_changed`（`:2096`），
后者调 7 次 `SettingsStore.set()`（`settings_store.py:37-40`），**每次都整文件序列化重写**。
输 40 字符 API key = **280 次整文件写**。滑块 `valueChanged`（`:1605`）同路径，逐像素触发。
**修复**：`SettingsStore` 加 `set_many(dict)` 单次落盘；`_on_model_config_changed` 加 300 ms 去抖。

### M6. Live2D 点击穿透遮罩每 1.5 秒同步 grab 一次
`app/ui/pet_window.py:1074-1088`（`_mask_timer.start(1500)`）、`:1090-1103`（`_grab_opaque`）
```python
img = w.grab().toImage().convertToFormat(QImage.Format_ARGB32)
```
`QWebEngineView.grab()` 会强制同步光栅化页面（要跟 Chromium 渲染进程往返），
再把整块 ARGB32 转 numpy，全程在 GUI 线程。
现有优化 `_mask_sig` 短路（`:1192`）只省了重建 `QRegion`/`setMask`，**没省 grab()**——grab 在签名比较之前就无条件执行了。
**修复**：grab + numpy 挪到 `QThread`；或把 grab 放到「有变化」的廉价探针之后。

### M7. 本地图片每次渲染都重新读盘 + 重新 base64
`app/ui/chat_window/__init__.py:3639-3719`（`_inline_local_images_to_data_url`）
```python
data = p.read_bytes()
b64 = _b64.b64encode(data).decode("ascii")
```
`_msg_html` 被 `_refresh_streaming_message` 调用，而它由 380 ms 的 `_typing_timer` 驱动（`:4023`），
于是流式期间**每秒约 2.6 次**重读重编码（单文件上限 8 MiB）。
`_load_history`（`:1189-1209`）在开窗时把 200 条历史消息再渲染一遍，历史图片全部重编码。
**修复**：按「绝对路径 + mtime」做 data-URL 记忆化缓存。

### M8. 每句台词起一个 QThread + 一个 event loop + 一个 HTTP client，无上限
`app/ui/chat_window/__init__.py:3054-3075` → `app/voice/voice.py:298` → `app/voice/gptsovits_tts.py:75`
每个句子：1 个新 QThread + 1 个新 `asyncio.run()` event loop + 1 个新 `httpx.AsyncClient`（=1 条新 TCP 连接）。
20 句回复 = 20 线程 / 20 loop / 20 条并发连接打**单进程**本地 api_v2。这正是 C5 竞态的放大器。
**修复**：改成单一常驻 worker 消费句子队列，并发度用信号量限到 2–4。

### M9. GUI 线程上的 60 秒 subprocess 与 6–8 秒 socket 等待
- `app/ui/ui_controller.py:1040` `subprocess.run(cmd, capture_output=True, text=True, timeout=60)`（按钮槽）→ GUI 线程冻结最长 60 s；之后 `reload_atlas` 也回 GUI 线程重解码全部 PNG。
- `app/main.py:425-436` `wait_dashboard_ready` 是 `socket.create_connection` + `time.sleep(0.2)` 轮询，被 `ui_controller.py:1139`（6 s）和 `:1157`（8 s）在 GUI 线程调用。

**修复**：都挪进 worker 线程，用 signal 回主线程。

### M10. `read_recent` 文档承诺「只读尾部」，代码却整文件读入内存
`app/brain/_companion_log.py:91-98`
```python
"""...按文件逆序读 (大文件场景下只读尾部)..."""     # :91
with p.open("r", encoding="utf-8") as f:
    lines = f.readlines()                           # :98  ← 整文件
```
文件现 2.87 MB 且只增不减；另外 `_default_log_path()`（`:25-29`）**每次调用都 mkdir**，
写路径（`trace_decision`/`trace_outcome`）每次都 `open("a")` + `mkdir`。
**修复**：用 seek 从文件尾读；或内存里维护 bounded deque；加文件轮转；目录只建一次。

### M11. Dashboard `/api/stats` 全表扫描 + `/api/runs` N+1 查询
`app/brain/trace.py:205-216`（`stats` 把**所有历史 tool 事件**读出来逐条 `json.loads`）、
`:165-168`（`list_runs` 在循环里逐 run 查 events → 1+100 = 101 次查询）。
另外 `app/web/dashboard.py:751-752` **每个请求都新建 `TraceRecorder`**，其 `__init__` 会跑
`_init_schema()` 的 `CREATE TABLE IF NOT EXISTS` DDL（实测 1.44 ms/请求），
每 5 秒在主应用正在用的同一个 DB 上发起写事务。
**修复**：`create_app()` 里建一个模块级 `TraceRecorder` 复用；`events(kind)` 加索引；
用 `WHERE run_id IN (...)` 替掉 N+1。

---

## LOW

| # | 问题 | 位置 | 代价 |
|---|---|---|---|
| L1 | `_key_to_display` O(n²)，每次比较跑一遍 `_normalize_key` 正则 | `app/core/app_registry.py:447-452` | ~70 条 builtin 键 → 最多 4,900 次正则替换/次 |
| L2 | 每个 `.lnk` 新建一次 COM `WScript.Shell` | `app/core/installed_apps.py:218-220` | 实测 248 个快捷方式 × 3.40 ms ≈ **843 ms**，占 1,238 ms 扫描的 68% |
| L3 | 贴纸目录每次展示都 `iterdir()` + `sorted()` | `app/animation/live2d_renderer.py:601-625` | 纯重复磁盘 I/O，值几乎不变 |
| L4 | 开发者模式滑块每次拖动都 fsync 落盘 | `app/ui/ui_controller.py:348` → `app/core/save.py:127-165` | `copy2` + `mkstemp` + `fsync` + `replace`，绕过正常路径的 5 s 去抖 |
| L5 | TTS 播放队列无 `maxsize` | `app/voice/voice.py:189` | 狼人杀 + 聊天同时说话时积压，宠物会补播过期台词 |
| L6 | PowerShell TTS 的 stderr 文件句柄只 flush 不 close | `app/voice/voice.py:448-460` | 每句泄漏一个 fd |
| L7 | 图片弹窗关闭时 `toHtml()` → `setHtml()` 全文档往返 | `app/ui/chat_window/__init__.py:2665-2672` | 含全部 base64 图片的整份文档序列化 + 重解析，在 GUI 线程 |

---

## 已核查且确认无问题（避免重复排查）

| 模式 | 结论 |
|---|---|
| 桌宠渲染帧循环 | `pet_window.paintEvent` 是空实现，精灵由子 QLabel 画；帧计时器用单次 `QTimer` 按帧时长重排，无逐帧重复工作 |
| 隐藏即省电 | `pet_window.py:672-713` 的 hide/show 会暂停渲染器 + 停掉遮罩/视线/贴纸/帧/气泡全部常驻定时器 |
| 精灵图集 | `sprite_atlas.py:100-147` 的 `ensure_loaded` 有 `_loaded` 守卫，PNG 只解码一次；`prescale` 只在启动/重调帧时长时跑 |
| Pixmap 缩放 | `app/ui/pixmap_utils.py` 有 8 条 LRU 缓存，resize 时才清，不在逐帧路径 |
| 危险工具确认弹窗 | `chat_window/__init__.py:2194-2251` 用 `QMetaObject.invokeMethod(..., QueuedConnection)` 正确切回 GUI 线程，只阻塞 worker 线程 —— 这是正确范式 |
| 网络 I/O 线程模型 | `worker.py` 的三个 worker 全部在 `QThread.run()` 里做 HTTP；同步工具走 `asyncio.to_thread`（`agent.py:554`） |
| Werewolf 并行 | 导演层用 `asyncio.gather`（`:228`/`:377`/`:467`/`:535`）+ 全局信号量 —— 唯一缺陷是 `_tool_call` 没遵守信号量（H6） |
| 五子棋 AI | 纯启发式，无 minimax，实测每步 ≤ 12 ms |
| 聊天历史上限 | `_trim_history` 确实被调用（5 处），本地历史被限制在 50 条 / 12,000 字符（唯一绕过是 checkpoint，见 C1） |
| 狼人杀状态列表 | 受 9 人 × `max_days=10` 约束，不会无限增长 |
| 项目文档 / skills 读取 | 按 `(path, mtime)` 缓存（`project_docs.py:54-56`、`skills.py:60-62`），每次只多一次 `stat()` |
| LangChain 自身 HTTP 池 | `langchain_openai` 的 `_get_default_async_httpx_client` 是模块级缓存，且项目始终复用同一持久 loop —— 这一层没问题 |
| 孤儿 QTimer | 只有 `live2d_renderer.py:360`、`motion.py:115/124`、`sprite_renderer.py:359` 三处无 parent，均为长生命周期单例成员，非逐次创建 |
| Dashboard 不拖累桌宠 | Dashboard 是**独立子进程**（`app/main.py:365`），其轮询开销不进入桌宠 Qt 线程 |
| 正则重复编译 | `sanitize_text` 的正则全是模块级编译常量；`filesearch.py:206` 的 `re.compile` 每次调用只编一次 |

---

## 建议修复顺序

**第一批（用户可感知，改动小、收益最大）**

1. **C3** `_wait_all_sentence_workers` 去掉阻塞 —— 一次改动消灭最长 120 秒冻结
2. **H1** TraceRecorder 复用连接 + 聚合计 token 事件 —— 6.5 ms/token 直接归零
3. **H2** 流式只清增量 —— 典型回复省 199 ms、长回复省 785 ms
4. **C4** AW 抓取挪进 worker 线程 —— 消除 24 秒周期性冻结
5. **M9** 两处 GUI 线程阻塞等待 —— 挪进 worker

**第二批（资源与正确性）**

6. **C1** checkpoint 只传本轮新消息 —— 275 MB 会自动回落，并给历史加轮转
7. **H4** TTS 缓存 glob 用 `cache_ext` —— 238 MB 可回收
8. **C5** TTS 自愈加锁 —— 防止进程风暴
9. **C2** 象棋搜索挪线程 + 加时间预算

**第三批（结构优化）**

10. **H5** LangChain 延迟导入 —— 启动快 ~1.1 秒
11. **H3** Dashboard 端点改 `def` + 增量解析缓存 —— 服务不再被堵
12. **H6** `_tool_call` 接入信号量/超时/退避
13. **H7/H8/H9** 连接池复用、缓存 TTL 生效、fallback 不重复计费

**第四批（打磨）**：M1–M5、M10–M11 与 LOW 7 项。

---

## 未验证 / 需运行时 trace 确认

- `langchain_agent.py:340-348` 的 subagent factory 会开**第二个** `AsyncSqliteSaver`，
  连同一个 `data/langchain_checkpoints.db`，可能存在 SQLite 写锁争用。静态无法确认，需运行时 trace。
- `QWebEngineView.grab()` 的真实耗时（M6）依赖真实 Live2D 模型，本轮未量化，仅依据 Qt 语义与调用路径判定。
- `installed_apps` 扫描在具体机器上的耗时随快捷方式数量浮动。
- `app/engine/tools/_system.py:373` 有一个**模块级** `open_app`，与 `register()`（`:92`）注册的内层闭包重复；
  这更像维护性问题（改 H8 时两处都要改），不计入性能项。
- `chat_window/__init__.py` 在 `:292` 与 `:1002` 重复定义了 `_on_developer_mode_changed`（后者覆盖前者），
  `ui_controller.py` 也在 `:142` 与 `:195` 连了两次该信号。槽函数体很小，不是性能问题，但建议顺手清理。

---

## 修复记录（2026-10-20）

按上面四批的顺序逐项落地，**32 项里改了 30 项**，每项都配了回归测试。
基线 `1322 passed, 8 skipped` → 收尾时 `1358 passed, 8 skipped`（95.9 s）。

### 关键实测变化

| 项 | 改前 | 改后 |
|---|---|---|
| H1 TraceRecorder 单次写入 | 6.50 ms | 0.059 ms（常驻连接 + WAL） |
| H2 流式清洗（2088 字回复） | 216.5 ms | 25.6 ms |
| H3 Dashboard `/api/companion` | 69.5 ms | 12.5 ms（事件循环卡顿 69.5 → 0.1 ms） |
| H5 `import app.main` | 3.61 s | 1.0 s（langchain 全部不预加载） |
| C1 checkpoint | 275.8 MB / 510 条 | 覆盖式写入，天然有界 |
| C2 象棋搜索（深度 4） | ~14 s | 3.0 s（时间预算内） |
| H4 TTS 缓存 | 733 个 wav / 238 MB | 可被清理（glob 用 `cache_ext`） |
| L2 已装应用扫描 | 1238 ms | 698 ms（COM 对象按线程复用） |
| M4 记忆 add（200 条） | 7.3 ms/条（且 O(N) 全表重写） | 5.5 ms/条（只写一行） |
| M11 trace `stats` / `list_runs(50)` | 全表扫描 / 101 次查询 | 0.74 ms / 1.54 ms（1 次查询） |

### 没做的 2 项（有意保留）

- **M6 Live2D 点击穿透遮罩**：`QWidget.grab()` / `QWebEngineView.grab()` 必须在 GUI 线程执行，
  审计建议的「挪到 QThread」在 Qt 里不安全；而「廉价探针」需要改 Live2D 的 JS bridge。
  现有实现已有自适应降频（1.5 s → 3 s）和 JS 闲置即停，收益已部分拿到，风险不值得冒。
- **M8 TTS 单 worker 队列**：要把「每句一个 QThread + 一个 event loop + 一个 HTTP client」
  改成常驻 worker + 信号量限流，属于整条 TTS 管线重写，收益（并发连接数）不抵风险。

### 顺带修掉的真 bug

- `settings_window.reset_to_defaults` 里 `self._set_status(f"语音已切换：{voice}")` 引用了未定义的
  `voice` → 点「重置默认值」必抛 NameError。
- L2 的 COM 缓存踩过坑：`WScript.Shell` 绑定创建它的线程，跨线程复用直接 0xC0000005 崩进程
  （后台扫描线程 + 主线程各扫一次就会撞上）。已改成 `threading.local()`。

### 因行为变更而调整的既有断言

M2 把「该调工具却只给文字」的重试上限从「无上限（最多 5 轮）」改成 1 次，
这是审计明确要求的行为变更，因此两条**改动前就存在**的测试里的轮次阈值要跟着对齐：

- `tests/test_minimax_tool_choice.py::test_force_applies_on_every_retry_turn_not_just_turn_zero`
  —— 断言 `len(force_flags) >= 3`（老行为下能连重试 5 轮）改成 `>= 2`。
  阈值的作用是「保证重试真的发生过」（重试没发生时只有 1 轮，断言照样会挂），
  而被守护的行为是「force 贯穿每一轮重试」——`all(force_flags)` 在 [True, True] 上
  依然完整覆盖「首轮 + 重试轮」，测试意图没被削弱。
- `tests/test_minimax_tool_choice.py::test_agent_loop_injects_no_fake_user_turns`
  —— 同理，`len(seen_messages) >= 3` 改成 `>= 2`；被守护的行为
  「AgentLoop 不往 messages 里塞自拼的伪 user 消息」逐条检查完全没动。
