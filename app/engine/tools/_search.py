"""网络搜索工具：web_search（主用 Tavily / 降级 DuckDuckGo）。

主备策略：
    1. 如果 llm.api_key 看起来是 Tavily 风格（且配置里启用了 tavily） → 调 Tavily API
       （Tavily 专为 AI 设计，质量高；免费 1000 次/月，访问 tavily.com 注册）
    2. 否则 → 降级到 DuckDuckGo（通过 ddgs 包；零 key 永久免费；不保证可用性）

策略由 main.py 装配工具时根据 llm_cfg.tavily_api_key（可空，缺则走 DDG）传入。
两个后端都不可用时返回友好提示 + 给出 web_search 替代（用 open_website 打开 Bing 搜索）。

不把结果原样返回给模型 —— 文本通常太长，先压缩成"标题 + 摘录 + URL"列表。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Optional

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)


def register(reg: ToolRegistry, tavily_api_key: Optional[str] = None,
             max_results: int = 5, request_timeout_s: float = 10.0) -> None:
    """注册 web_search 工具。

    Args:
        tavily_api_key: 若非空，主用 Tavily 后端；否则直接走 DuckDuckGo
        max_results: 每个查询最多返回几条结果（控制返回文本长度）
        request_timeout_s: HTTP 超时
    """

    def web_search(query: str, max_n: Optional[int] = None) -> str:
        """在网络上搜索 query，返回前 N 条结果的标题/摘要/链接（Markdown）。

        主用 Tavily（需 TAVILY_API_KEY，免费档 1000 次/月，质量高）；
        Tavily 失败时自动降级 DuckDuckGo（通过 ddgs 包，零 key，无需登录）。

        参数:
            query: 搜索关键词，必填
            max_n: 最多返回几条，1~10，默认 5

        返回:
            Markdown 文本：每条 `标题\\n摘要\\nURL` 三行。模型可据此回答用户问题。
        """
        if not query or not query.strip():
            return "错误：搜索关键词不能为空"
        n = max(1, min(int(max_n if max_n is not None else max_results), 10))
        # 1) Tavily
        if tavily_api_key:
            try:
                results = _tavily_search(query.strip(), n, tavily_api_key, request_timeout_s)
                if results:
                    return _format_results_md(results, backend="Tavily")
            except Exception as e:  # noqa: BLE001
                log.warning("Tavily 搜索失败，降级到 DuckDuckGo：%s", e)
        # 2) DuckDuckGo 降级
        try:
            results = _ddg_search(query.strip(), n, request_timeout_s)
            if results:
                return _format_results_md(results, backend="DuckDuckGo")
            return ("未搜到结果。建议手动访问 Bing 搜索："
                    + "https://www.bing.com/search?q=" + urllib.parse.quote(query.strip()))
        except Exception as e:  # noqa: BLE001
            log.warning("DuckDuckGo 搜索失败：%s", e)
            return (
                f"搜索失败（{e}）。建议手动访问 Bing 搜索："
                + "https://www.bing.com/search?q=" + urllib.parse.quote(query.strip())
            )

    reg.register(Tool(
        name="web_search",
        description="在网络上搜索关键词，返回前 N 条结果的标题/摘要/链接。"
                    "主用 Tavily（advanced depth + 近 30 天过滤，结果更精准更新）；"
                    "降级 DuckDuckGo（免 key）。"
                    "**query 写法建议**：要拿时效性内容（如「最新一集」「最近新闻」「今年榜单」）时，"
                    "请在 query 里加具体时间词（2024/2025/最新/本月/上周），否则容易搜到几年前的老内容。"
                    "例：搜「无职转生 最新一集 剧情」不如「无职相对飞龙 2025 最新剧透」精准。"
                    "可代替「打开浏览器搜 XXX」—— 你拿到结果后直接总结给用户，不要再说「已打开浏览器」。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "搜索关键词（必填）。时效性内容务必带年份/月份/最新等时间词。"},
                "max_n": {"type": "integer",
                          "description": "返回结果数，1~10，默认 5"},
            },
            "required": ["query"],
        },
        fn=web_search,
    ))


# ---------- 后端实现 ----------

def _tavily_search(query: str, n: int, api_key: str, timeout_s: float) -> list[dict]:
    """调 Tavily /search API。返回 [{title, url, content}, ...]。

    关键：raw_content（页面清理后的正文）+ max_results 上调到 10，content 截取 800 字。
    解决 Tavily 默认 content 只有导航/简介导致模型拿不到实质内容的问题。
    """
    import httpx
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": min(max(n * 2, 10), 10),  # 多取一些给 _clean_content 过滤
        "search_depth": "advanced",
        "include_answer": False,
        "include_raw_content": True,             # 拿正文（不是 snippet），让模型看到完整内容
        "topic": "general",
        "days": 30,
    }
    with httpx.Client(timeout=timeout_s) as client:
        r = client.post("https://api.tavily.com/search", json=payload)
        r.raise_for_status()
        obj = r.json()
    raw = obj.get("results") or []
    out = []
    for it in raw[:n]:
        # raw_content 比 content 更长（带正文），优先用；fallback 到 content
        body = (it.get("raw_content") or it.get("content") or "").strip()
        body = _clean_search_body(body)
        out.append({
            "title": (it.get("title") or "").strip(),
            "url": (it.get("url") or "").strip(),
            "content": body[:800],
        })
    return out


# 噪音关键词：网站导航/登录/版权/相关推荐类 —— 几乎不含实质内容
_SEARCH_NOISE_PATTERNS = (
    re.compile(r"(网页新闻|贴吧|知道|网盘|图片|视频|地图|文库|资讯|采购|"
                r"百度首页|登录|注册|设置|帮助|免责|反馈|投诉|下载|客户端|"
                r"热门搜索|搜索历史|收藏|评论|点赞|微博|微信|空间|"
                r"国际版|app|下载|扫一扫|二维码|分享到)",
                re.UNICODE),
    re.compile(r"^[^，。！？\n]*?(?:首页|帮助|登录|注册)\s*[^，。！？\n]{0,30}$",
               re.UNICODE),
    re.compile(r"={3,}|#{3,}|\*{3,}|-{3,}|_{3,}", re.UNICODE),  # ###### 分隔符行
)


def _clean_search_body(text: str) -> str:
    """清洗 Tavily 抓取结果中的导航/广告/格式噪音。

    百度/知乎/B站等页面的 snippet 经常含大量「网页新闻 贴吧 网盘 图片 视频
    地图 文库 资讯 采购 百科 百度首页 登录 注册」这类导航词。模型看到这些
    不知道答案的内容就会瞎编。逐行丢 + 去重，保留有实质文字的行。
    """
    if not text:
        return text
    kept_lines: list[str] = []
    seen: set[str] = set()
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        # 整行噪音词占比超 40% 跳过
        noise_count = sum(1 for p in _SEARCH_NOISE_PATTERNS if p.search(line))
        if noise_count >= 1 and len(line) < 60:
            # 短行大概率是导航/按钮
            continue
        # 去重（同一行重复出现）
        if line in seen:
            continue
        seen.add(line)
        kept_lines.append(line)
    # 把多条连续 ==== #### 等分隔符清理掉
    result = "\n".join(kept_lines)
    result = re.sub(r"\s*={3,}\s*", "\n", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _ddg_search(query: str, n: int, timeout_s: float) -> list[dict]:
    """通过 ddgs 包调 DuckDuckGo HTML 接口。零 key。

    ddgs 名字经历 DDGS → duckduckgo_search → ddgs；本项目装最新 `ddgs`。
    如果本地没装 `ddgs`，抛 ImportError 让上层降级到 webbrowser 提示。
    """
    try:
        from ddgs import DDGS  # ddgs >= 7
    except ImportError as e:
        raise RuntimeError(
            "未安装 ddgs 包（Tavily 失败后无法降级）。"
            "运行 `pip install ddgs` 安装 DuckDuckGo 后端，"
            "或在 config.yaml 设置 tavily_api_key 走 Tavily。"
        ) from e
    out = []
    with DDGS() as ddgs:
        # ddgs >= 7 用 .text() / .news() / .answers() 等；旧版用 .ddg()
        method = getattr(ddgs, "text", None) or getattr(ddgs, "ddg", None)
        if method is None:
            raise RuntimeError("ddgs 版本不兼容，缺少 text/ddg 方法")
        # timeout=timeout_s
        results = method(query, max_results=n, timeout=timeout_s) or []
        for r in results[:n]:
            out.append({
                "title": (r.get("title") or "").strip(),
                "url": (r.get("href") or r.get("url") or "").strip(),
                "content": (r.get("body") or r.get("snippet") or "").strip()[:600],
            })
    return out


def _format_results_md(results: list[dict], backend: str) -> str:
    """格式化为 Markdown 文本，便于模型总结。

    每个结果多行展示（标题 / 摘要 / URL），content 已 _clean_search_body 清洗。
    content 截到 400 字（先前 240 太少，复杂问题拿不到关键信息）。
    """
    if not results:
        return "（无结果）"
    lines = [f"搜索结果（{backend}）："]
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(无标题)"
        url = r.get("url") or ""
        content = (r.get("content") or "").strip().replace("\n", " ")
        if len(content) > 400:
            content = content[:400] + "…"
        lines.append(f"\n{i}. {title}\n   {content}\n   {url}")
    return "\n".join(lines)


__all__ = ["register"]