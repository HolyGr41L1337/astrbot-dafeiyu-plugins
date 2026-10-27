"""把自己身上装了哪些插件、能干什么写进系统提示。

插件有增减，过几分钟这份清单会自己跟着变。"""

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart

# 这些话是给人的解释，不跟着插件描述走
CAPS = (
    "【你会什么（这是真的，别否认）】\n"
    "- 发语音：你的话可以合成成你自己的声音发出去\n"
    "- 浏览器：你有自己的浏览器，能随便用：开页、读正文、点链接、列页面、关页、截图发群里"
    "（browser_open / browser_read / browser_click / browser_tabs / browser_close / browser_screenshot），"
    "有人丢链接你还会自动先看一遍；想查东西自己开页就行，不用等谁批准\n"
    "- 唱歌：爸妈点歌，你能唱一段发出来\n"
    "- 画画：说「画个什么」你会画简笔画；一家人的画一个月自己画一张\n"
    "- 记忆：群聊和私聊的话会记进流水账，另有记忆本记着爸爸、妈妈和一起做过的事"
)

REFRESH = 300  # 五分钟扫一次


@register("dafeiyu_skills", "大肥鱼", "让她知道自己有哪些技能", "0.1.0")
class DafeiyuSkills(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._text = ""
        self._at = 0.0

    def _build(self) -> str:
        lines = [CAPS, "", "【身上装着的插件】"]
        try:
            stars = self.context.get_all_stars()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[skills] 取插件列表失败：%s", exc)
            return CAPS

        seen = set()
        for star in stars:
            name = (getattr(star, "display_name", "") or getattr(star, "name", "") or "").strip()
            if not name or name in seen:
                continue
            if getattr(star, "reserved", False):
                continue
            seen.add(name)
            desc = (getattr(star, "short_desc", "") or getattr(star, "desc", "") or "").strip()
            desc = desc.replace("\n", " ")
            if len(desc) > 60:
                desc = desc[:60] + "…"
            lines.append(f"- {name}：{desc}" if desc else f"- {name}")
        lines.append("")
        lines.append(
            "上面这些是你现在真能做的事。有人问「你会不会 xx」，先看这份清单再回答，"
            "别拿「我只是个语言模型」「我只能打字」糊弄人。"
        )
        return "\n".join(lines)

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        now = time.time()
        if not self._text or now - self._at > REFRESH:
            self._text = self._build()
            self._at = now
            logger.info("[skills] 刷新技能清单：%d 字", len(self._text))
        try:
            req.extra_user_content_parts.append(TextPart(text=self._text).mark_as_temp())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[skills] 注入失败：%s", exc)
