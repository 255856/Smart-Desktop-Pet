# Demo 部署说明 / Deployment Guide

本文档说明怎么把 `docs/demo/` 部署到 **GitHub Pages**，让别人在浏览器里直接打开体验。

> **TL;DR**
> ```
> https://255856.github.io/Smart-Desktop-Pet/
> ```
> 部署完成后上面这个 URL 就能访问。

---

## 📍 当前状态

- ✅ `gh-pages` 分支已推送（包含 `index.html` + `live2d-demo.js` + `README.md`）
- ⏳ **Pages 还没启用** —— 仓库设置里点一下就行（约 30 秒）

---

## 🚀 启用 GitHub Pages（首次设置）

### 步骤 1：打开仓库设置

```
https://github.com/255856/Smart-Desktop-Pet/settings/pages
```

### 步骤 2：Source 选 `gh-pages` 分支

| 字段 | 值 |
|---|---|
| **Source** | `Deploy from a branch` |
| **Branch** | `gh-pages` / `(root)` |

### 步骤 3：保存

等 1-2 分钟，页面顶部会出现：
> ✅ Your site is live at `https://255856.github.io/Smart-Desktop-Pet/`

---

## 🔄 后续自动部署（推荐）

本仓库已配 GitHub Actions：`.github/workflows/deploy-demo.yml`

- **触发**：push 到 `master`，且 `docs/demo/**` 有改动
- **结果**：自动构建并部署到 GitHub Pages，无需手动操作

### 工作原理

```
master 分支（带完整项目）
       │
       │  push 触发 Actions
       ▼
┌─────────────────────────────────────┐
│ .github/workflows/deploy-demo.yml    │
│   1. checkout                        │
│   2. cp docs/demo/* → _demo/        │ ← 构建产物（仅 3 个文件）
│   3. actions/upload-pages-artifact   │
│   4. actions/deploy-pages@v4         │
└─────────────────────────────────────┘
       │
       ▼
gh-pages 分支（自动管理）
       │
       ▼
https://255856.github.io/Smart-Desktop-Pet/
```

> ⚠️ **注意**：如果你看到 GitHub 设置里 `Source` 变成 `GitHub Actions`，
> 这是 Actions 部署接管后的正常状态，**不要**改回 `Branch`。

---

## 🔧 手动重新部署（应急）

如果 Actions 抽风，可以本地重建：

```bash
# 在仓库根目录
git worktree add /tmp/gh-pages gh-pages 2>/dev/null || rm -rf /tmp/gh-pages
mkdir -p /tmp/gh-pages
cp docs/demo/index.html docs/demo/live2d-demo.js docs/demo/README.md /tmp/gh-pages/
cd /tmp/gh-pages
git init -q
git checkout -q --orphan gh-pages
git add .
git -c user.email="<你的邮箱>" -c user.name="<你的名字>" commit -q -m "Deploy Live2D Demo"
git remote add origin https://github.com/255856/Smart-Desktop-Pet.git
git push -f origin gh-pages
```

---

## 🌐 部署后访问方式

### 普通访问

```
https://255856.github.io/Smart-Desktop-Pet/
```

直接进 demo 页。顶部会有 **Model URL** 输入框，**用户自备模型**填进去即可。

### 嵌入到 README

已经做了。README 顶部徽章区有 `🎬 Live2D Demo` 链接 + 目录锚点链。

### 嵌入到第三方网站（iframe）

```html
<iframe src="https://255856.github.io/Smart-Desktop-Pet/"
        width="800" height="600"
        style="border:1px solid #444;border-radius:8px">
</iframe>
```

---

## ❓ 关于"别人怎么看模型"

**关键事实**：Live2D 模型有版权，**不能托管在 GitHub Pages**（GitHub ToS 也禁止）。

所以部署后的体验是：

| 场景 | 用户怎么做 |
|---|---|
| 体验完整功能 | 自己起 `serve.py --model-dir <自己的模型目录>`，在 URL 框填本地地址 |
| 只想看看渲染效果 | URL 框留空，看页面 UI 和控件（无模型时画布为空，但 UI 完整） |
| 录演示视频 | 用 `serve.py` 本地加载模型 + 屏幕录制 |

**官方可加载的样例模型**：Live2D 官方 [Hiyori](https://www.live2d.com/en/sample/sample01/) 是允许个人非商用加载的；用户可以本地下载后用 `serve.py --model-dir` 挂载。

---

## 🐛 故障排查

| 现象 | 原因 / 解法 |
|---|---|
| `404` | Pages 没启用 → 走上面"启用 GitHub Pages" |
| 页面 200 但空白 | 查看右侧日志 "SDK 加载完成" 段，看每个 SDK 实际从哪个源加载的；都从 jsdelivr/unpkg 加载说明 vendor 没被部署，重新跑 Actions |
| Actions 一直排队 | Settings → Actions → 启用 workflow 权限 |
| `gh-pages` 分支冲突 | 用 `git push -f`（孤儿分支 + 强制推送是正常流程）|

### CDN 加速（如果 jsdelivr / cubism.live2d.com 慢）

~~把 vendor JS 拷到 docs/demo/vendor/~~ **已默认启用**，见下面说明。

**加载策略：vendor → jsdelivr → unpkg 三级 fallback**

- ✅ 首选 `docs/demo/vendor/*.js`（零网络、零延迟、GitHub Pages 也能加载）
- ✅ vendor 加载失败时自动回退 jsdelivr
- ✅ jsdelivr 失败时自动回退 unpkg
- ✅ 三个都失败才报"SDK 加载失败"

vendor 文件夹已包含 3 个 JS（808KB 总大小）：

```
docs/demo/vendor/
├── pixi.min.js                   456 KB
├── cubism4.min.js                146 KB
└── live2dcubismcore.min.js       207 KB
```

**与桌面版保持同源同版本**（来自 `app/animation/cubism-sdk/`，与
`live2d_bridge.html` 用的完全一致），不会因为 SDK 版本不一致出现玄学 bug。

部署时 `vendor/` 会被一起部署到 gh-pages，浏览器加载页面时**第一次请求
就拿到 SDK**，不需要走任何 CDN。

页面右侧日志会显示每个 SDK 实际从哪个源加载的，方便排查：

```
SDK 加载完成：
  pixi      ← vendor/pixi.min.js
  cubism4   ← vendor/cubism4.min.js
  core      ← vendor/live2dcubismcore.min.js  ← ok
```

---

## 📜 部署物清单

`gh-pages` 分支根目录只有 3 个文件，**全部不包含版权内容**：

```
gh-pages/
├── index.html         # 演示页（加载 CDN 运行时）
├── live2d-demo.js     # 渲染逻辑（纯引擎，无模型）
└── README.md          # 使用说明
```

仓库主分支的 `.gitignore` 已显式排除 `assets/live2d/`，模型文件**永远不会被推上去**。
