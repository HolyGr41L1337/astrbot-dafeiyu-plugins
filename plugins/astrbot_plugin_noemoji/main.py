"""发出去之前把 emoji 剃掉。

人格里写「禁止 emoji」经常压不住，干脆在消息出去前做一次硬过滤。"""

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

# 各种 emoji、变体选择符、零宽连接符、装饰符号
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 表情、旗帜、补充符号
    "\U00002600-\U000027BF"  # 杂项符号与装饰符号
    "\U0001F1E6-\U0001F1FF"  # 区域指示符（国旗）
    "\U00002B00-\U00002BFF"  # 杂项符号和箭头
    "\U0000FE00-\U0000FE0F"  # 变体选择符
    "\U0000200D"             # 零宽连接符
    "\U000020E3"             # 组合用围框键帽
    "]+"
)


# 整行的内心独白：*（……）* 或 （……）
MONOLOGUE_LINE = re.compile(r"^\s*[*_]*\s*[（(][^）)]*[）)]\s*[*_]*\s*$")
# 行尾挂着的括号独白
MONOLOGUE_TAIL = re.compile(r"[\s]*[*_]*\s*[（(][^）)]{1,80}[）)]\s*[*_]*\s*$")


def strip_monologue(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        if MONOLOGUE_LINE.match(line):
            continue
        lines.append(MONOLOGUE_TAIL.sub("", line))
    return "\n".join(lines)


def strip_emoji(text: str) -> str:
    cleaned = EMOJI_RE.sub("", text)
    # 行内多余空格收一下，行首行尾的空格也去掉
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    # 每行行尾的空格和制表符去掉，空行保留
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    return cleaned.strip() if cleaned.strip() else ""


@register("noemoji", "大肥鱼", "发出去前把 emoji 剃掉", "0.1.0")
class NoEmoji(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

    @filter.on_decorating_result(priority=99999)
    async def strip(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if not result or not result.chain:
            return

        new_chain = []
        changed = False
        for comp in result.chain:
            if isinstance(comp, Plain):
                before = comp.text or ""
                after = strip_emoji(strip_monologue(before))
                if after != before:
                    changed = True
                if after:
                    comp.text = after
                    new_chain.append(comp)
            else:
                new_chain.append(comp)

        if changed:
            if new_chain:
                result.chain = new_chain
                logger.info("[noemoji] 已剃掉表情符号")
            else:
                # 整条只剩表情，就别发了
                result.chain = []
                event.stop_event()
                logger.info("[noemoji] 整条都是表情，拦住不发")
