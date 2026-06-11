from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError

from app.config import Settings


@dataclass
class _TokenCache:
    token: str = ""
    expires_at: float = 0.0


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token_cache = _TokenCache()

    def _join_url(self, path: str) -> str:
        return f"{self.settings.feishu_base_url.rstrip('/')}/{path.lstrip('/')}"

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            req_headers.update(headers)

        req = request.Request(url=url, data=body, headers=req_headers, method="POST")
        try:
            with request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"Feishu HTTP {e.code} {e.reason}: {err_body}") from e
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict) and int(data.get("code", 0)) != 0:
            raise RuntimeError(f"Feishu API 调用失败: code={data.get('code')} msg={data.get('msg')}")
        return data

    def _get_tenant_access_token(self) -> str:
        now = time.time()
        if self._token_cache.token and now < self._token_cache.expires_at:
            return self._token_cache.token

        app_id = (self.settings.feishu_app_id or "").strip()
        app_secret = (self.settings.feishu_app_secret or "").strip()
        if not app_id or not app_secret:
            raise RuntimeError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")

        data = self._post_json(
            self._join_url("/open-apis/auth/v3/tenant_access_token/internal"),
            {"app_id": app_id, "app_secret": app_secret},
        )
        token = str(data.get("tenant_access_token") or "")
        expire = int(data.get("expire", 0) or 0)
        if not token:
            raise RuntimeError("获取 tenant_access_token 失败")

        self._token_cache.token = token
        self._token_cache.expires_at = now + max(expire - 60, 60)
        return token

    @staticmethod
    def parse_text_message(content: str | None) -> str:
        if not content:
            return ""
        try:
            obj = json.loads(content)
            if isinstance(obj, dict):
                text = obj.get("text")
                return str(text or "").strip()
        except json.JSONDecodeError:
            pass
        return str(content).strip()

    def send_text_to_chat(self, chat_id: str, text: str) -> dict[str, Any]:
        token = self._get_tenant_access_token()
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        return self._post_json(
            self._join_url("/open-apis/im/v1/messages?receive_id_type=chat_id"),
            payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    def add_reaction(self, message_id: str, reaction_type: str = "OnIt") -> dict[str, Any]:
        """给消息加表情回应（用作“已收到”ack）。

        参考：https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce
        emoji_type 大小写敏感，常用：Get / THUMBSUP / OK / DONE / HEART / CLAP / SMILE / Fire。
        """
        token = self._get_tenant_access_token()
        url = self._join_url(f"/open-apis/im/v1/messages/{message_id}/reactions")
        payload = {"reaction_type": {"emoji_type": reaction_type}}
        return self._post_json(url, payload, headers={"Authorization": f"Bearer {token}"})
