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

| 场景 | 用户怎么做 |
|---|---|
| 看你部署的私有模型 | 直接打开 Pages demo，下拉框切换（模型从 gh-pages 分支提供，见上文「私有模型上 Pages」） |
| 看内置官方样例 | 打开 Pages demo，Hiyori / Miara 零配置自动加载（模型同样来自 gh-pages 分支） |
| 用自己的模型完整体验 | 本地 `serve.py --custom-dir <模型目录>` 或 `--model-dir` 挂载 |
| 只想看看渲染效果 | URL 框留空，看页面 UI 和控件 |

**版权提醒**：模型文件受上游创作者版权约束。官方 Hiyori/Miara 样例仅限个人非商用演示；
你自己的私有模型请确认拥有对外展示权后再放上 gh-pages（技术边界见上文安全边界表）。

---

## 🔐 私有模型上 Pages（如「超频猫猫」）

需求：**线上 demo 页能看到自己的私有模型，但别人 `git clone` 仓库拿不到模型文件。**

实现方式与内置 Hiyori / Miara 一致——**模型只放 `gh-pages` 分支，master 永远没有**：

```
master 分支                     gh-pages 分支                   Pages 线上
├── docs/demo/index.html        ├── hiyori_zh-Hans/             ├── index.html（master 最新）
├── docs/demo/live2d-demo.js    ├── miara_en/                   ├── live2d-demo.js（master 最新）
└── .github/workflows/…         ├── custom_models/              ├── hiyori_zh-Hans/
    （push 触发部署）            │   └── 超频猫猫/               ├── miara_en/
                                 └── （模型只在这里）             ├── custom_models/超频猫猫/
                                                                 └── custom-models.js（自动生成）
```

部署 Actions（`deploy-demo.yml`）构建时会：master 取页面代码 + gh-pages 取全部模型，
并扫描 `custom_models/` **自动生成 `custom-models.js` 注册表**（扫描各目录下的
`*.model3.json`，无需手写）。

### 把私有模型放上 gh-pages（一条命令序列）

```bash
# 在仓库根目录（模型源：assets/live2d/超频猫猫完整版/超频猫猫）
git worktree add /tmp/gh-pages gh-pages
mkdir -p /tmp/gh-pages/custom_models
cp -r "assets/live2d/超频猫猫完整版/超频猫猫" /tmp/gh-pages/custom_models/
cd /tmp/gh-pages
git add custom_models
git -c user.email="<你的邮箱>" -c user.name="<你的名字>" commit -m "Add private model 超频猫猫"
git push origin gh-pages
cd - && git worktree remove /tmp/gh-pages
```

推送后到 Actions 手动触发一次 `Deploy Live2D Demo to GitHub Pages`
（或随便 push 一个 `docs/demo/**` 改动），线上即可在下拉框看到「超频猫猫 Private」。

### 本地开发（免拷贝）

```bash
python docs/demo/serve.py --port 8765 --root . --custom-dir "assets/live2d/超频猫猫完整版"
```

`--custom-dir` 把本地模型目录挂载到 `/custom_models/*`（与 gh-pages 上的路径结构一致），
配合 `custom-models.js`（模板见 `custom-models.example.js`，已 gitignore）注册模型即可。
也可以直接把模型目录拷进 `docs/demo/custom_models/`（同样已 gitignore），无需任何参数。

### ⚠️ 必须知道的安全边界

| 事实 | 说明 |
|---|---|
| ✅ `git clone` 拿不到模型 | master 分支零模型文件（`.gitignore` 排除），克隆者只得到代码 + UI |
| ✅ 普通访客看不到模型文件链接 | 模型在渲染器内部加载，页面不展示下载入口 |
| ❌ 技术用户可以扒资源 | 浏览器必须下载 `.moc3`/贴图才能渲染，DevTools Network 里都能看到并另存——**任何公开网页都做不到真防下载** |
| ❌ gh-pages 分支本身是公开的 | 知道分支名的人可以 `git clone -b gh-pages` 拉到模型；介意请改用私有 CDN + 签名 URL |
| ⚖️ 版权自负 | 只部署你**拥有发布权**的模型；Live2D 官方样例（Hiyori/Miara）仅限个人非商用演示 |

---

## 🐛 故障排查

| 现象 | 原因 / 解法 |
|---|---|
| `404` | Pages 没启用 → 走上面"启用 GitHub Pages" |
| 页面 200 但空白 | 查看右侧日志 "SDK 加载完成" 段，看每个 SDK 实际从哪个源加载的；都从 jsdelivr/unpkg 加载说明 vendor 没被部署，重新跑 Actions |
| Actions 一直排队 | Settings → Actions → 启用 workflow 权限 |
| `gh-pages` 分支冲突 | 用 `git push -f`（孤儿分支 + 强制推送是正常流程）|

### CDN 加速（如果 jsdelivr 慢）

~~把 vendor JS 拷到 docs/demo/vendor/~~ **已默认启用**，见下面说明。

**加载策略：vendor → jsdelivr → unpkg 三级 fallback**

- ✅ 首选 `docs/demo/vendor/*.js`（零网络、零延迟、GitHub Pages 也能加载）
- ✅ vendor 加载失败时自动回退 jsdelivr
- ✅ jsdelivr 失败时自动回退 unpkg
- ✅ 三个都失败才报"SDK 加载失败"

vendor 文件夹包含 2 个 JS（663KB 总大小）：

```
docs/demo/vendor/
├── pixi.min.js                   456 KB
└── live2dcubismcore.min.js       207 KB
```

`pixi-live2d-display`（cubism4）走 jsdelivr CDN（实测 0.3.0 UMD 跟 PIXI v7 全量包兼容，
且关键要 `s.async = false` 强制顺序加载）。

---

## 📜 部署物清单

```
master 分支（源码，零模型）         gh-pages 分支（模型仓库）         Pages 线上（两者合并）
docs/demo/index.html               hiyori_zh-Hans/  miara_en/        master 的页面代码
docs/demo/live2d-demo.js           custom_models/私有模型/          + gh-pages 的全部模型
docs/demo/vendor/*.js              （git clone master 拿不到）       + 自动生成的 custom-models.js
docs/demo/custom-models.example.js
```

仓库主分支的 `.gitignore` 已显式排除 `assets/live2d/`、`docs/demo/custom_models/`、
`docs/demo/custom-models.js`，模型文件**永远不会进 master**。
