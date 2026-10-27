"""明确要语音的时候，把这一轮的回复整句合成语音发出去。

合成失败就退回文字，不会把话吞掉。参考音频和 TTS 地址走环境变量。"""

import os
import base64
import json
import re
import time
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart

TTS_API = os.getenv("DAFEIYU_TTS_API", "http://host.docker.internal:9880/tts")
DEFAULT_REF = os.getenv("DAFEIYU_TTS_REF", "/path/to/your/ref.wav")
DEFAULT_PROMPT = "什么嘛，这么简单的事情，你居然专门跑来问我。"
EMOTION_FILE = "/AstrBot/data/plugin_data/dafeiyu_split/emotions.json"
SHARED_LOG = Path("/AstrBot/data/plugin_data/shared/recent.jsonl")

# 固定的一段，告诉它自己有语音，别再说「语音没接上」
CAPABILITY = (
    "【你有语音】你不是只能打字：你说的话会被合成成你自己的声音发出去，"
    "刚才那种语音就是你自己发的。爸妈要语音的时候直接说就行，"
    "不许再说「语音还没接上」「本鱼发不了语音」「只能打字」这种话。"
)
NOTE_TTL = 900  # 发完语音后这么久内，下一轮告诉它刚才那条是语音

# 只有爸妈开口才算数，群里别人喊发语音不接
PARENTS = {
    os.getenv("DAFEIYU_DAD_QQ", ""): "爸爸",
    os.getenv("DAFEIYU_MOM_QQ", ""): "妈妈",
}

WANT_RE = re.compile(
    r"(发|来|用|整)?一?[条段句]?语音|语音(说|发|回|来)|说一句|说句话|说两句|"
    r"念一下|念给我|读一下|读给我|唱(一|个|首|段|句)"
)
DENY_RE = re.compile(r"不要语音|别发语音|不用语音|别用语音|别整语音|不用发语音")
WINDOW = 180      # 说了要语音之后，这么多秒内的回复都按语音发
MAX_CHARS = 140   # 太长了合成慢，截断
KO_RE = re.compile(r"```[\s\S]*?```")


def _pick_text(result) -> tuple[str, list]:
    """从回复链里取出要说的话，顺便把 @ 保留下来。"""
    ats = [c for c in result.chain if isinstance(c, At)]
    parts = []
    for comp in result.chain:
        text = getattr(comp, "text", None)
        if text:
            parts.append(str(text))
    raw = " ".join(p for p in parts if p).strip()
    raw = KO_RE.sub("", raw).strip()
    raw = re.sub(r"https?://\S+", "", raw).strip()
    raw = re.sub(r"[*_#>`]+", "", raw).strip()
    return raw, ats


@register("dafeiyu_voice", "大肥鱼", "爸妈要语音就直接发语音", "0.1.0")
class DafeiyuVoice(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        # umo -> 什么时候有人要过语音
        self._want: dict[str, float] = {}
        # umo -> (时间, 刚才用语音说了什么)
        self._last: dict[str, tuple[float, str]] = {}

    def _log_own_line(self, umo: str, text: str) -> None:
        """把这条语音也记进共通流水账，不然它自己都忘了说过啥。"""
        try:
            rec = {
                "ts": int(time.time()),
                "umo": umo,
                "speaker": "大肥鱼",
                "text": text[:300],
            }
            SHARED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with SHARED_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[voice] 记进流水账失败：%s", exc)

    @filter.on_llm_request()
    async def remind(self, event: AstrMessageEvent, req) -> None:
        """让它知道：自己会发语音，刚才那条就是语音。"""
        parts = [CAPABILITY]
        last = self._last.get(event.unified_msg_origin)
        if last and time.time() - last[0] <= NOTE_TTL:
            stamp = time.strftime("%H:%M", time.localtime(last[0]))
            parts.append(
                f"【你 {stamp} 发的那条是语音】语音内容：「{last[1][:150]}」。"
                "是你亲口说的，不是别人替你发的。"
            )
            self._last.pop(event.unified_msg_origin, None)
        try:
            req.extra_user_content_parts.append(TextPart(text="\n".join(parts)).mark_as_temp())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[voice] 注入语音提醒失败：%s", exc)

    def _ref(self) -> tuple[str, str]:
        """挑一条参考音频，尽量用平静的那条。"""
        try:
            with open(EMOTION_FILE, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("neutral") or []
            if items:
                return items[0]["path"], items[0]["text"]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[voice] 读情绪参考失败，用默认：%s", exc)
        return DEFAULT_REF, DEFAULT_PROMPT

    async def _tts(self, text: str) -> str:
        ref_path, ref_text = self._ref()
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                r = await client.post(
                    TTS_API,
                    json={
                        "text": text,
                        "text_lang": "zh",
                        "ref_audio_path": ref_path,
                        "prompt_text": ref_text,
                        "prompt_lang": "zh",
                        "text_split_method": "cut5",
                        "media_type": "wav",
                        "streaming_mode": False,
                    },
                )
            if r.status_code != 200:
                logger.warning("[voice] 合成失败 %s：%s", r.status_code, r.text[:120])
                return ""
            return base64.b64encode(r.content).decode()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[voice] 合成出错：%s", exc)
            return ""

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def mark(self, event: AstrMessageEvent) -> None:
        """记下：这个会话里爸妈要过语音。"""
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sid = ""
        if sid not in PARENTS:
            return
        msg = (event.message_str or "").strip()
        if not msg:
            return
        if DENY_RE.search(msg):
            self._want.pop(event.unified_msg_origin, None)
            return
        if WANT_RE.search(msg):
            self._want[event.unified_msg_origin] = time.time()
            logger.info("[voice] %s 要语音：%s", PARENTS[sid], msg[:40])

    @filter.on_decorating_result(priority=0)
    async def send_voice(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        asked = self._want.get(umo)
        if not asked or time.time() - asked > WINDOW:
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        text, ats = _pick_text(result)
        if not text:
            return

        # 要过一次就兑现一次，别把后面所有回复都变成语音
        self._want.pop(umo, None)
        wav = await self._tts(text[:MAX_CHARS])
        if not wav:
            logger.warning("[voice] 没合成出来，这一句还是发文字")
            return

        try:
            event.stop_event()
            result.chain = ats + [Record.fromBase64(wav)]
            self._last[umo] = (time.time(), text)
            self._log_own_line(umo, text)
            logger.info("[voice] 已改成语音发出：%s", text[:30])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[voice] 换成语音失败，保留文字：%s", exc)
