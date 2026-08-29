"""调试追踪（Trace）：记录 RAG 一次问答中每个节点的发生时间、做了什么、输入与输出。

产品视角：这是「节点时间线」的数据结构——
调试模式下，界面把每个节点渲染成一张可展开的卡片：
什么时候发生 → 花了多久 → 做了什么 → 输入是什么 → 输出是什么。
用于学习「一次问答到底经过了哪些环节」。
"""

from __future__ import annotations

from datetime import datetime


def now_str() -> str:
    """当前时间点（年月日 时分秒），节点时间线的时间戳。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_node(
    icon: str,
    name: str,
    *,
    time_str: str | None = None,
    elapsed: float | None = None,
    status: str = "已执行",
    summary: str = "",
    items: list[tuple[str, str]] | None = None,
) -> dict:
    """构造一个节点的追踪记录。

    - status："已执行" 或 "直通（规划 V2 启用）" 等说明；
    - items：[(标签, 内容), ...]，标签如 输入 / 输出 / 去重逻辑 / 改写 Prompt。
    """
    return {
        "icon": icon,
        "name": name,
        "time": time_str or now_str(),
        "elapsed": elapsed,
        "status": status,
        "summary": summary,
        "items": items or [],
    }
