"""Pydantic 数据模型。

ToolResult 是所有数据采集层（app/tools/*）的统一返回类型；
Skill 层只关心 ToolResult.data 与 ToolResult.ok / message。

HealthResponse 是 /health 接口的返回。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """所有数据采集函数的统一返回：成功时 data 必填，失败时 message 解释原因。"""

    ok: bool
    code: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str = "ok"
    azure_auth_mode: str
    azure_subscription_id_tail: str  # 只暴露 sub ID 末 6 位，避免泄露
    llm_ready: bool
    feishu_ready: bool
