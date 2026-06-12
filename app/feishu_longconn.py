"""飞书 WebSocket 长连接入口。

行为：
  1. 收到 @机器人 / 单聊文本消息 → 立即给原消息加表情回应（"GET"），让用户知道收到
  2. 在独立 worker 线程把消息送给 vm_agent.agent_invoke()
  3. 拿到回复后，**回复**原消息（保留上下文链路），群聊里带上 `<at user_id="xxx">` @ 发送者
  4. message_id 去重（10 分钟 TTL）防止飞书重投
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from app.agent.vm_agent import agent_invoke
from app.config import get_settings
from app.observability.audit import audit_log, setup_logging
from app.services.feishu_client import FeishuClient

logger = logging.getLogger(__name__)


# 并发处理的飞书消息数。配合 skill 已改为 asyncio.to_thread 非阻塞执行，
# 这里提高到 8，给同时 @ 的突发请求留余量。
_WORKER_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="feishu-agent")


# ── 消息去重 ─────────────────────────────────────────
_SEEN_LOCK = threading.Lock()
_SEEN: dict[str, float] = {}
_SEEN_TTL = 600


def _is_duplicate(message_id: str) -> bool:
    key = (message_id or "").strip()
    if not key:
        return False
    now = time.time()
    with _SEEN_LOCK:
        for k in [k for k, exp in _SEEN.items() if exp <= now]:
            _SEEN.pop(k, None)
        if key in _SEEN:
            return True
        _SEEN[key] = now + _SEEN_TTL
    return False


# ── 工具函数 ─────────────────────────────────────────
def _parse_text(content: str | None) -> str:
    if not content:
        return ""
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return str(obj.get("text") or "").strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


# 提及匹配：<at>...</at> 块，或「行首/空白后」的 @xxx。
# 用 (?:^|(?<=\s)) 限定 @ 必须在开头或空白后，避免把邮箱（如 user@domain.com）的 @ 域名误删。
_MENTION_RE = re.compile(r"<at[^>]*>.*?</at>|(?:^|(?<=\s))@\S+")


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


# ── 发送 ─────────────────────────────────────────────
def _format_reply_content(text: str, open_id: str | None, chat_type: str) -> str:
    """群聊时在正文前 @ 发送者；单聊不需要。"""
    if chat_type == "group" and open_id:
        return json.dumps(
            {"text": f"<at user_id=\"{open_id}\"></at>\n{text}"},
            ensure_ascii=False,
        )
    return json.dumps({"text": text}, ensure_ascii=False)


def _reply(
    lark_client: lark.Client,
    chat_type: str,
    chat_id: str,
    message_id: str,
    open_id: str | None,
    text: str,
) -> None:
    content = _format_reply_content(text, open_id, chat_type)
    if chat_type == "p2p":
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id).msg_type("text").content(content).build()
            ).build()
        )
        resp = lark_client.im.v1.message.create(req)
    else:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder().msg_type("text").content(content).build()
            ).build()
        )
        resp = lark_client.im.v1.message.reply(req)

    if not resp.success():
        logger.error("飞书发送失败: code=%s msg=%s", resp.code, resp.msg)


def _ack_reaction(http_client: FeishuClient, message_id: str, reaction: str) -> None:
    """给原消息加表情，作为"已收到"轻量 ack。失败仅记日志，不阻断主流程。"""
    try:
        http_client.add_reaction(message_id, reaction)
    except Exception as exc:
        logger.warning("加表情回应失败 (msg=%s, reaction=%s): %s", message_id, reaction, exc)


# ── 主入口 ───────────────────────────────────────────
def build_longconn_client() -> "lark.ws.Client":
    settings = get_settings()
    setup_logging(settings.app_log_level)

    app_id = (settings.feishu_app_id or "").strip()
    app_secret = (settings.feishu_app_secret or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")

    lark_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    http_client = FeishuClient(settings)
    ack_reaction = settings.feishu_ack_reaction or "Get"

    def _handle(text: str, session_id: str, chat_type: str, chat_id: str, message_id: str, open_id: str | None) -> None:
        t0 = time.perf_counter()
        try:
            clean_text = _strip_mentions(text)
            reply_text = agent_invoke(clean_text, session_id=session_id)
            if not reply_text:
                reply_text = "已收到请求，但暂时无法生成回复。"
            _reply(lark_client, chat_type, chat_id, message_id, open_id, reply_text)
            elapsed = time.perf_counter() - t0
            logger.info(
                "[perf] 端到端处理完成 session=%s chat=%s 耗时=%.2fs 回复字数=%d",
                session_id, chat_type, elapsed, len(reply_text),
            )
        except Exception as exc:
            logger.exception("Agent 执行异常")
            audit_log("feishu_agent_error", {"session_id": session_id, "error": str(exc)})
            friendly = (
                "😥 抱歉，处理您的请求时出了点问题，未能成功完成本次诊断。\n"
                "您可以：\n"
                "  1) 稍后重试，或换一种表述再 @ 我一次；\n"
                "  2) 如果问题持续存在，建议提交 Azure Support 工单获取人工协助："
                "https://portal.azure.com/#blade/Microsoft_Azure_Support/HelpAndSupportBlade"
            )
            try:
                _reply(lark_client, chat_type, chat_id, message_id, open_id, friendly)
            except Exception:
                logger.exception("发送错误回复也失败")

    def on_message(data: P2ImMessageReceiveV1) -> None:
        try:
            event = getattr(data, "event", None)
            if not event:
                return
            message = getattr(event, "message", None)
            if not message:
                return

            message_id = str(getattr(message, "message_id", "") or "")
            if _is_duplicate(message_id):
                return
            if str(getattr(message, "message_type", "") or "") != "text":
                return

            text = _parse_text(getattr(message, "content", ""))
            if not text:
                return

            chat_id = str(getattr(message, "chat_id", "") or "")
            if not chat_id:
                return

            sender = getattr(event, "sender", None)
            sid = getattr(sender, "sender_id", None) if sender else None
            open_id = str(getattr(sid, "open_id", "") or "") if sid else ""
            user_key = open_id or (str(getattr(sid, "user_id", "") or "") if sid else "") or "anonymous"
            session_id = f"feishu:{chat_id}:{user_key}"
            chat_type = str(getattr(message, "chat_type", "") or "")

            logger.info("收到飞书消息: user=%s chat_type=%s text=%s", user_key, chat_type, text[:80])

            # 立即给原消息加表情，告诉用户"收到了"（同步 1 个 HTTP 即可）
            _ack_reaction(http_client, message_id, ack_reaction)

            _WORKER_POOL.submit(
                _handle, text, session_id, chat_type, chat_id, message_id, open_id or None,
            )
        except Exception as exc:
            logger.exception("飞书消息解析异常")
            audit_log("feishu_longconn_parse_error", {"error": str(exc)})

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    return lark.ws.Client(
        app_id, app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )


def run_feishu_agent() -> None:
    ws = build_longconn_client()
    logger.info("飞书长连接 VM 诊断 Agent 已启动")
    ws.start()
