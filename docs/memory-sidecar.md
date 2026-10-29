# 记忆 sidecar（memory_sidecar）

桌宠的**独立记忆进程**：一个常驻 HTTP 服务持有全部记忆，主进程只通过 HTTP 读写。
类比参考实现 qq-bridge 的 `memory_sidecar/memory_store.py`，但记录格式换成了
**JSON 记忆卡**（主人要的），索引只是派生数据。

- **JSON 卡片是真值**：`data/memory_sidecar/cards.json`
- **写卡不等模型**：写 / 删 / 改只落 JSON，两个索引（Chroma / BM25）是派生的，
  由下一次检索或预热结束补上 —— 冷启动时模型要加载十几秒，写卡完全不等它
- **检索也不等模型**：模型那一路有 0.8s 预算，跑不完就先拿词面结果回答；
  刚写完、还没进向量索引的卡用**词面兜底**进候选 —— 「说完就忘」不会再发生
- **相关度闸门**：库里没有答案的问题一条都不给（阈值是量出来的，见 4.1）
- **运行时绝不联网下模型**：只读本地 HF 缓存，缺模型立刻失败并指向
  `scripts/prefetch_models.py`（见第 7 节）
- **两条粗排腿**：Chroma（向量，cosine）+ BM25（关键词，jieba + 字 bigram）
- **一条精排**：cross-encoder，**按设备自动选** —— 有 CUDA 用 `bge-reranker-v2-m3`，
  否则用 `bge-reranker-base`（实测数据见第 7 节）
- 端口默认 **8788**，只监听 127.0.0.1

---

## 1. 怎么跑

```bash
# 建数据目录 + 空卡库（幂等，跑几次都一样）
python -m memory_sidecar init

# 写入 / 更新记忆卡（文件可以是 JSON 数组，也可以是 {"cards": [...]}）
python -m memory_sidecar upsert --file cards.json

# 检索（--top-k / --mood / --kind / --key 都是可选的）
python -m memory_sidecar search --query "我不能吃什么" --top-k 5

# 删除 / 统计
python -m memory_sidecar delete --ids c1,c2
python -m memory_sidecar stats

# 常驻 HTTP 服务（推荐：模型只加载一次，之后的检索都是热的）
python -m memory_sidecar serve --port 8788
```

启动会打一行：

```
[memory_sidecar] 监听 http://127.0.0.1:8788  数据目录=E:\study\desktop-pet\data\memory_sidecar  卡片=3  模型预热=开
```

- **数据目录**：默认 `<repo>/data/memory_sidecar`，可用 `--data-dir` 或环境变量
  `DESKTOP_PET_MEMORY_DIR` 覆盖。
- **模型懒加载**：服务起来时只读卡片（BM25 也是脏了才建），两个 transformers 模型在
  后台线程里**串行**预热（并发加载会炸 `Cannot copy out of meta tensor`）。
  所以 `/health` / `/list` 在服务起来后几毫秒内就能回答，不用等模型。
- **预热完补索引**：冷启动期间写进来的卡先挂在「待写向量」队列上，预热结束时一次性
  补进 Chroma；之后的新写由下一次 `/search` 补（见第 5 节）。
- **Ctrl+C / SIGTERM** 优雅退出。

