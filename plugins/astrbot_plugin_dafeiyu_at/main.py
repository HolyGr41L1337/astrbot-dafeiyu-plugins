"""群里说话时把「爸爸，」「妈妈，」这种开头换成真 @。

只在群聊生效，私聊不动，剩下的字原样发。"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register

# 称呼 -> QQ 号
CALLS = {
    "@妈妈": os.getenv("DAFEIYU_MOM_QQ", ""),
    "@爸爸": os.getenv("DAFEIYU_DAD_QQ", ""),
}


def rebuild(text: str) -> list | None:
    """把文本里的 @妈妈 / @爸爸 拆成 At 段，没有就返回 None。"""
    pieces: list = []
    rest = text
    while rest:
        hit, pos = None, -1
        for token in CALLS:
            i = rest.find(token)
            if i != -1 and (pos == -1 or i < pos):
                hit, pos = token, i
        if hit is None:
            pieces.append(Plain(rest))
            break
        if pos > 0:
            pieces.append(Plain(rest[:pos]))
        pieces.append(At(qq=CALLS[hit]))
        rest = rest[pos + len(hit):]
    return pieces or None


@register("dafeiyu_at", "大肥鱼", "把称呼换成真的 @", "0.1.0")
class Address(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

    @filter.on_decorating_result(priority=99998)
    async def convert(self, event: AstrMessageEvent) -> None:
        if event.is_private_chat():
            return
        result = event.get_result()
        if not result or not result.chain:
            return

        new_chain: list = []
        hit = False
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                parts = rebuild(comp.text)
                if parts:
                    hit = True
                    new_chain.extend(parts)
                    continue
            new_chain.append(comp)

        if hit:
            result.chain = new_chain
            logger.info("[at] 把称呼换成了真的 @")
