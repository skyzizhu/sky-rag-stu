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

import json
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


def _sanitize_single_line(value: str) -> str:
    """压成单行：标题/URL 含换行会把任意键注入 YAML Front Matter（元数据投毒）。"""
    return re.sub(r"[\r\n]+", " ", value or "").strip()


def read_snapshot_origin(path: Path) -> dict | None:
    """读网页快照文件头里的来源：{url, title, domain, category}；不是网页快照或读不了返回 None。

    网页的原文网址只在快照文件头里（卡片元数据里没有），文件随归档 / 恢复一起移动，
    所以文件头是网页来源唯一可靠的记录。
    """
    from src.metadata import infer_from_path, parse_front_matter

    if Path(path).suffix.lower() != ".md":
        return None
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            head = fh.read(8192)  # 文件头在最前面且很短，不必读全文
    except OSError:
        return None
    front_matter, _ = parse_front_matter(head)
    url = str(front_matter.get("source_url") or "").strip()
    if front_matter.get("source_type") != "web" or not url:
        return None
    path_domain = infer_from_path(str(front_matter.get("path") or ""))["domain"]
    return {"url": url, "title": str(front_matter.get("title") or "").strip(),
            "domain": str(front_matter.get("domain") or path_domain),
            "category": str(front_matter.get("category") or "")}


def save_web_snapshot(url: str, text: str, title: str, domain: str = "reference",
                      category: str | None = None, knowledge_dir: Path | None = None,
                      target: Path | None = None, *,
                      blocked_paths: frozenset[Path] = frozenset(),
                      destination: Path | None = None,
                      existing_paths: tuple[Path, ...] = (),
                      include_legacy: bool = False) -> Path:
    """把新快照写入所选领域。已有 URL 复用原路径，保留文档 ID 和版本历史。

    同一网址改选领域时通过 Front Matter 更新领域。历史快照已有的分类声明随原文件保留。
    """
    import hashlib

    from src.domains import validate_code
    from src.upload_plan import available_path

    domain = validate_code(domain)
    url = _sanitize_single_line(url)
    title = _sanitize_single_line(title)
    short_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    safe_title = re.sub(r'[\\/:*?"<>|\s]+', "_", title[:30]) if title else short_hash
    root = knowledge_dir or PROJECT_ROOT / "knowledge"
    if target is None:
        # 文件名前缀只有 8 位哈希，用文件头里的网址再确认一次，避免两个网址撞前缀
        pattern = f"{short_hash}_*.md"
        candidates = sorted(destination.glob(pattern)) if destination is not None else []
        if destination is None or include_legacy:
            candidates.extend(sorted((root / "reference" / "webpages").glob(pattern)))
            candidates.extend(p for p in sorted(root.glob(f"*/{pattern}"))
                              if p.parent.name != "archive")
        candidates.extend(p for p in existing_paths if p.match(pattern))
        target = next((p for p in candidates if p.is_file()
                       and p.resolve() == root.resolve() / p.relative_to(root)
                       and p.relative_to(root) not in blocked_paths
                       and (read_snapshot_origin(p) or {}).get("url") == url), None)
        if target is None:
            target = available_path(destination or root / domain, f"{short_hash}_{safe_title}", ".md")
    target = Path(target)
    if target.resolve() != root.resolve() / target.relative_to(root):
        raise ValueError("网页快照路径不能通过符号链接访问")
    if blocked_paths and target.relative_to(root) in blocked_paths:
        raise ValueError("当前账号无权覆盖已有网页快照")
    if category is None:
        category = (read_snapshot_origin(target) or {}).get("category")
    target.parent.mkdir(parents=True, exist_ok=True)
    filename = target.name
    try:
        rel_path = target.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        rel_path = f"{domain}/{filename}"

    declared = ""
    if domain != rel_path.split("/", 1)[0]:
        declared += f"domain: {domain}\n"
    if category and category != "webpages":
        declared += f"category: {json.dumps(_sanitize_single_line(category), ensure_ascii=False)}\n"

    # Front Matter：让入库时自动带上 URL 和来源信息
    fm = f"""---
source: {filename}
title: {title or short_hash}
path: {rel_path}
source_type: web
source_url: {url}
{declared}---

# {title or url}

{text}
"""

    target.write_text(fm, encoding="utf-8")
    return target
