"""问答页的一次性滚动辅助组件。"""

from __future__ import annotations

import streamlit as st

_HTML = '<span class="chat-scroll-marker" aria-hidden="true"></span>'
_CSS = """
.chat-scroll-marker { display:block; width:1px; height:1px; pointer-events:none; }
"""
_JS = """
export default function(component) {
  const { parentElement, data } = component;
  const run = () => {
    const main = document.querySelector('[data-testid="stMain"]');
    const marker = parentElement.querySelector('.chat-scroll-marker');
    if (!main || !marker?.isConnected) return;
    const markerRect = marker.getBoundingClientRect();
    const mainRect = main.getBoundingClientRect();
    const delta = markerRect.bottom - mainRect.bottom + Number(data.bottomOffset || 0);
    if (Math.abs(delta) > 2) {
      main.scrollTo({
        top: main.scrollTop + delta,
        behavior: data.behavior || 'smooth',
      });
    }
  };
  requestAnimationFrame(() => requestAnimationFrame(run));
  setTimeout(run, 180);
}
"""

_SCROLL_COMPONENT = st.components.v2.component(
    "sky_chat_scroll",
    html=_HTML,
    css=_CSS,
    js=_JS,
)


def scroll_chat_to_latest(*, key: str, behavior: str = "smooth", bottom_offset: int = 76) -> None:
    """把当前标记滚动到固定输入框上方；组件只执行一次，不持续监听页面。"""
    _SCROLL_COMPONENT(
        key=key,
        data={"behavior": behavior, "bottomOffset": bottom_offset},
        height=1,
    )
