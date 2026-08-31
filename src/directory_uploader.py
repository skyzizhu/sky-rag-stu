"""保留相对路径的目录上传组件。

Streamlit 原生目录选择在点击选择时通常保留 webkitRelativePath，但把目录直接
拖入控件时，部分浏览器只返回文件名。本组件在浏览器端递归读取目录句柄，统一
生成 NFC 格式的完整相对路径，再把小型知识目录传给 Python。
"""

from __future__ import annotations

import base64
import binascii
import unicodedata
from pathlib import PurePosixPath

import streamlit as st

MAX_DIRECTORY_BYTES = 64 * 1024 * 1024


def _safe_relative_parts(path: str, *, allow_hidden: bool = False) -> tuple[str, ...]:
    """校验并规范化浏览器传来的目录相对路径。"""
    raw_parts = PurePosixPath(str(path).replace("\\", "/")).parts
    parts = tuple(
        unicodedata.normalize("NFC", part)
        for part in raw_parts
        if part not in ("", ".", "/")
    )
    if (
        len(parts) < 2
        or any(part == ".." or "\x00" in part for part in parts)
        or (not allow_hidden and any(part.startswith(".") for part in parts))
    ):
        raise ValueError(f"无效的目录文件路径：{path}")
    return parts


def decode_directory_batch(batch: dict) -> list[tuple[tuple[str, ...], bytes]]:
    """解码组件数据；写磁盘前再次校验路径、大小与重复项。"""
    if not isinstance(batch, dict) or not isinstance(batch.get("files"), list):
        raise ValueError("没有读取到有效的目录内容，请重新选择目录。")

    decoded: list[tuple[tuple[str, ...], bytes]] = []
    seen: set[tuple[str, ...]] = set()
    total_bytes = 0
    for item in batch["files"]:
        if not isinstance(item, dict):
            raise ValueError("目录文件数据格式不正确，请重新选择目录。")
        parts = _safe_relative_parts(item.get("path", ""))
        if parts in seen:
            raise ValueError(f"目录中存在重复路径：{'/'.join(parts)}")
        seen.add(parts)
        try:
            content = base64.b64decode(item.get("content", ""), validate=True)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise ValueError(f"文件内容读取失败：{'/'.join(parts)}") from exc
        declared_size = item.get("size")
        if not isinstance(declared_size, int) or declared_size < 0 or declared_size != len(content):
            raise ValueError(f"文件大小校验失败：{'/'.join(parts)}")
        total_bytes += len(content)
        if total_bytes > MAX_DIRECTORY_BYTES:
            raise ValueError("目录总大小超过 64 MB，请拆分后再导入。")
        decoded.append((parts, content))

    if not decoded:
        raise ValueError("目录中没有可上传的非隐藏文件。")
    return decoded


def decode_native_directory_uploads(uploads) -> list[tuple[tuple[str, ...], bytes]]:
    """读取 Streamlit 原生目录选择结果，并拒绝丢失了目录名的拖拽结果。"""
    decoded: list[tuple[tuple[str, ...], bytes]] = []
    seen: set[tuple[str, ...]] = set()
    for upload in uploads or []:
        parts = _safe_relative_parts(upload.name, allow_hidden=True)
        if any(part.startswith(".") for part in parts):
            continue
        if parts in seen:
            raise ValueError(f"目录中存在重复路径：{'/'.join(parts)}")
        seen.add(parts)
        decoded.append((parts, upload.getvalue()))
    if not decoded:
        raise ValueError("目录中没有可上传的非隐藏文件。")
    return decoded

_HTML = """
<div class="directory-dropzone" role="region" aria-label="拖拽目录到此处">
  <div class="upload-icon">⇧</div>
  <div class="upload-title">拖拽目录到此处</div>
  <div class="upload-subtitle">保留中文名称和全部子目录</div>
  <div class="upload-limit"></div>
</div>
<div class="directory-message" aria-live="polite"></div>
"""

