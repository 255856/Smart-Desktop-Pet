"""MiniMax 多模态 provider —— image + video (M3.1 image / H3 video)。

2026-10-15 v11: 主人截图 MiniMax Code 套餐表
(Free / Go / Explore / Build) 提到:
  - 文本模型 M3.1 Flash Preview
  - 视频模型 H3
  - 图像和声音模型
所以 MiniMax 是「文本+视频+图像+音频」全栈, 实际 API endpoint 跟 OpenAI
不完全兼容, 这里留 stub 引导主人接真 endpoint。

接法 (任选, 不挑死):
  1. MiniMax 官方 API: https://api.MiniMax.chat/v1/...  走 OpenAI 兼容
     (跟 openai.py 大同小异, 改 _endpoint / _api_key 来源)
  2. 视频走 MiniMax H3 专门接口 (异步 task + 轮询), 跟文生图不同路径
  3. 走 https://api.bailian.console.aliyun.com/ 阿里云百炼
     里的 Qwen-Image / Wan2.1 等 (国内可达)

本文件留 handler 签名 + 清晰错误, 主人接到真 endpoint 之后把
`gen_image` / `gen_video` 里的 raise 替换成真 HTTP 调用即可。
"""
from __future__ import annotations

import logging
import os

from app.brain.multimodal_router import ApiCapability, register

log = logging.getLogger(__name__)


def gen_image(prompt: str, **kwargs) -> str:
    """MiniMax image generation —— v11 留 stub, 等主人接真 endpoint。

    推荐的 endpoint 选项:
      1. MiniMax API: https://api.MiniMax.chat/v1/images/generations
         (OpenAI 兼容, 直接复用 openai.py 的 _post_json 套路)
      2. 阿里云百炼: https://dashscope.aliyuncs.com/api/v1/services/aigc/
         text2image/image-generation
         (model=qwen-image-plus, 走 DashScope SDK)
    """
    raise RuntimeError(
        "MiniMax.gen_image: 还没接真 endpoint。\n\n"
        "接法 (任选):\n"
        "  1. MiniMax 官方 — 跟 openai.py 一样走 OpenAI 兼容协议, "
        "改 _endpoint() / _api_key() 来源, 用 MINI_MAX_API_KEY 环境变量。\n"
        "  2. 阿里云百炼 Qwen-Image — pip install dashscope, "
        "调 dashscope.ImageSynthesis.call(model='qwen-image-plus', prompt=prompt)。\n"
        "  3. 其它国内 image gen provider 都行, 改 gen_image 函数体。\n\n"
        "改完 gen_image 即可, 其它 (router / /api 命令 / 多模态 menu) "
        "不用动, 直接能用。"
    )


def gen_video(prompt: str, **kwargs) -> str:
    """MiniMax H3 video generation —— v11 留 stub。

    MiniMax H3 视频生成通常是异步 task + 轮询模式:
      1. POST 创建 task, 返 task_id
      2. GET /tasks/{task_id} 轮询 status (PENDING / PROCESSING / SUCCESS / FAILED)
      3. SUCCESS 后返 video_url

    或者接其它:
      - Replicate cogvideox / animate-diff
      - 智谱 CogVideoX
      - Pika / RunwayML (海外)
    """
    raise RuntimeError(
        "MiniMax.gen_video: 还没接真 endpoint。\n\n"
        "接法 (任选):\n"
        "  1. MiniMax H3 官方 — 通常是 POST /tasks + 轮询 task status, "
        "URL 在 status=SUCCESS 时返回。\n"
        "  2. 阿里云百炼 Wan2.1 — dashscope.VideoSynthesis.call(), "
        "model='wan2.1-t2v-turbo', 同样异步 + 轮询。\n"
        "  3. Replicate (海外) — pip install replicate, replicate.run('stability-ai/...', input={...})。\n\n"
        "改完 gen_video 即可, 其它不用动。"
    )


def setup() -> None:
    cap = ApiCapability(
        name="MiniMax",
        display_name="MiniMax (M3.1 + H3 + 图像声音模型)",
        supports_image=True,
        supports_video=True,
        supports_audio=True,   # MiniMax 有语音 / 音乐模型
        model_image="M3.1-image",
        model_video="H3",
        model_audio="M3.1-voice",
        notes=(
            "MiniMax Code 套餐: Free 文本+图像声音限可用, Go/Explore/Build 全栈。\n"
            "实际 HTTP endpoint 没接 (stub 状态), 改本文件 gen_image / gen_video 即可。"
        ),
    )
    register(cap, gen_image=gen_image, gen_video=gen_video)
