# Live2D 模型自带外观切换 —— 进度接续（2026-09-19 更新）

## 目标
让冰糖 Live2D 用**模型自带能力**（切换情绪 / 发型 / 趣味表情），而不是把 sprite 帧动画硬套上去。
实测结论：模型有 **7 个情绪表情 + 9 个发型/头饰开关**，**没有真正换装系统**（衣服烘焙死；
Param81/82/84/85/88/89、Param44/46/47/48 是物理形变参数，不产生换装）。换装需更换模型。

---

## ★ 最终技术方案（已落地、已验证）

### 表情/情绪必须走官方 ExpressionManager，不能直接写 core 参数
- 直接在 `core.update` 前后 `setParameterValueById` / `addParameterValueById` 表情参数都**不稳定**
  （受 SDK 每帧生命周期 `saveParameters → expressionManager.update → … → core.update → loadParameters`
  竞争影响，loadParameters 每帧把参数恢复成 save 快照）。09-18 笔记里"set 无效、add 有效"的结论**已被推翻**。
- 可靠路径：
  - **内置情绪**：`model.expression(name)`（经 ExpressionManager）。
  - **模型支持但没注册成 exp3 文件的表情**（吐舌/鼓腮/思考/触摸微笑/睡觉闭眼）：
    动态 `em.createExpression(payload)` 注册成自定义表情，再 `em.setExpression(idx)`。
  - **自然**：`em.resetExpression()`。
  - **发型**：仍用 `core.setParameterValueById` 绝对值（持久、与表情独立共存，已验证）。

### 自定义表情注册（bridge `playCustomExpression`）
- payload：`{Type:'Live2D Expression', FadeInTime, FadeOutTime, Parameters:[{Id,Value,Blend}]}`，
  Blend ∈ Add / Multiply / Overwrite。
- 注册时 **`em.expressions[idx] = expr` 必须用下标直接赋值，不能 push**：
  `expressions` 是与 `definitions` 按 index 对齐的稀疏数组，push 会从 index 0 放，
  导致 setExpression 按旧下标去网络加载不存在的 File（`__tongue` → 404）并回落 default。
  `idx = em.definitions.length; definitions.push({Name,File}); expressions[idx]=expr`。
- 按 name 缓存 index，同名只注册一次。
- 闭眼（ParamEyeLOpen/ParamEyeROpen 默认值是 1，Add 关不掉）必须用 **Blend='Overwrite', Value=0**。

### 去重拦截坑（已修）
`ExpressionManager.setExpression` 里有 `if(t===expressions.indexOf(currentExpression)) return false`。
`resetExpression()` 只把空 default 放进播放队列，**不更新 currentExpression 引用**，
导致"自然 → 再点回上一个情绪"被判定为已在播放而拒绝。修复：bridge `resetExpressionState()`
在 reset 后手动 `em.currentExpression = em.defaultExpression`（indexOf 变 -1）。

### 源码改动（已落地、py_compile 通过、e2e 全绿）
- `app/animation/live2d_bridge.html`：
  - `_expressionManager()`（路径 `internalModel.motionManager.expressionManager`）；
  - `async playCustomExpression(name, params, fadeIn, fadeOut)`（下标赋值、按 name 缓存、返回 Promise<bool>）；
  - `resetExpressionState()`（reset + 同步 currentExpression）；
  - 旧 `resetExpression(extraResetIds)` 委托给 resetExpressionState；
  - `setExpression`(内置)、`applyParams`(发型/直接写参数)、`setParam`(姿态平滑)、drawMesh 去水印保持不变。
- `app/animation/live2d_renderer.py`：
  - `ACTION_MAP` 每项加 `kind`：'pose'（姿态，setParam 平滑，结束回正）/ 'expr'（表情，走自定义 Expression，结束恢复情绪）；
  - 常量 `_SMILE_PARAMS/_THINK_PARAMS/_SLEEP_PARAMS`；`_TEMP_POSE_PARAMS=["ParamBodyStretch"]`
    （眉/眼/嘴/吐舌/鼓腮不再手动归零，交给 ExpressionManager）；
  - `_play_emotion(key)`：用 `_parse_expressions` 解析出的**干净参数**（已剔除 red 里的 Paramheadxy 水印项）
    注册成 `__emo_<key>` 自定义表情，从源头避免水印；
  - `_play_custom_expr(slot, params, hold_ms, fadeIn, fadeOut)`：hold_ms 到期自动恢复情绪；
  - `_restore_emotion_if_awake` / `_restore_emotion`；
  - 重写 `set_emotion/reset_emotion/set_idle/set_sleep/set_wake/set_thinking/play_reaction/play_animation`。

### 验证（tools/verify_e2e.py，700×700、scale0.7、关眨眼）
状态机 currentExpression 下标/参数全部正确：
- 黑脸 idx=18 [Paramexpblack=1]；脸红 idx=19 [Paramlove2=1]（**无 Paramheadxy 水印**）；
  惊讶 idx=20 [chouxiang=1, eyeout=1]。
- 关右马尾生效；发型+黑脸共存（黑脸 idx=18 不变）。
- 吐舌 idx=21 [Paramtongueout=1]，结束后回 idx=18 黑脸；鼓腮 idx=22 [CheeckPuff=1]。
- 自然 idx=-1（空）；思考 idx=23 [BrowLY/RY=1, EyeBallY=-1]；回待机 idx=-1。
- 睡觉 idx=24 [EyeLOpen/EyeROpen/MouthOpenY=0]（Overwrite，截图闭眼）；醒来 idx=-1（截图睁眼）。
- 截图：tools/preview/look/e2e/00..13（含 _face 脸部裁剪）。
- 注意：帧外读 `core.getParameterValueById(表情参数)` 永远是默认值（loadParameters 已恢复），
  要读状态请读 `expressionManager.currentExpression._parameters`，画面以截图/像素 diff 为准。

