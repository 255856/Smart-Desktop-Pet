# 桌宠精灵图生成 Prompt 清单

> 用法：把每条 prompt **单独**复制到多模态模型/SD/Midjourney，生成 1024×1024 绿幕背景 MP4。
> 角色一致性的关键：每条 prompt 都**重复**写明完整角色描述，不要省略。

---

## 📁 文件命名 & 目录结构

模型生成后，请按下面命名放进 `sprite_gen/` 对应目录：

```
sprite_gen/
├── idle_calm/          # 第1组
│   ├── idle_calm_00.jpg
│   ├── idle_calm_01.jpg
│   ├── idle_calm_02.jpg
│   └── idle_calm_03.jpg
├── idle_blink/         # 第2组
├── idle_bounce/        # 第3组
├── idle_stretch/       # 第4组（可选）
├── touch_head_shy/
├── touch_head_purr/
├── touch_body_shy/
├── walk_left/
├── walk_right/
├── sidehide_left/
├── sidehide_right/
├── happy/
├── sad/
├── angry/
├── shy/
└── think/
```

放好后启动 `python main.py` 即可，桌宠会自动加载所有动画。

---

## 💡 提高一致性的小技巧

1. **同一家服务**：全用豆包（或全用 SD），不同模型风格差异大
2. **同提示词前缀**：每次都从「角色基准描述」开头
3. **同负面词**：每个 prompt 末尾都有 `NOT 3D, NOT CGI, NOT realistic, NOT photo`
4. **锁定 seed**：豆包/SD 大多支持 seed，相同 seed + 相似 prompt 出图会更一致
5. **首先生成 1 张做基准**：先单独跑 idle_calm 满意后，再批量生成其它状态
6. **后期处理**：用 ImageMagick / Pillow 把白底裁掉