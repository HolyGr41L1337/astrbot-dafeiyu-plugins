"""点歌就真唱一段。

把歌名交给宿主上的唱歌服务（找歌、切段、分离人声、RVC 转声、混伴奏），
拿回来的 wav 当语音发进群。唱不了就直说。"""

import os
import asyncio
import base64
import json
import re

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

SING_API = os.getenv("DAFEIYU_SING_API", "http://host.docker.internal:9881/sing")
PARENTS = {os.getenv("DAFEIYU_DAD_QQ", ""): "爸爸", os.getenv("DAFEIYU_MOM_QQ", ""): "妈妈"}
SING_RE = re.compile(r"唱(一?首|一?段|个|歌|首歌|一句)|来首|点歌|唱歌|唱唱")
MAX_WAIT = 900

EXTRACT_PROMPT = """从这句话里找出要点唱的歌：

「{msg}」

只输出一个 JSON，不要解释：
{{"song": "歌名", "artist": "歌手，没提到就留空"}}

如果里面没有具体的歌名，song 留空字符串。"""


@register("dafeiyu_sing", "大肥鱼", "爸妈点歌她就真唱", "0.1.0")
class DafeiyuSing(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._busy: set[str] = set()

    async def _extract(self, msg: str) -> tuple[str, str]:
        provider = self.context.get_using_provider()
        if provider is None:
            return "", ""
        try:
            resp = await provider.text_chat(
                prompt=EXTRACT_PROMPT.format(msg=msg[:300]),
                system_prompt="只输出 JSON。",
                contexts=[],
            )
            raw = (resp.completion_text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
            data = json.loads(raw)
            song = str(data.get("song") or "").strip()
            artist = str(data.get("artist") or "").strip()
            return song, artist
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sing] 取歌名失败：%s", exc)
            return "", ""

    async def _sing_and_send(self, umo: str, query: str, label: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=MAX_WAIT) as client:
                r = await client.post(SING_API, json={"query": query})
            if r.status_code != 200:
                detail = ""
                try:
                    detail = str(json.loads(r.text).get("error") or "")
                except Exception:  # noqa: BLE001
                    detail = r.text[:100]
                logger.warning("[sing] 服务返回 %s：%s", r.status_code, detail)
                await self.context.send_message(
                    umo, MessageChain([Plain(f"唱不了，{detail or '服务那边出问题了'}")])
                )
                return
            b64 = base64.b64encode(r.content).decode()
            await self.context.send_message(umo, MessageChain([Record.fromBase64(b64)]))
            logger.info("[sing] 唱完发出去：%s（%d 字节）", label, len(r.content))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sing] 唱歌流程失败：%s", exc)
            await self.context.send_message(
                umo, MessageChain([Plain(f"唱到一半出错了：{str(exc)[:80]}")])
            )
        finally:
            self._busy.discard(umo)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def listen(self, event: AstrMessageEvent) -> None:
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sid = ""
        if sid not in PARENTS:
            return
        msg = (event.message_str or "").strip()
        if not msg or not SING_RE.search(msg):
            return

        umo = event.unified_msg_origin
        if umo in self._busy:
            event.stop_event()
            await self.context.send_message(umo, MessageChain([Plain("别急，上一首还没唱完呢")]))
            return

        song, artist = await self._extract(msg)
        if not song:
            event.stop_event()
            await self.context.send_message(
                umo, MessageChain([Plain("唱什么呀，给本鱼个歌名呗")])
            )
            return

        query = f"{song} {artist}".strip()
        label = f"《{song}》" + (f" - {artist}" if artist else "")
        event.stop_event()
        self._busy.add(umo)
        await self.context.send_message(
            umo,
            MessageChain([Plain(f"行，{PARENTS[sid]}点的是{label}，本鱼去唱，等我一会儿")]),
        )
        asyncio.create_task(self._sing_and_send(umo, query, label))
