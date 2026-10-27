"""给她一个自己的浏览器。

宿主上跑一个 Playwright 服务（见 services/browser），这边开了六个工具：开页、读页、
点东西、截图、列页面、关页。有人发链接过来也会先自动读一遍再回话。

服务地址和家里人 QQ 都从环境变量读，见仓库根的 .env.example。"""

import os
import base64
import re
import time

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as ImageComp, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain

BROWSER_API = os.getenv("DAFEIYU_BROWSER_API", "http://host.docker.internal:9882")
PARENTS = {os.getenv("DAFEIYU_DAD_QQ", ""), os.getenv("DAFEIYU_MOM_QQ", "")}
URL_RE = re.compile(r"https?://[^\s，。！？、；：\"'<>（）()\[\]【】]+")
SHOT_RE = re.compile(r"截图|截个图|截张图|截一下|拍张照|截屏")
CACHE_TTL = 600


@register("dafeiyu_browser", "大肥鱼", "她自己的浏览器", "0.2.0")
class DafeiyuBrowser(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._cache: dict[str, tuple[float, str, str]] = {}
        self._last_url = ""

    # ---------- 跟宿主服务打交道 ----------

    async def _post(self, path: str, payload: dict, timeout: float = 70) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{BROWSER_API}{path}", json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} {r.text[:120]}")
        return r.json()

    async def _open(self, url: str, max_chars: int = 1500) -> dict:
        out = await self._post("/open", {"url": url, "max_chars": max_chars})
        if out.get("url"):
            self._last_url = out["url"]
        return out

    async def _read(self, page: str = "", url: str = "", max_chars: int = 5000) -> tuple[str, str, str]:
        """返回 (page, title, text)。"""
        hit = self._cache.get(url)
        if url and hit and time.time() - hit[0] < CACHE_TTL:
            return "", hit[1], hit[2]
        out = await self._post("/read", {"page": page, "url": url, "max_chars": max_chars})
        title = str(out.get("title") or "")
        text = str(out.get("text") or "")
        if url and text:
            self._cache[url] = (time.time(), title, text)
            self._last_url = out.get("url") or url
        return str(out.get("page") or ""), title, text

    async def _shot(self, page: str = "", url: str = "") -> bytes:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(f"{BROWSER_API}/screenshot", json={"page": page, "url": url})
        return r.content if r.status_code == 200 else b""

    async def _send_shot(self, umo: str, page: str = "", url: str = "") -> bool:
        blob = await self._shot(page, url)
        if not blob:
            return False
        await self.context.send_message(
            umo, MessageChain([ImageComp.fromBase64(base64.b64encode(blob).decode())])
        )
        return True

    # ---------- 给她的工具 ----------

    @filter.llm_tool(name="browser_open")
    async def tool_open(self, event: AstrMessageEvent, url: str) -> str:
        """用自己的浏览器打开一个网页，返回页面编号和开头的内容。

        想查资料、看新闻、看别人给的网址时，先用它打开。

        Args:
            url (string): 要打开的网址。

        Returns:
            页面编号、标题和正文开头；打不开会说打不开。
        """
        try:
            out = await self._open(url)
        except Exception as exc:  # noqa: BLE001
            return f"打不开这个网址：{exc}"
        return (
            f"打开了，页面编号 {out.get('page')}。标题：{out.get('title') or '（没有标题）'}\n"
            f"{str(out.get('text') or '')[:1500]}"
        )

    @filter.llm_tool(name="browser_read")
    async def tool_read(self, event: AstrMessageEvent, page: str = "", url: str = "") -> str:
        """读一个页面的正文：给页面编号就读那个，不给就给网址。

        Args:
            page (string): 页面编号，比如 p1。
            url (string): 网址；只给网址时会新开一个页面。

        Returns:
            标题加正文。
        """
        try:
            pid, title, text = await self._read(page, url)
        except Exception as exc:  # noqa: BLE001
            return f"读不到：{exc}"
        if not text:
            return "这个页面没读到内容。"
        return f"页面 {pid or page}｜{title or '（没有标题）'}\n{text[:4000]}"

    @filter.llm_tool(name="browser_click")
    async def tool_click(
        self, event: AstrMessageEvent, page: str, text: str = "", selector: str = ""
    ) -> str:
        """点页面上的东西：可以按文字点，也可以按 CSS 选择器点。

        想翻下一页、点开一条结果、点「登录」这类按钮时用。

        Args:
            page (string): 页面编号。
            text (string): 要点的文字。
            selector (string): CSS 选择器，二选一。

        Returns:
            点完之后的地址、标题和开头正文。
        """
        try:
            out = await self._post("/click", {"page": page, "text": text, "selector": selector})
        except Exception as exc:  # noqa: BLE001
            return f"没点动：{exc}"
        self._last_url = out.get("url") or self._last_url
        return (
            f"点完了，现在在 {out.get('url')}\n标题：{out.get('title') or '（没有标题）'}\n"
            f"{str(out.get('text') or '')[:1200]}"
        )

    @filter.llm_tool(name="browser_screenshot")
    async def tool_screenshot(self, event: AstrMessageEvent, page: str = "", url: str = "") -> str:
        """把一个网页截成图发到当前聊天里。

        想让他们看见页面长什么样时用；给页面编号截当前那个，给网址就截那个网址。

        Args:
            page (string): 页面编号。
            url (string): 网址。

        Returns:
            截图有没有发出去。
        """
        target = url or self._last_url
        try:
            ok = await self._send_shot(event.unified_msg_origin, page, url)
        except Exception as exc:  # noqa: BLE001
            return f"截图失败：{exc}"
        if not ok:
            return "截不出来，页面可能已经关了。"
        logger.info("[browser] 截图发出：%s / %s", page or "-", target)
        return "截图发到群里了。"

    @filter.llm_tool(name="browser_tabs")
    async def tool_tabs(self, event: AstrMessageEvent) -> str:
        """列出自己现在开着哪些页面。

        Returns:
            每个页面的编号、标题和地址。
        """
        try:
            out = await self._post("/tabs", {})
        except Exception as exc:  # noqa: BLE001
            return f"列不出来：{exc}"
        tabs = out.get("tabs") or []
        if not tabs:
            return "现在一个页面都没开。"
        return "\n".join(
            f"{t['page']}｜{t.get('title') or '（没有标题）'}｜{t.get('url')}" for t in tabs
        )

    @filter.llm_tool(name="browser_close")
    async def tool_close(self, event: AstrMessageEvent, page: str) -> str:
        """关掉一个自己开的页面。

        Args:
            page (string): 页面编号。

        Returns:
            关没关掉。
        """
        try:
            out = await self._post("/close", {"page": page})
        except Exception as exc:  # noqa: BLE001
            return f"关不掉：{exc}"
        return "关掉了。" if out.get("ok") else "没这个页面。"

    # ---------- 自动看链接 / 关键词截图 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        msg = (event.message_str or "").strip()
        if not msg or not SHOT_RE.search(msg):
            return
        urls = URL_RE.findall(msg)
        if not urls:
            return
        event.stop_event()
        try:
            ok = await self._send_shot(event.unified_msg_origin, url=urls[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[browser] 截图出错：%s", exc)
            ok = False
        if not ok:
            await self.context.send_message(
                event.unified_msg_origin, MessageChain([Plain("这个页面截不出来，换一个试试")])
            )

    @filter.on_llm_request()
    async def inject_page(self, event: AstrMessageEvent, req) -> None:
        msg = (event.message_str or "").strip()
        if not msg:
            return
        urls = URL_RE.findall(msg)
        if not urls:
            return
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sid = ""
        if sid not in PARENTS and "大肥鱼" not in msg:
            return

        url = urls[0]
        try:
            _, title, text = await self._read(url=url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[browser] 自动读页失败：%s", exc)
            return
        if not text:
            return
        note = (
            f"【你刚自己去打开了这个网页】\n地址：{url}\n标题：{title or '（没有标题）'}\n"
            f"正文摘要：\n{text}\n\n"
            "这些是你自己看到的内容，聊的时候可以直接引用；没有答案就照实说没看到。"
        )
        try:
            req.extra_user_content_parts.append(TextPart(text=note).mark_as_temp())
            logger.info("[browser] 读页注入 %d 字：%s", len(text), url[:60])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[browser] 注入失败：%s", exc)
