# Live2D Integration · Live2D 模型接入

## 概述

桌宠支持两种渲染：PNG 帧动画（sprite）和 Live2D（Cubism 4 + pixi-live2d-display）。Live2D 不可用时自动 fallback sprite。切换方式：

```yaml
# config.yaml
pet:
  renderer: live2d
  live2d:
    model_dir: "assets/live2d/超频猫猫完整版/超频猫猫"
    scale: 0.5
    random_expression: true       # 挂机随机表情
    random_expression_min_s: 25
    random_expression_max_s: 70
    max_fps: 30                   # 渲染帧率上限
```

依赖：`pip install PyQtWebEngine`。

## 模型目录结构

```
超频猫猫/
├── 超频猫猫.model3.json         # Cubism 4 模型定义
├── 超频猫猫.moc3                # 编译后的二进制
├── *.png                        # 贴图
├── Expressions/                 # 表情（可选，模型注册过的）
│   └── *.exp3.json
├── motions/                     # 动作（可选）
└── *.model.yaml                 # 模型 profile（本项目约定）
```

如果模型目录没有 `*.model.yaml`，渲染器会用启发式 fallback 自动分类（见下）。

## `*.model.yaml` 五段配置

放在模型根目录或 `assets/live2d_profiles/` 模板库里。模板示例见 `assets/live2d_profiles/bingtang.model.yaml` 和 `超频猫猫.model.yaml`。

### 1. emotions（标签驱动）

把模型回复末尾的 `[happy]` 等标签映射到具体表情组：

```yaml
emotions:
  happy:    [心心眼, 微笑]
  sad:      [哭哭眼, 泪眼]
  angry:    [生气脸]
  shy:      [脸红]
  surprised: [四周星星]
  thinking: [问号, 思考手势]
  scared:   [晕晕眼]
```

未命中走 `parse_reply()` 的关键词兜底。

### 2. actions（一次性动作映射）

把 LLM 工具调用（如 `play_animation("jump")`）映射到模型具体表情：

```yaml
actions:
  jump:   右打招呼
  eat:    右喵喵拳
  file:   画板
  spin:   转圈
  stretch: 伸懒腰
```

### 3. sleep（睡眠两段式）

```yaml
sleep:
  transition: 入睡动画名      # 一次性，播完循环 sleep_idle
  idle: 睡着循环动画名
  wake: 醒来动画名
```

### 4. watermark（是否隐藏水印）

```yaml
watermark:
  fingerprint: true           # true=按 drawable 名模糊匹配隐藏水印图层
  # 冰糖（bingtang）默认需要；超频猫猫不需要 fingerprint，关闭即可
```

### 5. random_expression（挂机随机表情池）

```yaml
random_expression:
  pool:                       # 表情池
    - 歪头
    - 打哈欠
    - 摇尾巴
    - 眨眼
  interval: [25, 70]          # 间隔秒数 [min, max]
  enable_in_chat: false       # 聊天时是否暂停
```

## cubism-sdk 三件套

`app/animation/cubism-sdk/` 下需要：

- `live2d.min.js` / `live2dcubismcore.min.js`
- `pixi.min.js`

来源：Cubism 官方 SDK 的 `Sample/Web/Demo/` 目录（或 `live2d.com/zh-CHS/sdk/download/cubism-sdk/` 申请）。**注意**：Cubism SDK 需申请，Live2D 公司对商业使用有限制，个人非商业使用通常免费。

## HTTP 服务绕过 Chromium CORS

`Live2DRenderer` 启动时会在 `127.0.0.1` 随机端口开一个 `ThreadingHTTPServer`，把模型目录挂出来。`live2d_bridge.html` 直接 `fetch('http://127.0.0.1:<port>/超频猫猫.model3.json')`，绕过 Chromium 默认的 `file://` CORS 限制。

回传 `model3.json` 时，handler 会**动态注入**两段：

```python
model3["FileReferences"]["Motions"] = {
    "idle": [{"File": "motions/idle.motion3.json"}],
    "Zzz":  [{"File": "motions/Zzz.motion3.json"}],
}
model3["FileReferences"]["Expressions"] = [
    {"Name": name, "File": f"Expressions/{name}.exp3.json"}
    for name in profile.emotion_names
]
```

原模型文件不动，桥端的 `ExpressionManager` / `MotionManager` 自动接管。

## Fallback 触发条件

- 未配置 `pet.live2d.model_dir`
- 目录不存在 / 没有 `*.model3.json`
- `PyQtWebEngine` 没装
- `Live2DRenderer` 构造抛异常

任意一条命中 → 自动 fallback sprite，不崩。

## 自定义表情注册

如果模型自带的 `Expressions/` 目录有 `*.exp3.json`，profile 不用声明，渲染器自动扫描；如果想改 `Name`，在 profile 的 `emotions` 里指定。

## 触发场景配置（v3.2）

设置面板「Live2D」页不再平铺外观切换入口（右键菜单仍可手动切换），改为「触发场景配置」：每个场景可自由搭配**表情 / 发型 / 配件 / 手势 / 特殊**五类外观，全部留空表示该场景不改变外观。

- **场景清单（18 个）**
  - 聊天情绪：`chat_happy` 开心、`chat_shy` 害羞、`chat_angry` 生气、`chat_sad` 难过、`chat_surprised` 惊讶、`chat_thinking` 思考
  - 状态：`thinking` AI 思考中、`sleeping` 睡觉、`wake` 醒来、`idle` 待机、`startup` 开机、`late_night` 深夜（22:00–6:00）、`idle_lonely` 许久未理（闲置 30 分钟）
  - 互动：`touch_head` 摸头、`touch_body` 摸身体、`double_click` 双击、`reminder` 收到提醒、`dragging` 拖拽中
- **持续 vs 一次性**：聊天情绪 / 状态 / 拖拽为持续场景，触发时替换当前外观（离开状态时恢复）；摸头 / 双击 / 提醒为一次性，播放到时长后自动恢复。
- **默认值**：未自定义时从模型 profile 的 `emotion_aliases` / `thinking` / `touch` / `actions` 推导，与手动触发时的外观一致。
- **自定义动作**：不预置固定动作，由用户在「自定义动作」卡新建（命名 + 触发方式 + 外观搭配 + 时长）。触发方式可绑定工具动作 hook（喂食 / 打开文件 / 转圈 / 伸懒腰 / 跳跃 / 游泳 / 吐舌 / 比耶），也可「仅手动播放」；手动动作出现在右键「玩一下」菜单。**仅 Live2D 模式，按模型分别持久化**（不同模型动作 / 外观参数不同）。
- **配置文件**：`data/live2d_scenes/<模型目录名>.json`，包含 18 个场景的覆盖值与自定义动作；删除该文件即恢复默认。

## 参考资料

- [live2d 官方示例](https://www.live2d.com/zh-CHS/learn/sample/)（官方免费模型）
- [模之屋](https://www.aplaybox.com/model)
- 本项目用过的[超频猫猫付费模型](https://www.bilibili.com/video/BV1uc3vzfEsN/)，注意版权