_CSS = """
.directory-dropzone {
  min-height: 142px;
  border: 1.5px dashed color-mix(in srgb, var(--st-primary-color) 34%, #94a3b8);
  border-radius: 16px;
  background: color-mix(in srgb, var(--st-primary-color) 4%, transparent);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 18px;
  cursor: pointer;
  text-align: center;
  transition: border-color .16s ease, background .16s ease, transform .16s ease;
  box-sizing: border-box;
}
.directory-dropzone:hover, .directory-dropzone.is-dragging {
  border-color: var(--st-primary-color);
  background: color-mix(in srgb, var(--st-primary-color) 9%, transparent);
  outline: none;
}
.directory-dropzone.is-dragging { transform: translateY(-1px); }
.upload-icon {
  width: 34px;
  height: 34px;
  border-radius: 11px;
  display: grid;
  place-items: center;
  color: #fff;
  background: linear-gradient(135deg, #5145e5, #7c3aed);
  font-size: 22px;
  line-height: 1;
  margin-bottom: 4px;
  box-shadow: 0 6px 18px rgba(81, 69, 229, .22);
}
.upload-title { color: var(--st-text-color); font-size: 14px; font-weight: 650; }
.upload-subtitle { color: color-mix(in srgb, var(--st-text-color) 62%, transparent); font-size: 12px; }
.upload-limit { color: color-mix(in srgb, var(--st-text-color) 46%, transparent); font-size: 11px; margin-top: 4px; }
.directory-message {
  min-height: 22px;
  margin-top: 7px;
  color: color-mix(in srgb, var(--st-text-color) 68%, transparent);
  font-size: 12px;
  line-height: 1.45;
}
.directory-message.success { color: #15803d; }
.directory-message.error { color: #dc2626; }
"""

