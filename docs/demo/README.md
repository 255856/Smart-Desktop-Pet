# Live2D Demo Page / Live2D 演示页

> 一个**完全脱离 PyQt**的独立 Live2D 渲染演示页：把桌宠项目里
> `app/animation/live2d_bridge.html` 的渲染逻辑抽出来，任何人浏览器打开就能体验。
>
> A **PyQt-free** standalone Live2D rendering demo. The renderer from
> `app/animation/live2d_bridge.html` is extracted into a browser-only page.

---

## 🌐 在线 Demo / Online Demo

部署完成后访问：

> **https://255856.github.io/Smart-Desktop-Pet/**

部署步骤见 [DEPLOY.md](DEPLOY.md)。

> **技术栈**：PIXI v7.4.0 + 原生 Cubism Core + `pixi-live2d-display` 0.3.0 真实
> Live2D 渲染管线（与桌面版 `app/animation/live2d_bridge.html` 桥接版一致）。
> 用户输入模型 URL 或拖入 `.model3.json` 即可看到**真实 Live2D 角色**渲染、
> 表情切换、口型同步、鼠标视线跟随。

---

## ⚠️ 模型版权说明 / Model Copyright Notice

> **本演示页不包含任何 Live2D 模型文件。**
>
> Live2D 模型（`*.model3.json`、`*.moc3`、`textures/*.png`、`*.motion3.json` 等）
> 受上游创作者版权约束，**禁止二次传播**。本仓库 `.gitignore` 已显式排除
> `assets/live2d/`，请勿将任何模型文件提交。
>
> 请使用你**拥有合法授权**的模型。常见的免费可商用样例：
> - [Live2D 官方 Sample "Hiyori"](https://www.live2d.com/en/sample/sample01/)
>   （仅限个人非商用，使用前阅读 EULA）
> - [Live2D 官方 Cubism SDK 自带样例](https://github.com/Live2D/CubismWebSamples)

> **This demo page contains NO Live2D model files.**
>
> Live2D models are copyrighted by their creators and **must not be
> redistributed**. The repo's `.gitignore` explicitly excludes
> `assets/live2d/`; please do not commit any model.

---

## 📂 文件清单 / Files

| 文件 | 作用 |
|---|---|
| `index.html` | 演示页入口（UI + 控件 + i18n + CDN 加载运行时） |
| `live2d-demo.js` | 渲染核心 + Agent 工具调用（剥离 QWebChannel 的独立版本） |
| `serve.py` | 零依赖本地服务器（stdlib only，带 CORS / 目录浏览 / 模型挂载） |
| `vendor/` | pixi / Cubism Core / pixi-live2d-display 本地副本（离线可用） |
| `hiyori_zh-Hans/` `miara_en/` | Live2D 官方授权演示模型（仅限个人演示，禁止二次传播） |

> **不存在的**：其他任何 `*.model3.json` / `*.moc3` / 贴图 —— 用户自备。

---

## 🚀 本地运行 / Run Locally

### 最简方式（仅静态页，不挂载模型）

```bash
python docs/demo/serve.py
# 浏览器打开 http://127.0.0.1:8765/
```

### 推荐方式（挂载模型目录）

```bash
# 假设你的模型放在 E:\live2d-models\Hiyori\
python docs/demo/serve.py --model-dir "E:\live2d-models\Hiyori"
```

启动后：

1. 浏览器打开 `http://127.0.0.1:8765/`
2. 看到根目录索引页（演示页本身）
3. 把模型 `*.model3.json` 文件拖到浏览器窗口加载，**或**
4. 在顶部 URL 框输入：`http://127.0.0.1:8765/models/Hiyori/Hiyo.model3.json`

> 💡 如果模型目录是标准 Cubism 结构（`xxx.model3.json` + 同名 `.moc3` + `textures/` + `motions/`），
> 直接拖 `xxx.model3.json` 进来即可，**不要拖整个文件夹**。

### 高级用法

```bash
# 改端口 + 改静态根 + 多个 --model-dir 不支持但可建符号链接
python docs/demo/serve.py --port 9000 --root ./docs/demo

# 挂载私有自定义模型（免拷贝，配合 custom-models.js 出现在模型下拉框）
python docs/demo/serve.py --port 8765 --root . --custom-dir "assets/live2d/超频猫猫完整版"

# 后台运行（Windows PowerShell）
Start-Process python -ArgumentList "docs/demo/serve.py","--model-dir","E:\live2d-models" -WindowStyle Hidden
```

### 私有自定义模型（如「超频猫猫」）

想让自己的模型出现在 demo 下拉框、且部署到 GitHub Pages 后**别人能看到但 git clone 拿不到**：

1. 复制 `custom-models.example.js` 为 `custom-models.js`（已 gitignore），登记模型；
2. 本地：`--custom-dir` 挂载，或把模型目录拷进 `docs/demo/custom_models/`；
3. 线上：模型放进 **gh-pages 分支**的 `custom_models/`，Actions 会自动注入并生成注册表
   —— master 分支永远没有模型文件，完整步骤与安全边界见 [DEPLOY.md](DEPLOY.md)。

---

## 🎮 页面交互 / Controls

| 控件 | 功能 |
|---|---|
| **快捷指令 chips** | 聊天框上方一排常用指令（时间 / 提醒 / 记忆 / 天气 / 搜索 / 打开网站），点击即发送 |
| **聊天输入框** | 自然语言对话：LLM Function Calling 自动选工具，无 key 时正则意图 + mock 回复 |
| **按住说话 🎤** | 浏览器 SpeechRecognition 语音输入 |
| **拖拽 `.model3.json` 链接** | 拖入 http(s) 模型链接直接加载（本地文件受浏览器沙箱限制，会给出提示） |
| **模型 URL 输入框** | 功能面板「模型」组，粘贴 `https://…/xxx.model3.json` 加载 |
| **动作 / 表情下拉 + 播放** | 播放所选动作组 / 表情 |
| **💬 测试口型** | 模拟说话嘴型同步 |
| **自动朗读 / 主动搭话开关** | 偏好设置，localStorage 持久化 |
| **查看记忆 / 遗忘 / 提醒列表** | 每条记忆与提醒支持 ✕ 单独删除 |
| **清空对话 / 导出对话** | 聊天历史管理（导出为 .txt） |
| **工具清单** | 查看网页版 18 个与桌面版同名对齐的工具 |
| **鼠标移动 / 点击模型** | 视线跟随；点击摸头 → 爱心特效 + 亲密度 +1 |
| **↺ 重置 / 切换模型** | 内置 Hiyori Pro/Free、Miara Pro 循环切换 |

## 🤖 Agent 工具对齐 / Tool Alignment

网页 Demo 注册了 **18 个与桌面版 `app/engine/tools/` 同名**的工具（浏览器沙箱内可用的子集），
配置 LLM Key 后由模型通过 **Function Calling 自主多轮调用**（ReAct：调用 → 观察 → 总结），
全流程可在调试面板 **Agent Trace** 中观察：

`get_current_time` `date_info` `calculate` `convert_units` `get_weather`（open-meteo，无需 key）
`web_search`（Tavily，无 key 用 mock）`remember_fact` `recall_memory` `forget_memory`
`add_reminder` `list_reminders` `delete_reminder` `open_website` `clipboard_copy`
`send_notification` `change_pet_emotion` `play_animation` `get_pet_status`

> 桌面版专属工具（打开本地应用、文件读写、截图、系统控制等）受浏览器安全沙箱限制，不在网页版范围内。
> 服务商不支持 `tools` 参数时自动降级为普通流式对话。

---

## 🌐 部署到 GitHub Pages / Deploy

演示页是**纯静态**的，可以直接托管：

### 方法 1：gh-pages 分支

```bash
# 在仓库根
git checkout -b gh-pages
git checkout master -- docs/demo
mv docs/demo/* .
git add . && git commit -m "Deploy demo"
git push origin gh-pages
```

GitHub Pages 设置：`Settings → Pages → Branch: gh-pages / root`。
然后访问 `https://<你的用户名>.github.io/desktop-pet/`。

### 方法 2：直接放 docs/demo/，开启 Pages 子目录

`Settings → Pages → Source: master /docs`。但**根目录浏览会被 docs 接管**，演示页需访问 `/demo/`。

### 托管后用本地模型

托管的页面是 `https://`，而你的模型在 `http://127.0.0.1:8765/`，浏览器允许跨域（页面自带 CORS 头 + Cross-Origin-Resource-Policy）。在 URL 框填入本地模型完整 URL 即可。

---

## 🔧 技术细节 / Tech Notes

### 与 `live2d_bridge.html` 的差异

| 项 | 桥接版（PyQt 内） | 演示版（独立 Web） |
|---|---|---|
| 桥接 | QWebChannel ↔ Python | 无（纯 UI） |
| SDK 来源 | 本地 `cubism-sdk/` | CDN（jsdelivr） |
| 模型来源 | 本地 HTTP server | 用户 URL 输入框 / File API |
| 入口触发 | Python 调用 `loadModel()` | 用户点"加载"或拖拽 |
| 水印处理 | 含冰糖模型特定指纹 | 仅通用 `name contains "watermark"` |
| 用途 | 桌宠本体运行 | 演示 / 验证 / 体验 |

### CDN 选择

- **PIXI v7** — `cdn.jsdelivr.net/npm/pixi.js@7.3.2/dist/pixi.min.js`
- **pixi-live2d-display cubism4** — `cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js`
- **Live2DCubismCore** — `cubism.live2d.com/sdk-web/bin/CubismSdkForWeb-4-r.1/Core/live2dcubismcore.min.js`

> `vendor/` 已内置 pixi / Cubism Core / pixi-live2d-display 三个运行时的本地副本，
> **离线也能运行**；加载顺序为 vendor → jsdelivr → unpkg 三级 fallback。

### 浏览器兼容

| 浏览器 | 版本 | 状态 |
|---|---|---|
| Chrome / Edge | 100+ | ✅ 完美 |
| Firefox | 100+ | ✅ 完美 |
| Safari | 15.4+ | ✅ 完美（File API + clipboard） |
| 移动浏览器 | 任意 | ⚠️ 演示用，桌面应用不需要 |

---

## 🐛 故障排查 / Troubleshooting

| 问题 | 原因 / 解法 |
|---|---|
| 页面空白 / SDK 加载失败 | 检查网络（CDN 需访问 jsdelivr / cubism.live2d.com） |
| `404 Not Found` 加载模型 | URL 拼写错；或模型文件没在 `--model-dir` 下 |
| `CORS / cross-origin` 报错 | 服务器已默认开 CORS；如果用别的服务器，确保响应头含 `Access-Control-Allow-Origin: *` |
| 模型渲染出**水印方框** | 通用 watermark 隐藏只匹配名称含 "watermark" 的图层；某些模型的防盗水印是伪装成普通 ArtMesh，需要走桌面端 `live2d_bridge.html` 里的"按 drawable index 指纹"逻辑（仅在仓库内置的冰糖模型上启用，避免误伤其他模型） |
| 启动后 `KeyError` / `NoneType` | 大概率是模型文件损坏或 `.model3.json` 引用了缺失的 `.moc3` / 贴图 |
| 性能差（CPU 风扇狂转） | demo 默认 `maxFPS=30`；可在 `live2d-demo.js` 改 `app.ticker.maxFPS` |

---

## 📜 License

MIT（与主项目一致）

本页含的第三方运行时：
- **Pixi.js** — MIT © GoodBoy Digital
- **pixi-live2d-display** — MIT © avgjs
- **Live2D Cubism Core** — Live2D Cubism SDK EULA（可重分发于应用程序内，不可独立售卖）
