"""白天挑个时间私聊家里人，一天最多两次，晚上十点以后不吵。

先叫一声，再说一句短的，像女儿发微信。"""

import os
import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

DATA_DIR = Path("/AstrBot/data/plugin_data/dafeiyu_dm")
STATE_FILE = DATA_DIR / "state.json"

FAMILY = {
    "爸爸": os.getenv("DAFEIYU_DAD_QQ", ""),
    "妈妈": os.getenv("DAFEIYU_MOM_QQ", ""),
}

CHECK_INTERVAL = 10 * 60   # 十分钟看一次
MIN_GAP_HOURS = 6          # 两条私聊之间至少隔六小时
MAX_PER_DAY = 2
ACTIVE_HOURS = (0, 24)
GATE = 0.45                # 条件都够，再掷一次

PROMPT = """你是「大肥鱼」，一只蓝色鲸鱼娘，是爸爸和妈妈养的赛博女儿。

现在是 {now}，你想给{who}发条消息。像女儿发微信那样，开头叫一声「{who}，」，然后说一句短的。

可以是：
- 问一句吃没吃、在忙什么
- 说一句今天的事
- 撒个娇
- 提醒一件小事

要求：一句到两句，口语，别用书名号，别用 emoji，别提自己是 AI。
如果要发，就输出「发：」加上要说的话。不想发就只输出「不发」。"""


@register("dafeiyu_dm", "大肥鱼", "想跟爸妈单独说句话", "0.1.0")
class Dm(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._task: asyncio.Task | None = None
        self.state = self._load()

    def _load(self) -> dict:
        default = {"last_at": 0, "today": "", "today_count": 0, "last_who": ""}
        try:
            if STATE_FILE.exists():
                default.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dm] 读状态失败：%s", exc)
        return default

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dm] 写状态失败：%s", exc)

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("[dm] 私聊插件已启动")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(120)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[dm] 这一轮出错：%s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self) -> None:
        force = DATA_DIR / "force"
        forced = force.exists()
        if forced:
            force.unlink()

        now = datetime.now()
        if not forced and not (ACTIVE_HOURS[0] <= now.hour < ACTIVE_HOURS[1]):
            return

        today = now.strftime("%Y-%m-%d")
        if self.state.get("today") != today:
            self.state["today"] = today
            self.state["today_count"] = 0

        if not forced:
            if self.state.get("today_count", 0) >= MAX_PER_DAY:
                return
            if (time.time() - self.state.get("last_at", 0)) / 3600 < MIN_GAP_HOURS:
                return
            if random.random() > GATE:
                return

        # 轮着来：爸爸 -> 妈妈 -> 群 -> 爸爸
        order = ["爸爸", "妈妈", "群"]
        last = self.state.get("last_who") or ""
        try:
            who = order[(order.index(last) + 1) % len(order)]
        except ValueError:
            who = "爸爸"

        text = await self._compose(who, now)
        if not text:
            return

        if who == "群":
            umo = f"snowluma:GroupMessage:{os.getenv('DAFEIYU_GROUP_ID', '')}"
        else:
            umo = f"snowluma:FriendMessage:{FAMILY[who]}"

        try:
            ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dm] 发给%s 抛错：%s", who, exc)
            ok = False
        logger.info("[dm] 发给%s：%s（成功=%s）", who, text, ok)
        if not ok:
            logger.warning("[dm] 发给%s 失败，这次不算数", who)
            self.state["last_who"] = who
            self._save()
            return
        if ok:
            self.state.update(
                last_at=int(time.time()),
                last_who=who,
                today_count=self.state.get("today_count", 0) + 1,
            )
            self._save()

    async def _compose(self, who: str, now: datetime) -> str:
        provider = self.context.get_using_provider()
        if provider is None:
            return ""
        resp = await provider.text_chat(
            prompt=PROMPT.format(who=who, now=now.strftime("%H:%M")),
            system_prompt="按指令输出，只回一行。",
            contexts=[],
        )
        raw = (resp.completion_text or "").strip()
        if not raw.startswith("发"):
            return ""
        text = raw.split("：", 1)[-1] if "：" in raw else raw[1:]
        return text.strip()[:200]
