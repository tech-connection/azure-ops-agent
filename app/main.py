"""程序入口。

两种启动方式：
  1. 飞书长连接（生产）：python -m app.main
  2. HTTP 健康检查（监控）：uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.config import get_settings
from app.models.schemas import HealthResponse
from app.observability.audit import setup_logging

settings = get_settings()
setup_logging(settings.app_log_level)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# HTTP（仅 /health；不暴露任何业务接口）
# ──────────────────────────────────────────────
app = FastAPI(title="Azure VM Diagnosis Agent")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        azure_auth_mode=settings.azure_auth_mode,
        azure_subscription_id_tail=settings.sub_id_tail,
        llm_ready=bool(settings.azure_openai_endpoint and settings.azure_openai_api_key and settings.azure_openai_deployment),
        feishu_ready=bool(settings.feishu_app_id and settings.feishu_app_secret),
    )


# ──────────────────────────────────────────────
# 飞书长连接入口
# ──────────────────────────────────────────────
def run_feishu_agent() -> None:
    try:
        from app.feishu_longconn import run_feishu_agent as _run
    except ImportError as e:
        logger.error("lark_oapi 未安装：%s", e)
        return
    logger.info("启动飞书长连接 Agent ...")
    _run()


if __name__ == "__main__":
    run_feishu_agent()