_JS = r"""
export default function(component) {
  const { parentElement, setStateValue, data } = component;
  const zone = parentElement.querySelector('.directory-dropzone');
  const message = parentElement.querySelector('.directory-message');
  const limit = parentElement.querySelector('.upload-limit');
  const maxBytes = data.maxBytes;

  const normalizePart = (value) => (value || '').normalize('NFC');
  const formatSize = (bytes) => bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(1)} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;

  limit.textContent = `单次最多 ${Math.round(maxBytes / 1024 / 1024)} MB`;

  function showMessage(text, kind = '') {
    message.textContent = text;
    message.className = `directory-message ${kind}`.trim();
  }

  function normalizePath(path) {
    return String(path || '')
      .replaceAll('\\', '/')
      .split('/')
      .filter(Boolean)
      .map(normalizePart)
      .join('/');
  }

  async function fileToBase64(file) {
    return await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
      reader.onerror = () => reject(reader.error || new Error(`无法读取 ${file.name}`));
      reader.readAsDataURL(file);
    });
  }

  async function walkHandle(handle, parentPath, output) {
    const currentPath = parentPath ? `${parentPath}/${normalizePart(handle.name)}` : normalizePart(handle.name);
    if (handle.kind === 'file') {
      output.push({ file: await handle.getFile(), path: currentPath });
      return;
    }
    for await (const child of handle.values()) {
      await walkHandle(child, currentPath, output);
    }
  }

  function readEntryFile(entry) {
    return new Promise((resolve, reject) => entry.file(resolve, reject));
  }

  function readEntryBatch(reader) {
    return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
  }

  async function walkEntry(entry, parentPath, output) {
    const currentPath = parentPath ? `${parentPath}/${normalizePart(entry.name)}` : normalizePart(entry.name);
    if (entry.isFile) {
      output.push({ file: await readEntryFile(entry), path: currentPath });
      return;
    }
    const reader = entry.createReader();
    while (true) {
      const entries = await readEntryBatch(reader);
      if (!entries.length) break;
      for (const child of entries) await walkEntry(child, currentPath, output);
    }
  }

  async function collectDroppedItems(event) {
    const output = [];
    const items = Array.from(event.dataTransfer?.items || []);
    for (const item of items) {
      if (item.kind !== 'file') continue;
      if (typeof item.getAsFileSystemHandle === 'function') {
        const handle = await item.getAsFileSystemHandle();
        if (handle) await walkHandle(handle, '', output);
      } else if (typeof item.webkitGetAsEntry === 'function') {
        const entry = item.webkitGetAsEntry();
        if (entry) await walkEntry(entry, '', output);
      }
    }
    if (output.length) return output;

    // 最后兜底只接受仍带相对路径的 File；绝不再伪造 uploaded-directory。
    for (const file of Array.from(event.dataTransfer?.files || [])) {
      const path = normalizePath(file.webkitRelativePath);
      if (!path.includes('/')) {
        throw new Error('浏览器没有提供目录名称，请点击控件选择目录，或换用 Chrome/Edge 后重试。');
      }
      output.push({ file, path });
    }
    return output;
  }

  async function publish(entries) {
    const usable = entries
      .map(({ file, path }) => ({ file, path: normalizePath(path) }))
      .filter(({ path }) => path && !path.split('/').some(part => part.startsWith('.')));
    if (!usable.length) throw new Error('目录中没有可上传的非隐藏文件。');

    const uniquePaths = new Set();
    for (const entry of usable) {
      if (!entry.path.includes('/')) throw new Error('没有读取到目录名称，已取消导入。');
      if (uniquePaths.has(entry.path)) throw new Error(`目录中存在重复路径：${entry.path}`);
      uniquePaths.add(entry.path);
    }

    const totalBytes = usable.reduce((sum, entry) => sum + entry.file.size, 0);
    if (totalBytes > maxBytes) {
      throw new Error(`目录总大小 ${formatSize(totalBytes)}，超过 ${formatSize(maxBytes)} 限制。`);
    }

    showMessage(`正在读取 ${usable.length} 个文件…`);
    const files = [];
    for (const entry of usable) {
      files.push({
        path: entry.path,
        size: entry.file.size,
        type: entry.file.type || '',
        content: await fileToBase64(entry.file),
      });
    }
    const roots = [...new Set(files.map(item => item.path.split('/')[0]))];
    const batch = {
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      roots,
      totalBytes,
      files,
    };
    setStateValue('batch', batch);
    showMessage(`已选择 ${roots.join('、')} · ${files.length} 个文件 · ${formatSize(totalBytes)}`, 'success');
  }

  async function run(task) {
    zone.classList.add('is-loading');
    try {
      await publish(await task());
    } catch (error) {
      showMessage(error?.message || String(error), 'error');
    } finally {
      zone.classList.remove('is-loading', 'is-dragging');
    }
  }

  if (data.summary) showMessage(data.summary, 'success');

  zone.ondragenter = (event) => { event.preventDefault(); zone.classList.add('is-dragging'); };
  zone.ondragover = (event) => { event.preventDefault(); zone.classList.add('is-dragging'); };
  zone.ondragleave = (event) => {
    if (!zone.contains(event.relatedTarget)) zone.classList.remove('is-dragging');
  };
  zone.ondrop = (event) => {
    event.preventDefault();
    run(() => collectDroppedItems(event));
  };
}
"""

_DIRECTORY_UPLOADER = st.components.v2.component(
    "sky_directory_uploader",
    html=_HTML,
    css=_CSS,
    js=_JS,
)


def directory_drop_uploader(*, key: str) -> dict | None:
    """渲染拖拽目录控件并返回 {id, roots, totalBytes, files}。"""
    component_state = st.session_state.get(key, {})
    current = component_state.get("batch") if isinstance(component_state, dict) else None
    summary = ""
    if isinstance(current, dict) and current.get("files"):
        roots = "、".join(current.get("roots") or [])
        summary = (
            f"已选择 {roots} · {len(current['files'])} 个文件 · "
            f"{current.get('totalBytes', 0) / 1024:.1f} KB"
        )
    result = _DIRECTORY_UPLOADER(
        key=key,
        data={"maxBytes": MAX_DIRECTORY_BYTES, "summary": summary},
        default={"batch": current},
        on_batch_change=lambda: None,
        height="content",
    )
    return result.batch if result and isinstance(result.batch, dict) else None
