"""不等人叫，自己找话说的那一层。

维护一小份状态（上句说了啥、隔了多久、现在几点），再让模型按状态说一句，
有时一句话拆两三条发出去。"""

import os
import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

DATA_DIR = Path("/AstrBot/data/plugin_data/dafeiyu_proactive")
STATE_FILE = DATA_DIR / "state.json"

# 爸爸和妈妈的 QQ 号，用来把 @爸爸 / @妈妈 变成真的 @
FAMILY = {
    "@爸爸": os.getenv("DAFEIYU_DAD_QQ", ""),
    "@妈妈": os.getenv("DAFEIYU_MOM_QQ", ""),
}

CHECK_INTERVAL = 10 * 60      # 十分钟看一次
MIN_GAP_HOURS = 2             # 两次主动至少隔两小时
MAX_PER_DAY = 5
QUIET_HOURS = (0, 24)         # 现在全天开放，想收窄就改成比如 (9, 23)

# 加权：安静越久越容易开口
BASE_CHANCE = 0.12
IDLE_BONUS = 0.06             # 每多安静一小时加一点
MAX_CHANCE = 0.75

# 被晾着的时候，把间隔拉长
UNANSWERED_BACKOFF = 1.6


def build_chain(text: str) -> MessageChain:
    """把文本里的 @爸爸 / @妈妈 拆成 At 段，其余当普通文字。"""
    chain = []
    rest = text
    while rest:
        hit, pos = None, -1
        for token in FAMILY:
            i = rest.find(token)
            if i != -1 and (pos == -1 or i < pos):
                hit, pos = token, i
        if hit is None:
            chain.append(Plain(rest))
            break
        if pos > 0:
            chain.append(Plain(rest[:pos]))
        chain.append(At(qq=FAMILY[hit]))
        rest = rest[pos + len(hit):]
    return MessageChain(chain or [Plain(text)])


PROMPT = """（我自己想开口，不是你在跟我说话）

现在 {now}。
上一次我自己开口是 {gap} 前。
群里已经安静了 {idle} 小时。
上次我主动说的是：「{last}」——{answered}。

角度参考（随便挑一个，不要照抄字面）：{angle}

现在按我平时的说话方式，自己说一两句：
- 只能拿真有的东西开口：现在的时间、我真实的状态、刚才真聊过的事。不知道就问。
- 不许编造任何具体的事件、场景、对话或数字。
- 不要问「在干嘛」；不要一口气问几个问题；不要连环追问。
- 不要提「自唤醒 / 系统 / 定时 / 插件」，也不要解释自己为什么突然开口。
- 不是等他们回，只是我自己想说。最多两句。
- 想叫爸妈就写 @爸爸 或 @妈妈。

要发就输出「发：」加上要说的话；不发就只输出「不发」。"""

ANGLES = [
    "说一句我现在有点无聊，想找人说两句话",
    "问一句爸妈吃了没",
    "催一句早点睡",
    "念叨一句好久没听到他俩说话了",
    "说一句我在想刚才那件事",
    "撒个娇，要点存在感",
]


