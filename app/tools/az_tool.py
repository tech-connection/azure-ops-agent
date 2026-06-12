"""把 `az` CLI 暴露成一个基本**只读**的 Agent 工具，供模型按 SKILL.md 里写的命令调用。

设计：
  - 标准 skill 模式：SKILL.md 正文里写明要执行的 `az` 命令，模型读取后调用本工具执行，
    命令**不写死在代码里**，完全以 SKILL.md 为准。
  - 安全（生产机！）：默认只放行只读查询类命令，任何改动/删除/进入 VM 的命令一律拒绝。
      * 白名单：命令的前导子命令必须落在 _ALLOWED_PREFIXES 内；
      * 黑名单兜底：参数里出现任何变更类动词（delete/create/update/...）直接拒绝；
      * 一律 argv 列表（shell=False），杜绝命令注入。
  - **唯一例外（受控写操作）**：`az support in-subscription tickets create`（提交 Azure 支持工单）。
    仅精确放行这一条建单命令；本工具不对命令参数做任何改写/注入，联系人/邮箱/工单名等参数全部由 SKILL.md 定义、模型填入。其余 az support 命令仅放行只读查询。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from agent_framework import tool

from app.tools.az_cli import AzCliError, run_az as _run_az_raw

logger = logging.getLogger(__name__)

# 只读子命令白名单（按前导 token 序列匹配，遇到第一个以 '-' 开头的参数即停止）。
_ALLOWED_PREFIXES: set[tuple[str, ...]] = {
    ("account", "show"),
    ("vm", "show"),
    ("vm", "list"),
    ("vm", "list-skus"),
    ("vm", "list-sizes"),
    ("vm", "get-instance-view"),
    ("monitor", "metrics", "list"),
    ("monitor", "metrics", "list-definitions"),
    ("network", "nic", "show"),
    ("network", "nic", "list"),
    ("network", "public-ip", "show"),
    ("disk", "show"),
    ("disk", "list"),
    ("resource", "show"),
    ("resource", "list"),
    ("rest",),  # 仅放行只读 GET（见 _validate_rest），用于 Resource Health 等无原生 az 子命令的 ARM 接口
}

# az support 命令白名单：只读查询 + 唯一受控写操作（建单）。其余 support 命令一律拒绝。
_ALLOWED_SUPPORT_PREFIXES: set[tuple[str, ...]] = {
    ("support", "services", "list"),
    ("support", "services", "problem-classifications", "list"),
    ("support", "in-subscription", "tickets", "list"),
    ("support", "in-subscription", "tickets", "show"),
    ("support", "in-subscription", "tickets", "create"),  # 受控写：提交支持工单
}

# 黑名单兜底：只要参数里出现这些变更类动词，一律拒绝（防止白名单被绕过）。
_DENIED_TOKENS: set[str] = {
    "delete", "create", "update", "set", "patch", "add", "remove",
    "start", "stop", "restart", "deallocate", "redeploy", "reimage",
    "run-command", "invoke", "execute", "ssh", "login", "logout",
    "capture", "convert", "generalize", "migrate", "perform-maintenance",
    "assign-identity", "extension",
}


def _leading_subcommand(args: list[str]) -> tuple[str, ...]:
    """取开头的非 flag token（直到第一个以 '-' 开头的参数），作为子命令路径。"""
    out: list[str] = []
    for a in args:
        if a.startswith("-"):
            break
        out.append(a.lower())
    return tuple(out)


def _flag_value(args: list[str], flag: str) -> str | None:
    """取 `--flag value` 形式的值（不支持 --flag=value 写法时回退处理）。"""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _validate_rest(args: list[str]) -> str | None:
    """`az rest` 仅放行只读 GET：method 必须是 get、URL 必须指向 ARM 管理面、禁止写 body。"""
    method = (_flag_value(args, "--method") or _flag_value(args, "-m") or "get").lower()
    if method != "get":
        return f"拒绝执行：az rest 仅允许 --method get（本工具只读），收到 {method!r}"
    for f in ("--body", "--input-file", "--headers"):
        # 禁止携带请求体/输入文件（避免任何写操作）；headers 也一并禁止以收敛攻击面。
        if any(a == f or a.startswith(f + "=") for a in args):
            return f"拒绝执行：az rest 不允许携带 {f}"
    url = _flag_value(args, "--url") or _flag_value(args, "-u") or ""
    if not url.lower().startswith("https://management.azure.com/"):
        return "拒绝执行：az rest 仅允许访问 https://management.azure.com/ 的只读接口"
    return None


def _validate_support(lead: tuple[str, ...], args: list[str]) -> str | None:
    """az support 命令校验：只读查询全部放行；写操作仅精确放行建单这一条。"""
    if any(lead[: len(p)] == p for p in _ALLOWED_SUPPORT_PREFIXES):
        return None
    allowed = "、".join(" ".join(p) for p in sorted(_ALLOWED_SUPPORT_PREFIXES))
    return (
        f"拒绝执行：'az {' '.join(lead) or '?'}' 不在 az support 白名单内。"
        f"仅允许：{allowed}"
    )


def _validate_readonly(args: list[str]) -> str | None:
    """校验命令。只读命令通过白/黑名单；az support 走专用授权（含受控建单）。"""
    if not args:
        return "命令为空"
    lead = _leading_subcommand(args)
    # az support 单独授权：黑名单不适用（建单本身含 'create'），由 _validate_support 精确放行
    if lead[:1] == ("support",):
        return _validate_support(lead, args)
    # 黑名单兜底
    for a in args:
        if a.lower() in _DENIED_TOKENS:
            return f"拒绝执行：命令包含变更类操作 {a!r}（本工具仅允许只读查询）"
    # 白名单：前导子命令须匹配某个允许前缀
    if not any(lead[: len(p)] == p for p in _ALLOWED_PREFIXES):
        allowed = "、".join(" ".join(p) for p in sorted(_ALLOWED_PREFIXES))
        return (
            f"拒绝执行：'az {' '.join(lead) or '?'}' 不在只读白名单内。"
            f"允许的命令前缀：{allowed}"
        )
    # az rest 需额外收敛为只读 GET
    if lead[:1] == ("rest",):
        return _validate_rest(args)
    return None


_MAX_OUTPUT_CHARS = 20000


@tool(
    name="run_az",
    description=(
        "执行一条 Azure CLI 命令并返回 JSON 结果。"
        "参数 args 是去掉开头 'az' 之后的参数列表（每个参数一个数组元素），"
        "例如 az vm show -d -g rg -n vm 对应 args=['vm','show','-d','-g','rg','-n','vm','-o','json']。"
        "默认仅支持只读查询（vm show / vm list-skus / monitor metrics list 等）；"
        "任何创建/修改/删除/重启/进入 VM 的命令都会被拒绝。"
        "唯一例外是提交 Azure 支持工单 az support in-subscription tickets create，"
        "其全部参数（含联系人/邮箱/工单名）由模型按 SKILL.md 提供，本工具不做参数注入。"
        "建议在命令中带 -o json，并用 --query 缩小返回数据。"
    ),
)
async def run_az(args: list[str]) -> str:
    """按 SKILL.md 给出的 az 命令执行，返回 JSON 字符串（或错误说明）。"""
    reason = _validate_readonly(args)
    if reason is not None:
        logger.warning("run_az 拒绝命令 args=%s 原因=%s", args, reason)
        return json.dumps({"error": "FORBIDDEN", "message": reason}, ensure_ascii=False)

    logger.info("run_az 执行: az %s", " ".join(args))
    cmd_label = " ".join(_leading_subcommand(args)) or "?"
    t0 = time.perf_counter()
    try:
        result = await asyncio.to_thread(_run_az_raw, args)
    except AzCliError as exc:
        logger.info("[perf] az %s 耗时=%.2fs（失败:%s）", cmd_label, time.perf_counter() - t0, exc.code)
        return json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.info("[perf] az %s 耗时=%.2fs（异常）", cmd_label, time.perf_counter() - t0)
        return json.dumps({"error": "INTERNAL_ERROR", "message": str(exc)}, ensure_ascii=False)

    logger.info("[perf] az %s 耗时=%.2fs", cmd_label, time.perf_counter() - t0)
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > _MAX_OUTPUT_CHARS:
        return json.dumps(
            {
                "error": "OUTPUT_TOO_LARGE",
                "message": (
                    f"返回数据过大（{len(text)} 字符）。请在命令中加 --query 收窄结果，"
                    "例如只取峰值点而非全部数据点。"
                ),
            },
            ensure_ascii=False,
        )
    return text
