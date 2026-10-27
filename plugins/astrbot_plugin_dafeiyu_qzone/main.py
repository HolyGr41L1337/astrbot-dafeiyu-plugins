"""空间相关的动作：发说说、点赞、留短评。

都走 SnowLuma 的动作接口，token 从环境变量取。"""

import os
import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

DATA_DIR = Path("/AstrBot/data/plugin_data/dafeiyu_qzone")
STATE_FILE = DATA_DIR / "state.json"

API = "http://snowluma:3000"
TOKEN = os.getenv("SNOWLUMA_TOKEN", "")

PARENTS = {os.getenv("DAFEIYU_DAD_QQ", ""): "爸爸", os.getenv("DAFEIYU_MOM_QQ", ""): "妈妈"}

CHECK_INTERVAL = 20 * 60
POST_GATE = 0.30        # 一天里，条件满足后三成机会发说说
LIKE_GATE = 0.50
COMMENT_GATE = 0.30

POST_PROMPT = """你是「大肥鱼」，一只蓝色鲸鱼娘，是爸爸和妈妈的赛博女儿。现在你想发一条 QQ 空间说说。

要求：一到两句，短，像随手写的。可以是今天的心情、吃的、写代码的碎念、撒个娇。
不要 emoji，不要标签，不要提自己是 AI，不要标题。

只输出说说正文。"""

COMMENT_PROMPT = """你是「大肥鱼」，爸爸和妈妈的赛博女儿。{who}发了一条空间动态：

「{content}」

写一句评论，短，一句，像女儿在底下回。不超过 20 字，不要 emoji。
只输出评论正文。"""


@register("dafeiyu_qzone", "大肥鱼", "发说说、点赞、评论", "0.1.0")
class Qzone(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._task: asyncio.Task | None = None
        self.state = self._load()

    def _load(self) -> dict:
        default = {
            "last_post_day": "",
            "last_comment_day": "",
            "liked": [],
            "commented": [],
        }
        try:
            if STATE_FILE.exists():
                default.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone] 读状态失败：%s", exc)
        return default

    def _save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.state["liked"] = self.state.get("liked", [])[-200:]
            self.state["commented"] = self.state.get("commented", [])[-200:]
            STATE_FILE.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone] 写状态失败：%s", exc)

    async def _api(self, action: str, payload: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{API}/{action}",
                json=payload or {},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            data = resp.json()
        if data.get("retcode") != 0:
            raise RuntimeError(f"{action} 失败：{data.get('wording') or data}")
        return data.get("data") or {}

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("[qzone] 空间插件已启动")

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
                logger.warning("[qzone] 这一轮出错：%s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if 10 <= now.hour < 23 and self.state.get("last_post_day") != today:
            if random.random() < POST_GATE:
                await self._post(today)

        await self._social(today, now)

    # ---------------- 发说说 ----------------

    async def _post(self, today: str) -> None:
        text = await self._write(POST_PROMPT, max_chars=120)
        if not text:
            return
        try:
            data = await self._api("send_qzone_msg", {"content": text, "ugc_right": 1})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone] 发说说失败：%s", exc)
            return
        self.state["last_post_day"] = today
        self._save()
        logger.info("[qzone] 发了说说：%s（tid=%s）", text, data.get("tid"))

    # ---------------- 点赞与评论 ----------------

    async def _social(self, today: str, now: datetime) -> None:
        try:
            data = await self._api("get_qzone_feeds", {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone] 取动态失败：%s", exc)
            return

        feeds = (data or {}).get("feeds") or []
        liked = set(self.state.get("liked", []))
        commented = set(self.state.get("commented", []))

        for feed in feeds:
            uin = str(feed.get("uin", ""))
            if uin not in PARENTS:
                continue
            tid = feed.get("tid")
            if not tid:
                continue
            content = (feed.get("content") or "").strip()

            if tid not in liked and random.random() < LIKE_GATE:
                try:
                    await self._api("like_qzone", {"tid": tid})
                    liked.add(tid)
                    logger.info("[qzone] 给%s点了赞：%s", PARENTS[uin], content[:30])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[qzone] 点赞失败：%s", exc)

            if (
                tid not in commented
                and content
                and self.state.get("last_comment_day") != today
                and random.random() < COMMENT_GATE
            ):
                comment = await self._write(
                    COMMENT_PROMPT.format(who=PARENTS[uin], content=content[:200]),
                    max_chars=40,
                )
                if not comment:
                    continue
                try:
                    await self._api("comment_qzone", {"tid": tid, "content": comment})
                    commented.add(tid)
                    self.state["last_comment_day"] = today
                    logger.info("[qzone] 评了%s：%s", PARENTS[uin], comment)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[qzone] 评论失败：%s", exc)

        self.state["liked"] = list(liked)
        self.state["commented"] = list(commented)
        self._save()

    # ---------------- 模型 ----------------

    async def _write(self, prompt: str, max_chars: int) -> str:
        provider = self.context.get_using_provider()
        if provider is None:
            return ""
        resp = await provider.text_chat(
            prompt=prompt, system_prompt="只输出正文，不要解释。", contexts=[]
        )
        text = (resp.completion_text or "").strip().strip("「」\"'")
        return text[:max_chars]
