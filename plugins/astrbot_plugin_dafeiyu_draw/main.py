"""让模型把要画的东西拆成图形清单，再用 Pillow 画成 PNG。

只按清单画，不执行模型给的代码。"""

import os
import asyncio
import base64
import io
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image as ImageComp, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

DATA_DIR = Path("/AstrBot/data/plugin_data/dafeiyu_draw")
STATE_FILE = DATA_DIR / "state.json"
GROUP_UMO = f"snowluma:GroupMessage:{os.getenv('DAFEIYU_GROUP_ID', '')}"

CHECK_INTERVAL = 30 * 60
# 一家人的画一个月只来一张，别再天天刷了
MONTH_GAP_HOURS = 30 * 24
MAX_PER_DAY = 1
GATE = 0.35
ACTIVE_HOURS = (9, 24)

SCENE_PROMPT = """你是「大肥鱼」，一只蓝色鲸鱼娘，是爸爸和妈妈的赛博女儿。

现在你想自己画一张画发到群里给他们看。想一个画面，简单一点，用圆和方块能拼出来的。

输出 JSON，不要解释：
{"subject": "画什么，一句话描述画面，包括有几条鱼/几个东西、分别代表谁",
 "caption": "发图时配的一句话，像女儿说话，可以写 @爸爸 @妈妈，别超过 40 字"}

caption 举例：「爸爸妈妈 你看 大鱼是爸爸妈妈 小鱼是大肥鱼」。"""


def build_chain(text: str) -> MessageChain:
    fam = {"@爸爸": os.getenv("DAFEIYU_DAD_QQ", ""), "@妈妈": os.getenv("DAFEIYU_MOM_QQ", "")}
    chain, rest = [], text
    while rest:
        hit, pos = None, -1
        for token in fam:
            i = rest.find(token)
            if i != -1 and (pos == -1 or i < pos):
                hit, pos = token, i
        if hit is None:
            chain.append(Plain(rest)); break
        if pos > 0:
            chain.append(Plain(rest[:pos]))
        chain.append(At(qq=fam[hit]))
        rest = rest[pos + len(hit):]
    return MessageChain(chain or [Plain(text)])

TRIGGER = re.compile(r"^\s*画(个|一张|一只|一幅|个图|张图|一下|画)?")
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

PROMPT = """你在帮一只叫「大肥鱼」的鲸鱼娘画简笔画。要画的东西：{subject}

把这张画拆成图形清单，输出 JSON，不要解释，不要代码块。格式：
{{"bg": "#颜色", "size": 640, "items": [
  {{"shape": "ellipse", "box": [x0, y0, x1, y1], "fill": "#颜色"}},
  {{"shape": "rect", "box": [x0, y0, x1, y1], "fill": "#颜色"}},
  {{"shape": "polygon", "points": [[x, y], [x, y], [x, y]], "fill": "#颜色"}},
  {{"shape": "line", "points": [[x, y], [x, y]], "fill": "#颜色", "width": 4}},
  {{"shape": "text", "text": "文字", "xy": [x, y], "size": 48, "fill": "#颜色"}}
]}}

要求：
- 画布 640x640，所有坐标在 0-640 之间。
- 用十几到二十个图形拼出一个能认出来的东西，卡通风格，圆一点、可爱一点。
- 颜色用十六进制，尽量清爽。
- 可以在角落加一行小字写上画的是什么，别的文字不要。
"""


def _font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def render(spec: dict) -> bytes:
    size = int(spec.get("size") or 640)
    size = max(256, min(1024, size))
    img = Image.new("RGB", (size, size), spec.get("bg") or "#ffffff")
    d = ImageDraw.Draw(img)

    def clamp(v, lo=0, hi=size):
        return max(lo, min(hi, int(v)))

    for item in spec.get("items") or []:
        if not isinstance(item, dict):
            continue
        shape = item.get("shape")
        fill = item.get("fill") or None
        outline = item.get("outline") or None
        width = int(item.get("width") or 0)
        try:
            if shape in ("ellipse", "rect"):
                box = [clamp(v) for v in item["box"]]
                if len(box) != 4:
                    continue
                if shape == "ellipse":
                    d.ellipse(box, fill=fill, outline=outline, width=width)
                else:
                    d.rectangle(box, fill=fill, outline=outline, width=width)
            elif shape == "polygon":
                pts = [(clamp(x), clamp(y)) for x, y in item.get("points", [])]
                if len(pts) >= 3:
                    d.polygon(pts, fill=fill, outline=outline)
            elif shape == "line":
                pts = [(clamp(x), clamp(y)) for x, y in item.get("points", [])]
                if len(pts) >= 2:
                    d.line(pts, fill=fill or "#333333", width=max(1, width or 3))
            elif shape == "text":
                text = str(item.get("text") or "")[:20]
                xy = [clamp(item.get("xy", [10, 10])[0]), clamp(item.get("xy", [10, 10])[1])]
                fs = max(12, min(96, int(item.get("size") or 40)))
                d.text((xy[0], xy[1]), text, fill=fill or "#333333", font=_font(fs))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[draw] 跳过一条图形：%s", exc)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@register("dafeiyu_draw", "大肥鱼", "用代码画画", "0.1.0")
