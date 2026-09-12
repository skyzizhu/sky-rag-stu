"""问答页的滚动辅助组件。

两个组件：
- scroll_chat_to_latest：一次性把「标记」滚动到固定输入框上方。
  带重试循环——长对话 rerun 后布局是逐步完成的，单次滚动经常算错
  目标位置（表现为滚一半停在中途、或停在顶部没滚到底）。
- scroll_follow_stream：流式回答期间持续跟随内容增长贴底滚动，
  让用户始终看得到最新输出的内容；用户主动上滑阅读时自动停止跟随。
"""

from __future__ import annotations

import streamlit as st

_HTML = '<span class="chat-scroll-marker" aria-hidden="true"></span>'
_CSS = """
.chat-scroll-marker { display:block; width:1px; height:1px; pointer-events:none; }
"""

# 滚动容器：优先 stMain（Streamlit 主滚动区），取不到时退回窗口滚动
_SCROLLER_JS = """
const getScroller = () => document.querySelector('[data-testid="stMain"]');
"""

_SCROLL_JS = """
export default function(component) {
  const { parentElement, data } = component;
  const offset = Number(data.bottomOffset || 0);
  const getMarker = () =>
    parentElement.querySelector('.chat-scroll-marker')
    || document.querySelector('.chat-scroll-marker:last-of-type');

  const run = (behavior) => {
    const scroller = document.querySelector('[data-testid="stMain"]');
    const marker = getMarker();
    if (!marker?.isConnected) return true;
    const markerRect = marker.getBoundingClientRect();
    let delta;
    if (scroller) {
      delta = markerRect.bottom - scroller.getBoundingClientRect().bottom + offset;
    } else {
      delta = markerRect.bottom - window.innerHeight + offset;
    }
    if (Math.abs(delta) <= 2) return true;
    if (scroller) {
      scroller.scrollTo({ top: scroller.scrollTop + delta, behavior });
    } else {
      window.scrollTo({ top: window.scrollY + delta, behavior });
    }
    return false;
  };

  // 首次按指定行为滚（smooth），后续重试用 auto 避免和进行中的平滑动画互相打架
  let attempts = 0;
  const tick = () => {
    attempts += 1;
    const done = run(attempts <= 1 ? (data.behavior || 'smooth') : 'auto');
    if (!done && attempts < 25) setTimeout(tick, 80);
  };
  requestAnimationFrame(() => requestAnimationFrame(tick));
  setTimeout(tick, 150);
}
"""

# 流式跟随：持续贴底，直到内容停止增长 / 超时 / 用户主动上滑
_FOLLOW_JS = """
export default function(component) {
  const { data } = component;
  const offset = Number(data.bottomOffset || 0);
  const maxMs = Number(data.maxSeconds || 60) * 1000;
  const start = Date.now();
  let following = true;
  let lastLen = -1;
  const scroller = document.querySelector('[data-testid="stMain"]') || window;
  const isWindow = scroller === window;

  const distToBottom = () => isWindow
    ? document.documentElement.scrollHeight - window.scrollY - window.innerHeight
    : scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;

  const pinBottom = () => {
    if (isWindow) window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'auto' });
    else scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'auto' });
  };

  // 用户上滑超过 240px 即停止跟随（把阅读主动权还给用户）
  const onScroll = () => {
    if (distToBottom() > 240) following = false;
  };
  if (!isWindow) scroller.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('scroll', onScroll, { passive: true });

  const timer = setInterval(() => {
    if (Date.now() - start > maxMs) { clearInterval(timer); return; }
    if (!following) return;
    const len = (document.querySelector('[data-testid="stMain"]')?.textContent || '').length;
    if (len === lastLen) return;  // 内容没增长不动作
    lastLen = len;
    if (distToBottom() > offset + 10) pinBottom();
  }, 200);
}
"""

_SCROLL_COMPONENT = st.components.v2.component(
    "sky_chat_scroll",
    html=_HTML,
    css=_CSS,
    js=_SCROLL_JS,
)

_FOLLOW_COMPONENT = st.components.v2.component(
    "sky_chat_follow",
    html=_HTML,
    css=_CSS,
    js=_FOLLOW_JS,
)


def scroll_chat_to_latest(*, key: str, behavior: str = "smooth", bottom_offset: int = 76) -> None:
    """把当前标记滚动到固定输入框上方；带重试循环直到布局稳定。"""
    _SCROLL_COMPONENT(
        key=key,
        data={"behavior": behavior, "bottomOffset": bottom_offset},
        height=1,
    )


def scroll_follow_stream(*, key: str, max_seconds: int = 60, bottom_offset: int = 76) -> None:
    """流式回答期间持续贴底跟随；用户上滑阅读即自动让位。"""
    _FOLLOW_COMPONENT(
        key=key,
        data={"maxSeconds": max_seconds, "bottomOffset": bottom_offset},
        height=1,
    )