---

## 剩余待办
1. **全量回归** `E:\python3.10.10\python.exe -m pytest tests -q`（基线 319 passed，末尾 offscreen Qt 清理 traceback 是噪声）。
2. **真实桌宠 UI 实测**：右键菜单切情绪/发型、玩一下（吐舌/鼓腮）、双击、触摸、AI 思考、睡眠/唤醒，确认共存与复位。
3. 向用户说明：**模型无换装**（已实测衣服烘焙死），菜单只提供情绪 + 发型 + 趣味表情；换装需更换模型。

## 关键参数（verbatim）
- 7 情绪：hah{Paramlove3:0.966}、angery{Paramlove4:1}、black{Paramexpblack:1}、
  red{Paramlove2:1（另含 Paramheadxy:30 水印，已在参数层剔除）}、meimao1{ParamgameX8:-1}、
  meimao2{ParamgameX8:1}、O O{Paraexpchouxiang:1, Paramexpeyeout:1}。
- 9 发型 ParamgameX2~7（30=隐藏 / 0=显示）：HAIR11 去左发包、HAIR22 去左马尾、HAIR33 去左蝴蝶结、
  hairR1 去右发包、HAIR2 去右马尾、HAIR3 去右蝴蝶结、Rhair 左侧披肩、danhair 收双马尾、danhairL 右侧披肩。
- 趣味：Paramtongueout 吐舌（单 e，idx124）、ParamCheeckPuff 鼓腮（Cheeck 双 e，idx128）、ParamMouthOpenY 张嘴。
- 水印：注册名 expression1/expression2（shuiyin1/2），触发 Paramheadxy*；drawMesh 已拦截 12 个水印 drawable
  （指纹 694：682/683/688/691/692/693 + 名字含 WaterMark）；情绪参数层也剔除。
- 环境：解释器 `E:\python3.10.10\python.exe`；PowerShell GL 噪声 `2>$null`；长命令自动转后台；
  Edit 要求本会话**完整 Read**（不带 offset/limit）后才能改；QImage ARGB32 是 BGRA，numpy 转 RGB 用 `[:,:,[2,1,0]]`。
- 版权：模型 Bilibili 神宫凉子 UID 13737731，仅限个人桌面自用。

---

# 2026-09-19 超频猫猫模型接入：Live2D 通用引擎 + 每模型映射配置

## 架构变化
- 新增 `app/animation/live2d_model_profile.py`：每个模型目录放一份 `*.model.yaml`，
  描述表情分类（特殊/发型/配件/手势/表情）、触发映射（聊天情绪/工具动作→条目）、
  睡眠参数、水印处理、随机表情池。渲染器/右键菜单/设置页全部由它驱动。
- `live2d_renderer.py` 的冰糖硬编码（ACTION_MAP、_EMOTION_ALIAS、发型表、水印指纹）
  迁入 `E:/study/live2d/bingtang/bingtang/bingtang.model.yaml`；超频猫猫配置在
  模型目录 `超频猫猫.model.yaml`。
- HTTP 服务回传 model3.json 时动态注入 Motions（Idle/Sleep）与 Expressions 段
  （超频猫猫/冰糖的 model3.json 均未注册表情与动作），原模型文件不动。

## 关键坑与修复（重要）
1. **mask 组数超限**：超频猫猫有 40 个裁剪组 > 默认 1 张 mask 渲染纹理的 36 上限，
   裁剪失效导致贴图乱层。桥端按 drawable maskCount 重初始化渲染器
   `renderer.initialize(coreModel, need)`（need = ceil(用mask的drawable数/32)）。
2. **动作回滚直接参数**：pixi-live2d-display 每帧 `core.update()` 后调
   `loadParameters()`，直接写 core 的参数（发型/配件/睡眠开关）会被待机动作
   周期性清掉。桥端监听 `beforeModelUpdate` 事件每帧重写 `_keptParams/_keptParts`
   （applyParams / hideParts 自动登记）。
3. **超频猫猫水印**：是独立部件 `Part14`（cdi3: "0000 星雾语企划水印002.psd"），
   profile `watermark: {mode: part, part_ids: [Part14]}` → 桥端 hideParts 置 0。
   不是 Key1 参数（那是 VTS 里的显示开关，置 0 无效）。

## 能力接口（PetRenderer 新增默认实现，sprite 不受影响）
- `get_menu_groups() / activate_menu_item() / reset_all_appearance()`：分类外观菜单
- `get_play_options()`：玩一下菜单（live2d 按 profile 动作映射动态生成）
- `set_random_expressions() / is_random_expressions_enabled() / note_activity()`：
  挂机随机表情（idle 定时器，聊天/交互暂停），config.yaml pet.live2d.random_expression* 控制

## 右键菜单（live2d）结构
切换表情（自然+17脸）· 切换发型（默认+6）· 特殊(4)/配件(10)/手势(15) 子菜单 ·
复位全部外观 · 随机表情开关 · 睡觉/醒来 · 吃饭 · 玩一下（9 个映射动作）
设置窗口新增「Live2D」Tab：分类条目下拉+应用、复位全部、随机表情开关。

## 验证
- `python tools/verify_chaopin.py`：HTTP 注入 → 表情/发型/手势/配件/睡觉/思考/复位 全流程截图
  （tools/preview_chaopin/）。
- `python tools/verify_live2d.py`：冰糖回归。
- pytest 329 通过（含新增 tests/test_live2d_model_profile.py）。
