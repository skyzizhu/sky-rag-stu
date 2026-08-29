"""应用设置持久化：把「设置 → 检索设置」的选择保存到项目根目录 settings.json。

settings.json 纳入版本管理（不含任何敏感信息，API Key 只在 .env），
换电脑/重新克隆后设置依然生效。
"""

from __future__ import annotations

import json

from src.config import PROJECT_ROOT

SETTINGS_PATH = PROJECT_ROOT / "settings.json"
_LEGACY_PATH = PROJECT_ROOT / "storage" / "settings.json"  # 旧位置（storage 曾被 gitignore）

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
    path = SETTINGS_PATH
    if not path.exists() and _LEGACY_PATH.exists():  # 兼容旧位置：自动迁移
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_LEGACY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_app_settings(settings: dict) -> None:
    """保存设置。写入失败静默忽略（设置只是偏好，不该打断问答）。"""
    try:
        SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
