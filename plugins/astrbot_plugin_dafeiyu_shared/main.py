"""群聊和私聊共用一本流水账。

最近 500 条滚动保存，每次请求挑一段塞进上下文，省得两边各失忆一次。
另外支持「记住：」开头的消息写进一份全局记忆。"""

import os
import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart

DATA_DIR = Path("/AstrBot/data/plugin_data/shared")
LOG_FILE = DATA_DIR / "recent.jsonl"
GLOBAL_FILE = DATA_DIR / "global_memory.md"

KEEP_RECORDS = 500        # 本地滚动保留条数
WINDOW_HOURS = 36         # 注入窗口
INJECT_LIMIT = 40         # 一次最多注入多少条，防吃 token
SAME_CHANNEL_LIMIT = 15   # 当前这个会话最近的几句

# 触发写进全局记忆的开头
REMEMBER_RE = re.compile(r"^\s*(记住了吗|记住[：:]|要记住|记一下)")

# QQ 号 → 叫法。昵称拿不到的时候靠这个兜底，别让妈妈变成「有人」
WHO = {
    os.getenv("DAFEIYU_DAD_QQ", ""): "爸爸",
    os.getenv("DAFEIYU_MOM_QQ", ""): "妈妈",
}
SELF_LABEL = "你自己（大肥鱼）"
SELF_RULE = (
    "标着「你自己（大肥鱼）」的是你自己说过的话，"
    "不是爸爸、妈妈或其他人说的；别把两边搞混，拿不准就说记不清了。"
)


def _label(rec: dict) -> str:
    """把流水账里的 speaker 翻成人话，重点是把「大肥鱼」标成「你自己」。"""
    speaker = (rec.get("speaker") or "").strip()
    if speaker in ("大肥鱼", "bot"):
        return SELF_LABEL
    if speaker in ("", "有人"):
        return "有人（昵称没拿到）"
    return speaker


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _append_record(umo: str, speaker: str, text: str) -> None:
    _ensure_dir()
    record = {
        "ts": int(time.time()),
        "umo": umo,
        "speaker": speaker,
        "text": text[:300],
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _trim()


def _trim() -> None:
    """只留最近 KEEP_RECORDS 条。"""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) <= KEEP_RECORDS:
            return
        LOG_FILE.write_text(
            "\n".join(lines[-KEEP_RECORDS:]) + "\n", encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[shared] 裁剪失败：%s", exc)


def _recent(since_ts: int) -> list[dict]:
    try:
        out = []
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) >= since_ts:
                out.append(rec)
        return out
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[shared] 读流水账失败：%s", exc)
        return []


def _remember(text: str) -> None:
    _ensure_dir()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with GLOBAL_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- [{stamp}] {text}\n")


def _global_memory() -> str:
    try:
        return GLOBAL_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("[shared] 读全局记忆失败：%s", exc)
        return ""


def _channel(umo: str) -> str:
    if "GroupMessage" in umo:
        return "群"
    if "FriendMessage" in umo:
        return "私聊"
    return "别处"


@register("dafeiyu_shared", "大肥鱼", "群聊私聊共通记忆", "0.1.0")
class Shared(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

    # ---------- 记录 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def record_user(self, event: AstrMessageEvent) -> None:
        text = (event.message_str or "").strip()
        if not text:
            return
        try:
            sender = event.get_sender_name() or "有人"
        except Exception:  # noqa: BLE001
            sender = "有人"

        # 按 QQ 号认人：爸爸、妈妈一律写成「爸爸」「妈妈」，不跟昵称走
        sid = ""
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sid = ""
        sender = WHO.get(sid) or sender

        _append_record(event.unified_msg_origin, sender, text)

        # 「记住了吗」这类，抄进全局记忆
        if REMEMBER_RE.match(text):
            _remember(f"{sender}：{text}")
            logger.info("[shared] 记进全局记忆：%s", text[:40])

    @filter.on_decorating_result(priority=1)
    async def record_bot(self, event: AstrMessageEvent) -> None:
        try:
            result = event.get_result()
            if not result or not result.chain:
                return
            parts = []
            for comp in result.chain:
                text = getattr(comp, "text", None)
                if text:
                    parts.append(text)
            reply = " ".join(p for p in parts if p).strip()
            if reply:
                _append_record(event.unified_msg_origin, "大肥鱼", reply)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[shared] 记自己的话失败：%s", exc)

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        umo = event.unified_msg_origin
        since = int(time.time()) - WINDOW_HOURS * 3600
        all_recent = _recent(since)
        same = [r for r in all_recent if r.get("umo") == umo][-SAME_CHANNEL_LIMIT:]
        records = [r for r in all_recent if r.get("umo") != umo][-INJECT_LIMIT:]

        blocks = []

        global_mem = _global_memory()
        if global_mem:
            blocks.append(f"【你记下的事】\n{global_mem}")

        if same:
            lines = []
            for rec in same:
                stamp = time.strftime("%H:%M", time.localtime(rec["ts"]))
                lines.append(f"{stamp} {_label(rec)}：{rec['text']}")
            blocks.append(
                "【这个会话刚才说的话】\n" + SELF_RULE + "\n" + "\n".join(lines)
            )

        if records:
            lines = []
            for rec in records:
                stamp = time.strftime("%m-%d %H:%M", time.localtime(rec["ts"]))
                lines.append(
                    f"{stamp} [{_channel(rec['umo'])}] {_label(rec)}：{rec['text']}"
                )
            blocks.append(
                "【另一边最近说过的话】\n" + SELF_RULE + "\n" + "\n".join(lines)
            )

        if not blocks:
            return

        payload = "\n\n".join(blocks)
        for part in req.extra_user_content_parts:
            if "【另一边最近说过的话】" in getattr(part, "text", ""):
                return

        req.extra_user_content_parts.append(TextPart(text=payload).mark_as_temp())
        logger.info(
            "[shared] 注入 本会话 %d 条 / 别处 %d 条，全局记忆 %d 字，合计 %d 字",
            len(same), len(records), len(global_mem), len(payload),
        )
