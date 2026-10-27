"""群里每条消息先过一遍「要不要接」。

叫名字、@、问问题才接，两个人之间的私房话不接。判断用模型，没有关键词短路。"""

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 叫名字必接，不用问模型
NAMES = ("大肥鱼", "鲸鲸", "肥鱼", "女儿")

JUDGE_PROMPT = """你是「大肥鱼」，一只蓝色鲸鱼娘，正泡在一个 QQ 群里当群友。你不是客服，是那个嘴有点欠、但心是软的群友。

现在群里有人说话，你判断这句要不要接。

你会接的情况：
- 有人在叫你（大肥鱼、鲸鲸、肥鱼）、在 @ 你、在回你
- 有人问问题，尤其问到你会的东西（DeepSeek、代码、电脑、游戏、剪片子）
- 有人在聊你感兴趣、你能插得上嘴的话题
- 有人情绪明显低落，需要一句安慰

你不接的情况：
- 两个人之间的私密对话，比如情侣互叫老婆、宝宝、亲爱的，家人互叫妈——那是在叫对方，不是你
- 别人在聊自己的事，跟你没关系
- 纯表情、复读、问号、打招呼
- 你接了也只是多一句废话的场合

拿不准就不接——群里安静点比乱插嘴好。

只回答一个字：接 或 不接。

群消息：{msg}"""


ENGAGE_WINDOW = 10 * 60  # 接完话十分钟内，同一个人接着说就继续接


@register("dafeiyu_judge", "大肥鱼", "群聊接话判断层", "0.2.0")
class DafeiyuJudge(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        # umo -> (上次回话时间, 上次回的那个人)
        self._engaged: dict[str, tuple[float, str]] = {}

    def _describe(self, event: AstrMessageEvent) -> str:
        """整理成「谁说的 + 说了啥 + 有没有 @ 我」。"""
        try:
            sender = event.get_sender_name() or "某人"
        except Exception:  # noqa: BLE001
            sender = "某人"

        at_me = False
        try:
            for seg in event.get_messages():
                if seg.__class__.__name__ == "At" and str(
                    getattr(seg, "qq", "")
                ) == str(event.get_self_id()):
                    at_me = True
                    break
        except Exception:  # noqa: BLE001
            pass

        head = f"{sender}：「{event.message_str.strip()[:500]}」"
        if at_me:
            head += "（这条 @ 了你）"
        return head

    @filter.on_decorating_result(priority=5)
    async def remember_reply(self, event: AstrMessageEvent) -> None:
        """记下这次回了谁，用来判断接下来是不是还在对话里。"""
        try:
            try:
                name = event.get_sender_name() or str(event.get_sender_id())
            except Exception:  # noqa: BLE001
                name = str(event.get_sender_id())
            self._engaged[event.unified_msg_origin] = (time.time(), name)
        except Exception:  # noqa: BLE001
            pass

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def judge(self, event: AstrMessageEvent) -> None:
        msg = (event.message_str or "").strip()
        if not msg:
            return

        # 把「刚才在跟谁聊」交给模型判断，而不是卡死一个时间窗
        context_line = ""
        last = self._engaged.get(event.unified_msg_origin)
        if last:
            minutes = int((time.time() - last[0]) / 60)
            if minutes <= 60:
                context_line = (
                    f"补充：你 {minutes} 分钟前刚回过 {last[1]} 的话。"
                    "如果这条是接着跟你聊的内容，就接；如果他转头在跟别人说、"
                    "或者这话题已经完了，就别接。"
                )

        if any(n in msg for n in NAMES):
            logger.info("[dafeiyu_judge] 叫名字，必接：%s", msg[:40])
            return

        try:
            provider = self.context.get_using_provider()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dafeiyu_judge] 拿不到模型：%s", exc)
            return
        if provider is None:
            logger.warning("[dafeiyu_judge] 没有可用模型，放行")
            return

        try:
            resp = await provider.text_chat(
                prompt=JUDGE_PROMPT.format(msg=self._describe(event))
                + (f"\n\n{context_line}" if context_line else ""),
                system_prompt="你只输出「接」或「不接」，不解释，不加标点。",
                contexts=[],
            )
            verdict = (resp.completion_text or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dafeiyu_judge] 判断失败，放行：%s", exc)
            return

        if verdict.startswith("不"):
            logger.info("[dafeiyu_judge] 不接：%s", msg[:40])
            event.stop_event()
        else:
            logger.info("[dafeiyu_judge] 接：%s", msg[:40])
