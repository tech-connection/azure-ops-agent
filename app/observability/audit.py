import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


logger = logging.getLogger("azure_support_agent.audit")

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """配置全局日志：同时输出到控制台和日志文件。"""
    log_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(log_level)

    # 避免重复添加 handler
    if root.handlers:
        return

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FMT)

    # 控制台输出
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件输出（10 MB 轮转，保留 5 个备份）
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        _LOG_DIR / "agent.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 抑制 Azure SDK 冗长的 HTTP 请求/响应头日志
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    logging.getLogger("azure.identity").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def audit_log(event: str, payload: dict[str, Any]) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "payload": payload,
    }
    logger.info(json.dumps(record, ensure_ascii=False))
