"""V3.8：索引纪元——跨进程的检索缓存一致性。

任何改变检索结果的写操作（入库、删除、版本过期、改元数据）都调用 bump()，
把一个全新的随机值写进 storage/index_epoch。各进程的 BM25 缓存与问答缓存在使用前
对比 current()：与上次构建时不同就整体作废。

为什么需要：BM25 缓存原先只靠「卡片总数变化」判断是否过期——只改元数据的写操作
（档案编辑改可见部门 / 分类）卡片数不变，缓存永不失效；
而 MCP 服务是独立进程，前台进程里调用的 invalidate_cache() 根本通知不到它。
收窄文档可见部门后，其他部门仍能经 BM25 通道检索到它——这是一个权限问题。

用内容而不是修改时间比较：粗粒度文件系统上两次写入可能拿到同一个时间戳。
"""

from __future__ import annotations

import os
import uuid

from src.config import PROJECT_ROOT

EPOCH_PATH = PROJECT_ROOT / "storage" / "index_epoch"


def bump() -> None:
    """标记「知识库已变化」：所有进程的检索缓存在下次使用时作废。失败不抛出。"""
    try:
        EPOCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = EPOCH_PATH.with_name(f"{EPOCH_PATH.name}.{os.getpid()}.tmp")
        tmp.write_text(uuid.uuid4().hex, encoding="utf-8")
        os.replace(tmp, EPOCH_PATH)
    except OSError:
        pass


def current() -> str:
    """当前纪元；文件不存在时为空串（等价于「从未变化」）。"""
    try:
        return EPOCH_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