## 2. 端点

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/health` | — | `{ok, cards, cardsTotal, pendingIndex, models:{embed,rerank}, bm25, degraded, degradation, dataDir, port, uptimeSec}` |
| GET | `/list` | `?kind=&limit=&include_superseded=` | `{ok, kind, includeSuperseded, cards:[...], count, total}`（重要度 desc → 新近 desc，默认 50 条） |
| GET | `/stats` | — | `{ok, stats:{cards, active, superseded, byKind, pendingIndex, vector, bm25, rerank, degradation...}}` |
| GET | `/graph` | `?limit=&top_k=&mode=all\|neighbors` | `{ok, projection, dim, mode, nodes:[...], edges:[...]}`（语义图，见 9.1） |
| GET | `/sleep/plan` | — | `{ok, clusters, blur, forget, counts}`（睡觉计划，只算不做，见 9.2） |
| GET | `/` | — | 端点清单 |
| POST | `/search` | `{query, top_k, mood?, kind?, kinds?, key?}` | `{ok, query, results:[...], path, degraded[, gate][, modelPending][, error]}` |
| POST | `/upsert` | `{cards:[...]}` | `{ok, upserted}` |
| POST | `/delete` | `{ids:[...]}`（也接受逗号字符串） | `{ok, deleted}` |
| POST | `/supersede` | `{oldId, card}` | `{ok, upserted}` |
| POST | `/rebuild` | — | `{ok, rebuilt:{vector, bm25, errors}}` |
| POST | `/sleep/apply` | `{clusters:[{ids, summary}], blur?, forget?}` | `{ok, merged, blurred, forgotten, newCards}`（见 9.2） |

`top_k` 也接受驼峰写法 `topK`。

`path` 说明这次是谁给的候选：`hybrid`（向量 + 词面）/ `vector` / `bm25` /
`pending`（只有「还没进向量索引的卡」被词面兜底命中）/ `fallback` / `none`。
`modelPending=true` 表示模型的向量腿这一轮没在预算内跑完（先用词面结果回答，
**不是**降级，/health 的 `modelTimeouts` 会累加）。

**约定：`/search` 永远返回 200 + 可解析 JSON。** 哪怕两条粗排腿全挂，也是

```json
{"ok": false, "results": [], "degraded": true, "error": "记忆检索不可用：..."}
```

调用方不需要为 500 写分支。

### `/list`：不带关键词时怎么把卡列出来

`recall_memory` 没给关键词时不该只能说「库里有 N 条卡」—— `GET /list` 直接把卡按
**重要度 desc → 更新时间 desc** 列出来（同分按 id 稳定排序）：

```
GET /list?kind=fact&limit=20
{"ok": true, "kind": "fact", "count": 20, "total": 37, "cards": [
  {"id": "c1", "key": "master", "kind": "fact", "text": "主人不吃辣",
   "category": "preference", "mood": "happy",
   "importance": 0.9, "status": "active", "createdAt": 1793264984577,
   "updatedAt": 1793264984577, "hitCount": 2, "lastHitAt": 1793264990000}
]}
```

- `count` = 本次返回条数；`total` = 符合 `kind` 的总条数（被 `limit` 截断时两者不等）。
- `limit` 默认 50、上限 500；传 0 / 负数 / 不是数字都当「没传」（不报错）。
- 默认只列 `status=active`；要连历史卡（被 `supersede` 掉的旧卡）一起看就加
  `?include_superseded=1`（等价写法 `?status=all`）。每条都带 `status`，
  调用方可以自己再过滤一次。
- **不检索、不算向量、不加载模型**（纯内存排序）。
- 存储层对应 `store.list_cards(kind=None, limit=50, include_superseded=False)`
  与 `store.count_cards(kind, include_superseded=False)`。

`results` 每条：

```json
{"id": "c1", "key": "master", "kind": "fact", "text": "主人不吃辣",
 "category": "preference", "mood": "happy", "importance": 0.9, "status": "active",
 "score": 0.1359, "rel": 0.143, "rerankLogit": 0.2881, "decay": 1.0,
 "sim": 0.5231, "createdAt": 1793264984577, "updatedAt": 1793264984577,
 "hitCount": 2, "lastHitAt": 1793264990000}
