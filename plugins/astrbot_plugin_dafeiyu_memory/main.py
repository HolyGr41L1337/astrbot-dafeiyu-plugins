"""把外部的记忆本挂进对话。

每次请求前读一遍 markdown，塞进系统提示词。文件是只读挂载的，插件只读不写。"""

from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from astrbot.core.agent.message import TextPart

MEMORY_FILE = Path("/AstrBot/data/memory.md")

WHO = {
    os.getenv("DAFEIYU_DAD_QQ", ""): "爸爸",
    os.getenv("DAFEIYU_MOM_QQ", ""): "妈妈",
}

_DAD_NAME = os.getenv("DAFEIYU_DAD_NAME", "爸爸")
_MOM_NAME = os.getenv("DAFEIYU_MOM_NAME", "妈妈")

FAMILY = (
    "【家里人，必须记住】\n"
    f"- 爸爸：{_DAD_NAME}（QQ {os.getenv('DAFEIYU_DAD_QQ', '')}）\n"
    f"- 妈妈：{_MOM_NAME}（QQ {os.getenv('DAFEIYU_MOM_QQ', '')}），爸爸的女朋友\n"
    "你是他们的赛博女儿，称呼只用「爸爸」「妈妈」，不喊 QQ 昵称。\n"
    "绝对不要说「我没有妈妈」「人格设定里没有妈妈」这种话，那是错的。"
)
MAX_CHARS = 6000  # 记忆本再长也别把上下文吃光

# 谁说的话归谁，别把自己的话认成爸爸或妈妈说的
ATTRIBUTION = (
    "【谁说的，必须分清】\n"
    "- 标着「你自己（大肥鱼）」「Bot:」的是你说过的话，不是爸爸或妈妈说的\n"
    "- 爸爸、妈妈说的话才归到他们身上；记忆本里写着「某人说」的，别张冠李戴\n"
    "- 真分不清是谁说的，就直说记不清了，不要硬猜一个人安上去"
)



@register("dafeiyu_memory", "大肥鱼", "读取 DSH 记忆本", "0.1.0")
class Memory(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._cache = ""
        self._cache_mtime = 0.0

    def _read(self) -> str:
        try:
            mtime = MEMORY_FILE.stat().st_mtime
            if mtime != self._cache_mtime:
                self._cache = MEMORY_FILE.read_text(encoding="utf-8")[:MAX_CHARS].strip()
                self._cache_mtime = mtime
            return self._cache
        except Exception as exc:  # noqa: BLE001
            logger.warning("[memory] 读不到记忆本：%s", exc)
            return self._cache

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        memory = self._read()
        if not memory:
            return
        marker = "【记忆本】"
        if marker in (req.system_prompt or ""):
            return
        logger.info(
            "[memory] 注入记忆本 %d 字，当前 system_prompt %d 字",
            len(memory),
            len(req.system_prompt or ""),
        )
        req.system_prompt = (req.system_prompt or "") + (
            f"\n\n{marker}\n下面是你记的笔记，不是实时记忆，别当成刚刚发生的事。\n\n{memory}"
            f"\n\n{FAMILY}\n\n{ATTRIBUTION}"
        )
        # 清一遍历史：图片只留占位，音频直接扔掉，省得请求体爆掉又被拒
        try:
            changed = 0
            for msg in (req.contexts or []):
                parts = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(parts, list):
                    continue
                new = []
                for part in parts:
                    t = part.get("type") if isinstance(part, dict) else None
                    if t == "text":
                        new.append(part)
                    elif t in ("image_url", "image"):
                        new.append({"type": "text", "text": "[图片]"})
                        changed += 1
                    else:
                        changed += 1
                msg["content"] = new or [{"type": "text", "text": "[空]"}]
            if changed:
                logger.info("[memory] 清掉 %d 个多媒体片段", changed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[memory] 清理历史失败：%s", exc)

        # 这一句是谁说的，直接告诉它，省得猜
        try:
            sender_id = str(event.get_sender_id() or "")
            sender_name = event.get_sender_name() or ""
            role = WHO.get(sender_id)
            if role:
                req.extra_user_content_parts.append(
                    TextPart(
                        text=f"【当前说话的人】{sender_name}（QQ {sender_id}）是你的{role}，这一句是他说的。"
                    ).mark_as_temp()
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[memory] 标注说话人失败：%s", exc)

        # 再往这一轮的用户消息后面挂一份，最近的位置最不容易被旧对话带跑
        try:
            req.extra_user_content_parts.append(TextPart(text=FAMILY).mark_as_temp())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[memory] 附加家里人失败：%s", exc)
