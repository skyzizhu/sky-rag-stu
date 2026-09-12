"""V3 插件：网页内容抓取器——将 URL 网页转为知识库可用的文本。

产品视角：这是一个"输入源插件"——
RAG 核心流程不感知网页的存在，它只负责处理文本。
这个插件负责把 URL 变成文本，交给现有流水线。

使用方式：
    from src.web_fetcher import fetch_and_parse
    result = fetch_and_parse("https://example.com/article")
    # result: {"text": "正文纯文本", "title": "页面标题", "url": "..."}

依赖：requests + BeautifulSoup（项目已有，无需额外安装）
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

from src.config import PROJECT_ROOT

# 请求头（模拟浏览器，避免被反爬拦截）
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_TIMEOUT = 15  # 秒
_MAX_BYTES = 5 * 1024 * 1024  # 单页正文上限 5 MB：恶意/超大页面不至耗尽内存


def fetch_html(url: str) -> tuple[str, bool]:
    """获取 URL 的 HTML 源码，返回 (html, cert_skipped)。

    SSL 校验失败时自动降级重试一次（不校验证书）。常见原因：目标网站证书链
    不完整（浏览器会自动补全中间证书所以打得开，Python 严格校验打不开）。
    个人知识库场景下 URL 是用户自己输入的，降级可接受，但 cert_skipped
    会一路传到界面提示用户。
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, stream=True)
        cert_skipped = False
    except requests.exceptions.SSLError:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT,
                                stream=True, verify=False)
        cert_skipped = True
    resp.raise_for_status()
    # 流式读取并截断：timeout 只约束单次读，不限总量
    parts: list[str] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536, decode_unicode=False):
        total += len(chunk)
        if total > _MAX_BYTES:
            break
        parts.append(chunk.decode(resp.encoding or "utf-8", errors="replace"))
    resp.close()
    return "".join(parts), cert_skipped


def extract_text(html: str) -> dict:
    """从 HTML 中提取正文纯文本 + 页面标题。

    清理规则：
    - 移除 <script>、<style>、<nav>、<header>、<footer>、<noscript>
    - 保留 <h1>~<h6> 标题为 Markdown 格式（方便切片按章节拆分）
    - 短行碎片（<15 字）合并进相邻段落
    """
    soup = BeautifulSoup(html, "html.parser")

    # 页面标题
    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    # 移除不需要的标签
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "iframe", "svg", "form", "button"]):
        tag.decompose()

    # 将标题标签转为 Markdown 格式（便于切片按章节拆分）
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            h.replace_with(f"{'#' * level} {h.get_text(strip=True)}")

    # 提取纯文本
    text = soup.get_text(separator="\n")

    # 清理：去多余空行、短行碎片合并
    lines = text.split("\n")
    merged: list[str] = []
    buffer = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if buffer:
                merged.append(buffer)
                buffer = ""
            continue
        if len(stripped) < 15:
            buffer += (" " if buffer else "") + stripped
            continue
        if buffer:
            merged.append(buffer)
            buffer = ""
        merged.append(line)
    if buffer:
        merged.append(buffer)

    clean_text = "\n".join(merged)

    return {"text": clean_text, "title": title}


def fetch_and_parse(url: str) -> dict:
    """抓取网页并提取正文。返回 {"text", "title", "url", "cert_skipped"}。"""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    html, cert_skipped = fetch_html(url)
    result = extract_text(html)
    result["url"] = url
    result["cert_skipped"] = cert_skipped

    if not result["text"] or len(result["text"]) < 50:
        raise ValueError(f"网页正文内容过短（{len(result['text'])} 字），可能无法有效入库")

    return result


def is_valid_url(url: str) -> bool:
    """检查字符串是否是合法的 HTTP/HTTPS URL。"""
    return bool(re.match(r"^https?://[^\s/$.?#].[^\s]*$", url.strip(), re.IGNORECASE))


# 网页快照保存目录
_SNAPSHOT_DIR = PROJECT_ROOT / "knowledge" / "reference" / "webpages"



def _sanitize_single_line(value: str) -> str:
    """压成单行：标题/URL 含换行会把任意键注入 YAML Front Matter（元数据投毒）。"""
    return re.sub(r"[\r\n]+", " ", value or "").strip()


def save_web_snapshot(url: str, text: str, title: str) -> Path:
    """将网页正文保存为 .md 文件到 knowledge/reference/webpages/ 目录。

    这样网页内容就走标准的文件入库流水线，不需要改动任何核心节点。
    文件名：URL 的 MD5 前 8 位 + 页面标题（截取 30 字）.md
    """
    import hashlib

    url = _sanitize_single_line(url)
    title = _sanitize_single_line(title)
    short_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", title[:30]) if title else short_hash
    filename = f"{short_hash}_{safe_title}.md"

    snapshot_dir = PROJECT_ROOT / "knowledge" / "reference" / "webpages"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Front Matter：让入库时自动带上 URL 和来源信息
    fm = f"""---
source: {filename}
title: {title or short_hash}
path: reference/webpages/{filename}
source_type: web
source_url: {url}
---

# {title or url}

{text}
"""

    file_path = snapshot_dir / filename
    file_path.write_text(fm, encoding="utf-8")
    return file_path