@register("dafeiyu_proactive", "大肥鱼", "自唤醒：按状态自己开口", "0.2.0")
class Proactive(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._task: asyncio.Task | None = None
        self.state = self._load()

    # ---------------- 状态 ----------------

    def _load(self) -> dict:
        default = {
            "umo": "",
            "last_message_at": 0,
            "last_message_text": "",
            "last_proactive_at": 0,
            "last_line": "",
            "last_line_answered": True,
            "unanswered": 0,
            "today": "",
            "today_count": 0,
        }
        try:
            if STATE_FILE.exists():
                default.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[proactive] 读状态失败：%s", exc)
        return default

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[proactive] 写状态失败：%s", exc)

    # ---------------- 记账 ----------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def remember(self, event: AstrMessageEvent) -> None:
        text = (event.message_str or "").strip()
        if not text:
            return
        try:
            sender = event.get_sender_name() or "有人"
        except Exception:  # noqa: BLE001
            sender = "有人"

        # 有人在主动开口之后说话，就算被接了
        if (
            self.state.get("last_proactive_at", 0)
            and self.state.get("last_message_at", 0) <= self.state["last_proactive_at"]
        ):
            self.state["last_line_answered"] = True
            self.state["unanswered"] = 0

        self.state["umo"] = event.unified_msg_origin
        self.state["last_message_at"] = int(time.time())
        self.state["last_message_text"] = f"{sender}：{text[:80]}"
        self._save()

    # ---------------- 循环 ----------------

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("[proactive] 自唤醒已启动，每 %d 分钟看一次", CHECK_INTERVAL // 60)

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(90)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[proactive] 这一轮出错：%s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self) -> None:
        if not self.state.get("umo"):
            logger.warning("[proactive] 还没有群记录，跳过")
            return

        # 手动触发：放一个 force 文件，下一轮无视闸门直接开口一次
        force = DATA_DIR / "force"
        if force.exists():
            force.unlink()
            text = await self._compose(3.0, 3.0)
            if text:
                await self._send(text)
                logger.info("[proactive] 手动触发完成")
            else:
                logger.info("[proactive] 手动触发：模型选择不说")
            return

        now = datetime.now()
        if not (QUIET_HOURS[0] <= now.hour < QUIET_HOURS[1]):
            return

        today = now.strftime("%Y-%m-%d")
        if self.state.get("today") != today:
            self.state["today"] = today
            self.state["today_count"] = 0

        if self.state.get("today_count", 0) >= MAX_PER_DAY:
            return

        now_ts = time.time()
        idle_h = (now_ts - self.state.get("last_message_at", 0)) / 3600
        gap_h = (now_ts - self.state.get("last_proactive_at", 0)) / 3600

        # 被晾着就退一步，别越没人理越来劲
        need_gap = MIN_GAP_HOURS * (UNANSWERED_BACKOFF ** self.state.get("unanswered", 0))
        if gap_h < need_gap:
            return

        # 安静越久，越容易开口
        chance = min(MAX_CHANCE, BASE_CHANCE + IDLE_BONUS * max(0.0, idle_h - MIN_GAP_HOURS))
        if random.random() > chance:
            return

        text = await self._compose(idle_h, gap_h)
        if not text:
            return

        await self._send(text)

    async def _send(self, text: str) -> None:
        # 一个回合拆成 1-3 条，隔几秒发，像连发消息
        parts = [p.strip() for p in text.replace("！", "！\n").replace("？", "？\n").split("\n") if p.strip()]
        if len(parts) > 3:
            parts = ["".join(parts)]
        parts = parts[:3] or [text]

        sent = 0
        for part in parts:
            ok = await self.context.send_message(self.state["umo"], build_chain(part))
            if not ok:
                break
            sent += 1
            if part is not parts[-1]:
                await asyncio.sleep(random.uniform(3, 12))

        if sent:
            self.state["last_proactive_at"] = int(time.time())
            self.state["last_line"] = text[:80]
            self.state["last_line_answered"] = False
            self.state["unanswered"] = self.state.get("unanswered", 0) + 1
            self.state["today_count"] = self.state.get("today_count", 0) + 1
            self._save()
            logger.info("[proactive] 主动说了(%d 条)：%s", sent, text[:60])

    async def _compose(self, idle_h: float, gap_h: float) -> str:
        provider = self.context.get_using_provider()
        if provider is None:
            return ""

        last = self.state.get("last_line") or "（还没开过口）"
        answered = (
            "被接了" if self.state.get("last_line_answered") else "没人接"
        )
        if not self.state.get("last_proactive_at"):
            answered = "（还没开过口）"

        prompt = PROMPT.format(
            now=datetime.now().strftime("%H:%M"),
            gap=f"{round(gap_h)} 小时" if gap_h >= 1 else "不到一小时",
            idle=round(idle_h, 1),
            last=last,
            answered=answered,
            angle=random.choice(ANGLES),
        )
        resp = await provider.text_chat(
            prompt=prompt, system_prompt="按指令输出，只回一行。", contexts=[]
        )
        raw = (resp.completion_text or "").strip()
        if not raw.startswith("发"):
            return ""
        text = raw.split("：", 1)[-1] if "：" in raw else raw[1:]
        return text.strip()[:200]
