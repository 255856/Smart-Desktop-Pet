"use strict";
/**
 * 私有自定义模型注册表（模板）
 * ============================================
 *
 * 把本文件复制为同目录的 `custom-models.js`（已 gitignore，不会进 master 仓库），
 * 按下面的格式登记你的私有模型，刷新页面即可在下拉框看到。
 *
 * 路径约定（两种方式二选一）：
 *
 * 1) 本地 serve.py 挂载（推荐，免拷贝）：
 *        python docs/demo/serve.py --port 8765 --root . --custom-dir "assets/live2d/超频猫猫完整版"
 *    然后 path 写 `custom_models/<模型目录>/<模型>.model3.json`
 *
 * 2) 直接把模型目录拷进 `docs/demo/custom_models/`（该目录同样已 gitignore）
 *
 * 部署到 GitHub Pages：见 DEPLOY.md「私有模型上 Pages」——
 * 模型放进 gh-pages 分支的 custom_models/，Actions 部署时自动注入；
 * master 分支永远没有模型文件，git clone 拿不到。
 *
 * ⚠️ 版权提醒：模型版权归原作者。gh-pages 分支与线上 Pages 是公开的，
 * 任何能在浏览器里看到模型的人技术上都能把资源另存下来——本机制只是
 * 让模型不进源码仓库，并非防盗版手段。请只部署你拥有发布权的模型。
 */
window.CUSTOM_MODELS = [
  // {
  //   id: "chaopin_cat",            // 唯一 ID（建议 ASCII，存 localStorage 用）
  //   name: "超频猫猫",              // 下拉框 / 模型卡显示名
  //   badge: "Private",             // 角标（如 Private / Custom）
  //   path: "custom_models/超频猫猫/超频猫猫.model3.json",
  // },
];
