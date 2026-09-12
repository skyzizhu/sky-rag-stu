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
    "theme",
    "language",
    "installed_plugins",
]


def _normalize_choices(settings: dict) -> dict:
    """把旧版「界面中文值」迁移成语言无关的代码值（i18n 引入前的存量设置）。

    domain_choice：「全部」→「all」，「work · 学习」→「work」
    scope_choice：「仅 active」→「active」，「包含归档」→「all」，「仅归档」→「archive」
    同时做类型/取值校验：手改坏或旧版本写入的不兼容值一律回退默认，
    否则一个坏值会让整个应用启动即崩。
    """
    dc = settings.get("domain_choice")
    if dc is not None and not isinstance(dc, str):
        settings["domain_choice"] = "all"
    dc = settings.get("domain_choice")
    if dc:
        if dc in ("全部", "All", "すべて"):
            settings["domain_choice"] = "all"
        elif " " in dc:
            settings["domain_choice"] = dc.split(" ")[0]
    sc = settings.get("scope_choice")
    if sc is not None and not isinstance(sc, str):
        settings["scope_choice"] = "active"
    sc = settings.get("scope_choice")
    if sc:
        settings["scope_choice"] = {
            "仅 active": "active", "Active only": "active", "active のみ": "active",
            "包含归档": "all", "Include archived": "all", "アーカイブを含む": "all",
            "仅归档": "archive", "Archived only": "archive", "アーカイブのみ": "archive",
        }.get(sc, sc)
    if sc not in (None, "active", "all", "archive"):
        settings["scope_choice"] = "active"
    # 布尔键：非布尔一律回退
    for key in ("debug_mode", "query_understanding", "hybrid_search", "rerank"):
        if key in settings and not isinstance(settings[key], bool):
            settings.pop(key)
    # top_k：整数且在滑条范围内，否则移除（回退 .env 默认值）
    tk = settings.get("top_k")
    if tk is not None and (not isinstance(tk, int) or isinstance(tk, bool) or not 1 <= tk <= 10):
        settings.pop("top_k")
    # theme / language：白名单
    if settings.get("theme") not in (None, "system", "light", "dark"):
        settings.pop("theme")
    if settings.get("language") not in (None, "system", "zh-CN", "zh-TW", "en", "ja"):
        settings.pop("language")
    # 插件清单：必须是字符串列表
    plugins = settings.get("installed_plugins")
    if plugins is not None and (not isinstance(plugins, list)
                                or not all(isinstance(x, str) for x in plugins)):
        settings.pop("installed_plugins")
    return settings


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
        return _normalize_choices(data) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_app_settings(settings: dict) -> None:
    """保存设置。先写临时文件再原子替换：写一半崩溃不会留下损坏 JSON。"""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(SETTINGS_PATH)
    except OSError:
        pass
