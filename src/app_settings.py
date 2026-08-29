"""应用设置持久化：把「设置 → 检索设置」的选择保存到 storage/settings.json。

产品视角：检索设置是使用偏好，改了就应该记住——
刷新页面、重启服务都不丢，不用每次重新调。
注意：API Key 等敏感信息仍在 .env，不会写进这个文件；
storage/ 目录在 .gitignore 里，个人偏好不会进入开源仓库。
"""

from __future__ import annotations

import json

from src.config import PROJECT_ROOT

SETTINGS_PATH = PROJECT_ROOT / "storage" / "settings.json"

# 允许持久化的设置键（与界面上的设置项一一对应）
SETTINGS_KEYS = [
    "top_k",
    "domain_choice",
    "scope_choice",
    "debug_mode",
    "query_understanding",
    "hybrid_search",
    "rerank",
]


def load_app_settings() -> dict:
    """读取已保存的设置。文件不存在或损坏时返回空字典（回退到 .env 默认值）。"""
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_app_settings(settings: dict) -> None:
    """保存设置。写入失败静默忽略（设置只是偏好，不该打断问答）。"""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
