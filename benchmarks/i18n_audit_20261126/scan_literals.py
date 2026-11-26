#!/usr/bin/env python3
"""AST 扫描 app.py：可见 st.* 控件的第一位置参数若为字符串常量（非 t() 调用），
即多语言缺口候选。过滤 CSS/HTML/装饰性常量，按可见文本处理。"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

VISIBLE = {"button", "caption", "subheader", "header", "title", "write", "text_input",
           "text_area", "number_input", "selectbox", "multiselect", "radio", "checkbox",
           "toggle", "info", "success", "warning", "error", "toast", "help", "metric",
           "download_button", "file_uploader", "tabs", "page_link", "popover", "chat_input",
           "markdown", "label"}


def literal_label(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (isinstance(node, ast.JoinedStr) or
            (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod))):
        return "<f-string>"
    if isinstance(node, ast.Call):
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        return f"<call:{name}>" if name else None
    return None


def main(path: str) -> None:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in VISIBLE and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "st"):
            continue
        if not node.args:
            continue
        label = literal_label(node.args[0])
        if label is None:
            continue
        if label in ("<f-string>",) or label.startswith("<call:"):
            # 动态拼接：抓取其中的纯字符串常量片段（粗粒度，供人工复核）
            frags = [n.value for n in ast.walk(node.args[0])
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and len(n.value) >= 2 and not n.value.startswith(("<", ":", "#", "{"))
                     and not n.value.rstrip().endswith(("}", ">", ":", ";"))]
            hits.append((node.lineno, node.func.attr, "DYNAMIC", " | ".join(frags)[:160]))
            continue
        s = label.strip()
        if (not s or s.startswith(("<", ":", "#", "{")) or s.endswith(("}", ">", ":", ";", '"'))
                or set(s) <= set("·—-| /()[]{}0123456789.%:pxem#") or len(s) < 2):
            continue
        hits.append((node.lineno, node.func.attr, "LITERAL", s[:120]))
    for lineno, attr, kind, text in hits:
        print(f"{lineno:5d}  st.{attr:<12} {kind:<8} {text}")
    print(f"---- 共 {len(hits)} 处候选 ----", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "app.py")
