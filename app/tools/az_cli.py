"""Azure CLI 执行适配层：对外只暴露一个只读的 `run_az`。

由 app/tools/az_tool.py 包装成 Agent 工具，供 SKILL.md 驱动的技能（CPU / 内存 /
磁盘 / 网络 / Resource Health）按文档里写明的 az 命令逐条执行。

安全：
  - 一律用 subprocess argv 列表（shell=False）传参，绝不拼接 shell 字符串，
    杜绝命令注入（vm_name / resource_group 来自用户输入，是注入面）。
  - 鉴权：
      AZURE_AUTH_MODE=cli → 复用当前 az 登录态（与原 AzureCliCredential 行为一致）；
      AZURE_AUTH_MODE=spn → 首次调用时 az login --service-principal 登录一次并缓存。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0
_login_lock = threading.Lock()
_logged_in = False


class AzCliError(Exception):
    """az 命令执行失败。code 与原 SDK 路径的错误码对齐，便于上层统一处理。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ─────────────────── az 可执行文件 & 登录 ───────────────────
def _az_path() -> str:
    p = shutil.which("az")
    if not p:
        raise AzCliError("AZ_NOT_FOUND", "未找到 az CLI，请先安装 Azure CLI 并确保在 PATH 中")
    return p


def _ensure_login() -> None:
    """spn 模式首次调用时登录一次并缓存；cli 模式直接复用现有登录态。"""
    global _logged_in
    s = get_settings()
    mode = (s.azure_auth_mode or "").strip().lower()

    if mode in {"cli", "azure_cli"}:
        return  # 复用当前 az 登录态

    if mode in {"spn", "client_secret", "service_principal"}:
        if _logged_in:
            return
        with _login_lock:
            if _logged_in:
                return
            if not (s.azure_tenant_id and s.azure_client_id and s.azure_client_secret):
                raise AzCliError(
                    "UNAUTHORIZED",
                    "AZURE_AUTH_MODE=spn 时必须配置 AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET",
                )
            # 注意：secret 经 argv 传给子进程（不经 shell）。生产如需进一步收敛，
            # 可改用托管标识（MSI）或预先 az login 缓存，避免 secret 出现在进程参数中。
            cmd = [
                _az_path(), "login", "--service-principal",
                "-u", s.azure_client_id,
                "-p", s.azure_client_secret,
                "--tenant", s.azure_tenant_id,
                "-o", "none", "--only-show-errors",
            ]
            logger.info("az 凭证模式: spn，执行 az login --service-principal（一次性）")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT)
            if proc.returncode != 0:
                raise AzCliError("UNAUTHORIZED", f"az login 失败：{proc.stderr.strip()[:300]}")
            _logged_in = True
        return

    raise AzCliError("UNAUTHORIZED", f"不支持的 AZURE_AUTH_MODE={mode!r}，仅允许 'spn' 或 'cli'")


def _classify_error(stderr: str) -> tuple[str, str]:
    """把 az stderr 归类到与 SDK 路径一致的错误码。"""
    low = (stderr or "").lower()
    msg = (stderr or "").strip()
    short = msg[-300:] if len(msg) > 300 else msg
    if "was not found" in low or "resourcenotfound" in low or "not found" in low:
        return "NOT_FOUND", short or "资源不存在"
    if "aadsts" in low or "az login" in low or "credential" in low or "expired" in low or "unauthorized" in low:
        return "UNAUTHORIZED", short or "Azure 认证失败，请检查登录态/凭证"
    return "AZURE_ERROR", short or "az 命令执行失败"


def run_az(args: list[str], timeout: float = _DEFAULT_TIMEOUT) -> Any:
    """执行 `az <args>` 并解析 JSON 输出。

    args 不含开头的 'az'；务必传 argv 列表（每个参数一个元素），不要传整条字符串。
    返回解析后的 JSON（dict / list），无输出时返回 None。
    """
    _ensure_login()
    cmd = [_az_path(), *args]
    # 锁定到配置指定的订阅，避免误用 az 默认订阅（az 登录态默认订阅可能与本服务不同）。
    if "--subscription" not in args:
        sub = (get_settings().azure_subscription_id or "").strip()
        if sub:
            cmd += ["--subscription", sub]
    cmd += ["--only-show-errors"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AzCliError("TIMEOUT", f"az 命令超时（>{timeout:.0f}s）：az {' '.join(args[:3])} ...")
    except Exception as exc:  # noqa: BLE001
        raise AzCliError("INTERNAL_ERROR", f"az 命令执行异常：{exc}")

    if proc.returncode != 0:
        code, msg = _classify_error(proc.stderr)
        raise AzCliError(code, msg)

    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise AzCliError("PARSE_ERROR", f"az 输出非合法 JSON：{exc}")
