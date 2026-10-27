"""把一段回复拆成几条发，顺便决定哪句走语音。

语音用本机 GPT-SoVITS 合成，按情绪换参考音频。"""

import asyncio
import base64
import json
import random
import re

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

MAX_CHARS = 120
MAX_PARTS = 5
MIN_GAP, MAX_GAP = 0.8, 2.2

TTS_API = os.getenv("DAFEIYU_TTS_API", "http://host.docker.internal:9880/tts")
TTS_REF = os.getenv("DAFEIYU_TTS_REF", "/path/to/your/ref.wav")
TTS_PROMPT = "什么嘛，这么简单的事情，你居然专门跑来问我。"
EMOTION_FILE = "/AstrBot/data/plugin_data/dafeiyu_split/emotions.json"

EMOTION_MAP = {
    "平静": "neutral",
    "开心": "surprise",
    "撒娇": "neutral",
    "难过": "sad",
    "生气": "anger",
    "惊讶": "surprise",
    "紧张": "nervous",
    "害怕": "fear",
}

SENTENCE = re.compile(r"(?<=[。！？!?，,；;])")
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000200D\U000020E3"
    "]+"
)

VOICE_JUDGE_PROMPT = """你是「大肥鱼」，一只蓝色鲸鱼娘，正在跟爸爸和妈妈聊 QQ。

下面是要发的几句话，判断每一句该用语音还是文字，以及语音时的情绪。

用语音：撒娇、道晚安、安慰人、短短一句有情绪的、想让他听见语气的话。
用文字：技术内容、代码、链接、信息量大、需要反复看的、正经回答。

只输出一个 JSON 数组，数量与句子一致，不要解释：
[{"mode":"语音","emotion":"撒娇"},{"mode":"文字"}]

情绪从「平静、开心、撒娇、难过、生气、惊讶、紧张、害怕」里挑一个。

句子：
{lines}"""


def clean(text: str) -> str:
    t = EMOJI_RE.sub("", text)
    t = t.replace("。", " ").replace("，", " ").replace(",", " ").replace("、", " ")
    t = t.replace("；", " ").replace(";", " ")
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = "\n".join(line.strip() for line in t.split("\n"))
    return t.strip()


def split_text(text: str) -> list[str]:
    text = text.strip()
    if not text or len(text) > MAX_CHARS:
        return []
    if "```" in text or "http" in text:
        return []

    parts = [p.strip() for p in SENTENCE.split(text) if p.strip()]
    merged: list[str] = []
    for p in parts:
        if merged and len(p) <= 6:
            merged[-1] += p
        else:
            merged.append(p)

    if len(merged) < 2:
        return []
    if len(merged) > MAX_PARTS:
        merged = merged[: MAX_PARTS - 1] + ["".join(merged[MAX_PARTS - 1:])]
    return merged


@register("dafeiyu_split", "大肥鱼", "一句话一条发，语音文字自己判断", "0.2.0")
class Split(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

    @filter.on_decorating_result(priority=2)
    async def split(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if not result or not result.chain:
            return

        texts, ats = [], []
        for comp in result.chain:
            if isinstance(comp, At):
                ats.append(comp)
            elif isinstance(comp, Plain):
                texts.append(comp.text or "")
            else:
                return

        joined = "".join(texts).strip()
        parts = split_text(joined) if joined else []

        if not parts:
            cleaned = clean(joined)
            if cleaned and cleaned != joined:
                result.chain = ats + [Plain(cleaned)]
            return

        parts = [p for p in (clean(x) for x in parts) if p]
        if not parts:
            return

        umo = event.unified_msg_origin
        plans = await self._decide(parts)
        logger.info("[split] %d 条，形态：%s", len(parts), [p[0] for p in plans])

        event.stop_event()
        result.chain = []

        async def send_all() -> None:
            for idx, part in enumerate(parts):
                if idx:
                    await asyncio.sleep(random.uniform(MIN_GAP, MAX_GAP))
                mode, emotion = plans[idx] if idx < len(plans) else ("文字", "平静")
                prefix = ats if idx == 0 else []
                if mode == "语音":
                    wav = await self._tts(part, emotion)
                    if wav:
                        try:
                            await self.context.send_message(
                                umo, MessageChain([Record.fromBase64(wav)])
                            )
                            continue
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("[split] 语音发送失败，改发文字：%s", exc)
                try:
                    await self.context.send_message(
                        umo, MessageChain(prefix + [Plain(part)])
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[split] 发送失败：%s", exc)
                    return

        asyncio.create_task(send_all())

    async def _decide(self, parts: list[str]) -> list[tuple[str, str]]:
        provider = self.context.get_using_provider()
        if provider is None:
            return [("文字", "平静")] * len(parts)

        lines = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(parts))
        try:
            resp = await provider.text_chat(
                # 提示词里有示例 JSON 的花括号，用 format 会当场 KeyError，只能替换
                prompt=VOICE_JUDGE_PROMPT.replace("{lines}", lines),
                system_prompt="只输出 JSON 数组。",
                contexts=[],
            )
            raw = re.sub(r"^```(?:json)?|```$", "", (resp.completion_text or "").strip(), flags=re.M).strip()
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError("不是数组")
            out: list[tuple[str, str]] = []
            for x in items:
                if isinstance(x, dict):
                    mode = "语音" if str(x.get("mode", "")).strip() == "语音" else "文字"
                    out.append((mode, str(x.get("emotion") or "平静").strip()))
                else:
                    out.append(("文字", "平静"))
            while len(out) < len(parts):
                out.append(("文字", "平静"))
            return out[: len(parts)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[split] 形态判断失败，全走文字：%s", exc)
            return [("文字", "平静")] * len(parts)

    def _ref_for(self, emotion: str) -> tuple[str, str]:
        try:
            data = json.loads(open(EMOTION_FILE, encoding="utf-8").read())
            style = EMOTION_MAP.get(emotion, "neutral")
            items = data.get(style) or data.get("neutral") or []
            if items:
                item = random.choice(items)
                return item["path"], item["text"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[split] 取情绪参考失败：%s", exc)
        return TTS_REF, TTS_PROMPT

    async def _tts(self, text: str, emotion: str = "平静") -> str:
        ref_path, ref_text = self._ref_for(emotion)
        try:
            async with httpx.AsyncClient(timeout=120) as client:
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
                logger.warning("[split] 合成失败：%s", r.text[:120])
                return ""
            return base64.b64encode(r.content).decode()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[split] 合成出错：%s", exc)
            return ""