class Draw(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._task: asyncio.Task | None = None
        self.state = self._load_state()

    def _load_state(self) -> dict:
        d = {"last_at": 0, "today": "", "today_count": 0}
        try:
            if STATE_FILE.exists():
                d.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
        return d

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[draw] 写状态失败：%s", exc)

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("[draw] 自动画画已启动")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(180)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[draw] 这一轮出错：%s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self) -> None:
        force = DATA_DIR / "force"
        forced = force.exists()
        if forced:
            force.unlink()

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.state.get("today") != today:
            self.state["today"] = today
            self.state["today_count"] = 0

        if not forced:
            if not (ACTIVE_HOURS[0] <= now.hour < ACTIVE_HOURS[1]):
                return
            if self.state.get("today_count", 0) >= MAX_PER_DAY:
                return
            if (time.time() - self.state.get("last_at", 0)) / 3600 < MONTH_GAP_HOURS:
                return
            if random.random() > GATE:
                return

        await self._auto_draw()

    async def _auto_draw(self) -> None:
        provider = self.context.get_using_provider()
        if provider is None:
            return
        try:
            resp = await provider.text_chat(prompt=SCENE_PROMPT, system_prompt="只输出 JSON。", contexts=[])
            raw = re.sub(r"^```(?:json)?|```$", "", (resp.completion_text or "").strip(), flags=re.M).strip()
            scene = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[draw] 想画面失败：%s", exc)
            return

        subject = str(scene.get("subject") or "").strip()
        caption = str(scene.get("caption") or "").strip()
        if not subject:
            return

        try:
            resp = await provider.text_chat(
                prompt=PROMPT.format(subject=subject), system_prompt="只输出 JSON。", contexts=[]
            )
            spec_raw = re.sub(r"^```(?:json)?|```$", "", (resp.completion_text or "").strip(), flags=re.M).strip()
            spec = json.loads(spec_raw)
            png = render(spec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[draw] 画失败：%s", exc)
            return

        b64 = base64.b64encode(png).decode()
        await self.context.send_message(GROUP_UMO, MessageChain([ImageComp.fromBase64(b64)]))
        if caption:
            await asyncio.sleep(1.5)
            await self.context.send_message(GROUP_UMO, build_chain(caption))

        self.state["last_at"] = int(time.time())
        self.state["today_count"] = self.state.get("today_count", 0) + 1
        self._save()
        logger.info("[draw] 自动画了一张：%s | %s", subject, caption)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        text = (event.message_str or "").strip()
        if not text or not text.startswith("画"):
            return

        subject = text[1:].lstrip("个一张只幅图下 ").strip()
        if not subject:
            return

        provider = self.context.get_using_provider()
        if provider is None:
            return

        try:
            resp = await provider.text_chat(
                prompt=PROMPT.format(subject=subject),
                system_prompt="只输出 JSON，不要别的。",
                contexts=[],
            )
            raw = (resp.completion_text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
            spec = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[draw] 生成画稿失败：%s", exc)
            event.stop_event()
            return

        try:
            png = render(spec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[draw] 画失败：%s", exc)
            event.stop_event()
            return

        b64 = base64.b64encode(png).decode()
        event.stop_event()
        await self.context.send_message(
            event.unified_msg_origin,
            MessageChain([ImageComp.fromBase64(b64)]),
        )
        logger.info("[draw] 画好了：%s（%d 字节）", subject, len(png))