```

- `category`：写卡时塞进来的「老的内容类型」（`preference` / `shared` / ...）。
  **/list 与 /search 都带这个键**（没写就是空字符串）—— 面板的类别筛选和主动发言的
  `recent_shared()` 直接读它，不用再回读 cards.json 按 id 补。
- `sim`：这一行与 query 的**余弦相似度**（向量腿没命中就是 `null`）。
- `gate`：这次闸门做了什么 —— `{top, topSim, kept, dropped, minScoreRatio, minSimilarity}`。

## 3. 卡片格式

`cards.json` 就是一个数组（UTF-8、`ensure_ascii=False`），一张卡：

| 字段 | 说明 |
| --- | --- |
| `id` | **必填**，主键 |
| `text` | **必填**，卡片正文（截断到 500 字） |
| `kind` | `fact`（默认）/ `event` / `topic` / `thought` / `impression`，决定时间衰减半衰期 |
| `key` | 归属标识（会话 / 人物），检索时可按它过滤 |
| `mood` | 写入时的情绪；检索时同情绪有 1.15 倍加成 |
| `importance` | 0~1，权重 = `0.5 + 0.5 * importance` |
| `createdAt` / `updatedAt` | 毫秒时间戳；`updatedAt` 决定时间衰减 |
| `hitCount` / `lastHitAt` | 召回计数（sidecar 自己维护） |
| `status` | `active` / `superseded`；`supersede` 只标记不删除 |
| 其它字段 | 原样保留（方便上层塞自己的元数据） |

`supersede(old_id, new_card)` 的语义就是 UPDATE：旧卡 `status=superseded`、
`supersededBy=新 id`，新卡 `supersedes=旧 id`，检索只返回新卡。

半衰期（天）：`thought` 2 / `topic` 7 / `event` 30 / `fact` 90 / `impression` 180。

## 4. 打分

```
base   = RRF(向量排名, BM25 排名)                # 1/(60+rank)
rel    = tanh(rerank_logit / 2)                  # 压到 (-1, 1)
decay  = 0.5 ** (age_days / half_life_days)
mood   = 1.15 if card.mood == cur_mood else 1.0
imp    = 0.5 + 0.5 * importance
final  = rel * (0.4 + 0.6 * decay) * mood * imp  # 没有精排时 rel 退回 min(1, base*10)
```

和参考实现的**代码**一致（它文档里写的是 `final = rerank_logit * decay * mood * imp`，
但直接拿可为负的 logit 乘衰减会把「老的不相关记忆」顶到「新的不相关记忆」前面，
所以它的代码先把 logit 压成 rel，并用 `0.4 + 0.6 * decay`）。

### 4.1 相关度闸门（够不着就别硬塞）

老实现有这道闸门，迁到 sidecar 时丢了 —— 于是「用不上就别硬扯」只剩一句免责文案：
库里**没有答案**的问题也会硬塞 6 行、而且全是正分（评测台：无关注入平均 4.0 行）。

现在是三条规则（阈值**全部量出来**，同一把尺子 `scripts/eval_memory_recall.py`）：

| 规则 | 看什么 | 阈值 | 怎么算出来的 |
| --- | --- | --- | --- |
| ① 余弦门禁 | 最像的那条卡的余弦相似度（`gate.topSim`） | **0.42** | 有答案的问句**下限 0.456**、四条无答案问句**上限 0.376** → 取中间 |
| ② 补位行门槛 | 这一行的最终分 / 顶行最终分 | **0.12** | 融合/重排分的相对窗口（老实现量到 0.088~0.19） |
| ③ 语义保命符 | 这一行的余弦 / 顶行余弦 | **0.75** | 复合问句的另外一半 0.907（要留）、近似陷阱 0.690（要丢） |

- ① 关门 = 这一问跟记忆库没有关系 → **一条都不给**（`results: []`）。
- ② 只裁「顺带捞回来的」尾巴；③ 保证「确实是同一件事、只是精排判低」的行不被裁
  （`我同事和老板分别是谁` → 「同事小李」重排分只有顶行的 0.10，但余弦比 0.907）。
- 顶行永远保留；一条正分都没有则整轮不给。

实测（32 条语料 / 20 条问句，真模型 bge-small-zh + bge-reranker）：

| 指标 | 闸门前 | 闸门后 |
| --- | --- | --- |
| recall@6 | 1.000 | **1.000** |
| precision@6 | 0.219 | **0.623** |
| hit-rate | 1.000 | 1.000 |
| 无关注入（平均 / 最多） | 4.0 行 / 6 行 | **0.0 行 / 0 行** |
| 近似陷阱压过答案 | 0 | **0** |

三个旋钮（构造函数 kwarg / 环境变量 / 命令行都行，优先级：显式参数 > 环境变量 > 默认）：

| 旋钮 | 环境变量 | 命令行 |
| --- | --- | --- |
| `min_similarity` | `DESKTOP_PET_MEMORY_MIN_SIMILARITY` | `--min-similarity` |
| `min_score_ratio` | `DESKTOP_PET_MEMORY_MIN_SCORE_RATIO` | `--min-score-ratio` |
| `dense_keep_ratio` | `DESKTOP_PET_MEMORY_DENSE_KEEP_RATIO` | `--dense-keep-ratio` |
| `model_budget`（秒） | `DESKTOP_PET_MEMORY_MODEL_BUDGET` | `--model-budget` |

> 阈值跟**嵌入模型**绑定（余弦相似度的尺度是模型给的）。换 `DESKTOP_PET_MEMORY_EMBED`
> 之后要拿评测台重标一次；假模型（测试里的假向量）没有语义，功能测试直接
> `min_similarity=0` 把它关掉。

## 5. 数据目录

```
data/memory_sidecar/
├── cards.json          # 真值（唯一不可再生的东西）
└── chroma/             # 派生索引：可以随时删
```

**Chroma 目录删了、写坏了都没事**：启动/检索发现索引缺卡就自动从 `cards.json`
重建（`rebuild_index()`）。BM25 索引每次启动都从卡片重建，不落盘。

### 写卡为什么不等模型

索引是派生的 —— 那就没有理由让「写一条记忆」排在一把十几秒的模型锁后面
（本机实测冷启动时写卡被拖到 **19.5 秒**，这是设计错误，不是性能问题）。

| 操作 | 干什么 | 耗时量级 |
| --- | --- | --- |
| `upsert` / `delete` / `supersede` | 写 `cards.json` + 标脏（向量进挂起队列） | 毫秒级（与模型无关） |
| `search` | 重建脏了的 BM25（本地计算）→ 挂起队列词面兜底 → 模型那一路（有预算）→ 精排 → 闸门 | 稳态实测 80~104ms（top_k=6，含 HTTP）；模型很慢时 ≤ 预算（0.8s） |
| `warmup()`（serve 后台线程） | 串行加载两个模型，结束时 `sync_index()` 排空队列 | 冷启动十几秒（不阻塞写） |

**「刚写完立刻搜」为什么看得见**：检索的候选来自**真值本身**，不是只来自派生索引 ——

1. BM25 脏了就重建（纯本地计算，不等模型）；
2. **挂起队列词面兜底**：还没进向量索引的卡，用和 BM25 同一套分词（CJK 字 bigram /
   jieba 词 / 英文词）做一次字面重合，命中就并进候选（`path=pending`）；
3. 模型那一路（补向量索引 + 查向量 + 冷精排）只给 `model_budget` 预算，
   跑不完就先拿 1、2 回答，索引让它在后台补（`modelPending=true`,`/health.modelTimeouts` 会累加）。

同一把尺子实测（假模型每次 embed 睡 3 秒 = 「模型很慢」，聊天读超时 2s）：

| | upsert→search | 结果里有没有那张卡 |
| --- | --- | --- |
| 改之前 | **7742 ms**（先同步补索引 3 次 embed） | 有（但聊天端 2s 就超时了，主人看到的是空记忆块） |
| 改之后 | **820 ms** | 有 |

向量腿降级时挂起项**保留**，下次再补；卡片早就落盘了，一条都不会丢。

`/health` 的 `pendingIndex` 就是「还欠多少条向量」；它是**不抢锁**的计数快照，
所以模型正在加载时也能毫秒级回答。

## 6. 降级（都有测试）

| 挂掉的东西 | 行为 | `/health` |
| --- | --- | --- |
| 精排模型 | 只用 RRF 粗排，`rerankLogit=null` | `models.rerank="degraded"`, `degraded=true` |
| BM25（bm25s 没装） | 只走向量 | `bm25="degraded"` |
| 模型还没加载完 | 写卡照常（毫秒级），向量挂起；检索**不等它**（词面结果先答） | `pendingIndex>0`, models 仍是 `unloaded`, `modelTimeouts` 涨 |
| 向量模型 | 只走 BM25，**照样回答** | `models.embed="degraded"` |
| 模型不在本地缓存 | 立刻失败（不联网），BM25 照样回答，错误里指 prefetch 脚本 | `models.embed="missing"` |
| Chroma 打不开 / 文件坏了 | 清空重建 → 从 `cards.json` 补索引 → 继续服务 | 有 `Chroma 异常` 记录 |
| 全都挂 | `/search` 200 + `{ok:false, results:[], error:"..."}` | `degraded=true` |

模型加载失败后有 30 秒冷却（`retry_cooldown`），避免每次检索都卡在加载上。

## 7. 模型

| 用途 | 默认 | 覆盖方式 |
| --- | --- | --- |
| 向量 | `BAAI/bge-small-zh-v1.5`（512 维） | `DESKTOP_PET_MEMORY_EMBED` |
| 精排 | **按设备自动选**：有 CUDA → `BAAI/bge-reranker-v2-m3`，否则 → `BAAI/bge-reranker-base` | `DESKTOP_PET_MEMORY_RERANK` |

精排为什么要分设备（同一批 16 对 query/候选，同一台机器实测）：

| 模型 | 16 对重排（GPU） | （CPU） | 语义用例分离度 |
| --- | --- | --- | --- |
| `BAAI/bge-reranker-base` | 209 ms | 291 ms | **1.28x**（几乎分不开） |
| `BAAI/bge-reranker-v2-m3` | **41 ms** | 1016 ms | **2.7x** |

- **有 CUDA** → v2-m3：又快又能分开（41ms / 2.7x）。
- **没有 CUDA** → base：v2-m3 在 CPU 上要 1016ms，比 base 的 291ms 慢 3.5 倍，
  纯 CPU 机器上反而拖慢每一次检索。
- 规则写在 `store.resolve_rerank_model()`；启动时会打一行日志说明「选了哪个、为什么」
  （走 stderr，命令行子命令的 stdout 仍然只有一行 JSON）：

```
[memory_sidecar] 精排模型 = BAAI/bge-reranker-v2-m3（检测到 CUDA：v2-m3 在 GPU 上 41ms、分离度 2.7x（base 是 209ms、1.28x））
```

- `DESKTOP_PET_MEMORY_RERANK` **仍然优先**：设了就听它的，自动选择只在没设时生效。

### 7.1 运行时**绝不联网下载**

模型都在 HF 默认缓存里（`%USERPROFILE%\.cache\huggingface\hub`）。sidecar 只读本地缓存：

- `SentenceTransformer` / `CrossEncoder` 都传 `local_files_only=True`；
- 加载前先**只查磁盘**（`models--<owner>--<name>/snapshots/*`，认 `HF_HUB_CACHE` /
  `HF_HOME` / `XDG_CACHE_HOME`）—— 缺模型连 transformers 都不 import，毫秒级失败；
- 失败信息直接指路：

```
向量模型不在本地缓存：BAAI/bge-small-zh-v1.5（运行时不会联网下载）。
先跑一次 python scripts/prefetch_models.py 把模型备好；
或者设 DESKTOP_PET_VECTOR_ALLOW_DOWNLOAD=1 允许 sidecar 自己下载。
```

- 确实要让它自己备模型：`DESKTOP_PET_VECTOR_ALLOW_DOWNLOAD=1`（默认关）。
- `/health` 的 `models.embed` / `models.rerank` 会区分：

| 值 | 意思 | 主人要做的事 |
| --- | --- | --- |
| `unloaded` | 还没加载（懒加载 / 预热中） | 等一会儿 |
| `ready` | 就绪 | — |
| `missing` | **不在本地缓存** | 跑 `scripts/prefetch_models.py` |
| `degraded` | 在缓存里但加载/推理失败 | 看日志，或 `--force` 重下 |
| `disabled` | 被开关关掉（`--no-vector` / `--no-rerank`） | — |

实测提示：bge-reranker-base 对短中文查询的打分偏低（logit 常在 0~1 之间），
卡片措辞会影响排序 —— 例如查询「我不能吃什么」时，卡片「主人不吃辣」得 0.29，
而「主人不吃辣，吃辣会胃疼」只有 0.07。这也是为什么有 GPU 就用 v2-m3。

依赖（`requirements.txt` 里已声明）：`bm25s`（BM25 腿，缺了自动降级成纯向量）、
`jieba`（中文分词，缺了退化成字 + 字 bigram）。两个都**缺了也能跑**。

## 8. 测试

```bash
# 全部（假模型，不需要网络 / 不需要 API key / 不加载真模型）
python -m pytest tests/test_memory_sidecar.py tests/test_memory_sidecar_server.py -q

# 真实模型（要 1.1GB 模型 + 十几秒加载，默认跳过）
DESKTOP_PET_MEMORY_SIDECAR_REAL=1 python -m pytest tests/test_memory_sidecar_real_models.py -q -s
```

## 9. 语义图与睡觉

### 9.1 `/graph`：语义图

把记忆卡投影成一张图：**节点 = 记忆卡，边 = 语义上挨着的两张卡**。

- 位置取自 Chroma 里已存的向量（归一化的 bge-small-zh，512 维），PCA 降到 3 维 ——
  意思相近的记忆在图上自然挨着。三个轴各自按标准差开方缩放，否则第一主成分会吃掉
  绝大部分方差，整张图被压成一条线。
- 边默认取每个节点的余弦近邻（`top_k` 个）；`mode=all` 时改成「所有超过相似度
  下限的连线」，并且**保证每个点至少有一条**（它最像的那条）—— 不然会有点孤零零挂着。
- 只读派生索引，**不碰模型**：向量已经在 Chroma 里，缺模型照样出图。
  没有向量 / 少于两张卡就返回 `ok=false`，调用方退回自己的布局。

开发者面板的记忆 Tab 默认用 `mode=all`（图上所有线都画出来）；点一个节点只留跟它
相连的线，点空白处全部回来。

### 9.2 睡觉：整理记忆

睡觉做三件事。**在哪算、动哪张卡都在 sidecar**；模型只负责写摘要那句话。

| 动作 | 判定 | 做什么 |
| --- | --- | --- |
| **归组** | 向量相似度 ≥ 0.65（绝对下限），且 ≥ 0.85×各自最像的那条（相对下限） | 一组的卡合并成一条新卡，旧卡标 `superseded` |
| **模糊** | 重要度 ≤ 0.35 且 ≥ 30 天没碰过 | 重要性 ×0.45，标 `fuzzy`（已模糊过的不重复模糊） |
| **遗忘** | 重要度 ≤ 0.35 且 ≥ 180 天没碰过 | 卡片删掉 |

- 「多久没碰」取 `max(updatedAt, lastHitAt, createdAt)`。
- 归组用**单链聚合**，一组最多 12 张（超了就不并 —— 一句话概括不出十几件事）。
- 相似度阈值是照着 bge-small-zh 实测调的：真相关的两句在 0.80~0.92，不相干的在
  0.52~0.64 —— 0.65 卡在中间，0.55 会把噪音也归进来。
- 归组里的卡不再重复进模糊 / 遗忘名单。

流程分两步，模型不参与判定：

```
GET  /sleep/plan   → {clusters:[{id, ids, texts, ...}], blur:[...], forget:[...], counts}
   ↓  调用方（面板）拿 clusters 里的 texts 去问模型要每组一句摘要
POST /sleep/apply  {clusters:[{ids, summary}]}  → 真正动卡片
```

`/sleep/apply` 没给 `summary` 的组**不合并** —— 宁可少做，也不把内容丢了。

写完卡后台补一次向量索引（不阻塞响应），所以合并出来的新卡过一会儿才出现在语义图里。

