"""V3 插件系统——管理 RAG 的可选输入源插件。

产品视角：插件是 RAG 的"输入源扩展"——
核心 RAG 流程只处理文本，插件负责把不同来源的内容变成文本。
目前已有的插件：
    web_fetcher：抓取 URL 网页正文，转为知识卡片
未来可以添加：
    Notion 导出、飞书文档、数据库连接、RSS 订阅……

设计原则：
- 插件注册与核心 RAG 流程解耦（安装/卸载不影响核心功能）
- 已安装状态持久化到 settings.json
- 每个插件独立文件（如 web_fetcher.py），不污染核心代码
"""

from __future__ import annotations

import json

from src.app_settings import load_app_settings, save_app_settings

# ---------- 插件注册表 ----------
# 每个插件的元信息（名字、描述、入口函数的模块路径）
PLUGIN_REGISTRY = {
    "web_fetcher": {
        "name": "🌐 网页内容入库",
        "description": "输入 URL，抓取网页正文，自动切片入库为知识卡片。适合收藏技术文档、产品说明、教程等。",
        "module": "src.web_fetcher",
        "version": "1.0",
    },
    # 未来插件在这里注册：
    # "notion_import": {
    #     "name": "📝 Notion 导入",
    #     "description": "连接 Notion 工作区，导入页面内容为知识卡片。",
    #     "module": "src.plugins.notion_import",
    #     "version": "1.0",
    # },
}


def get_installed_plugins() -> list[str]:
    """从 settings.json 读取已安装的插件 ID 列表。"""
    settings = load_app_settings()
    return settings.get("installed_plugins", [])


def is_installed(plugin_id: str) -> bool:
    return plugin_id in get_installed_plugins()


def install_plugin(plugin_id: str) -> bool:
    """安装插件（标记为已安装）。"""
    if plugin_id not in PLUGIN_REGISTRY:
        return False
    installed = get_installed_plugins()
    if plugin_id not in installed:
        installed.append(plugin_id)
        settings = load_app_settings()
        settings["installed_plugins"] = installed
        save_app_settings(settings)
    return True


def uninstall_plugin(plugin_id: str) -> bool:
    """卸载插件（从已安装列表移除，不删代码）。"""
    installed = get_installed_plugins()
    if plugin_id in installed:
        installed.remove(plugin_id)
        settings = load_app_settings()
        settings["installed_plugins"] = installed
        save_app_settings(settings)
    return True
