"""Skill 内嵌建议生成器。

把"已经渲染好的中文数据 + 领域规则 + 关键事实 dict"喂给小 LLM，
让它写「四、下一步建议」段。

设计要点：
  - **不喂原始 JSON**：减少幻觉，保证 LLM 围绕已展示给用户的数字写
  - **领域规则硬编码**：阈值、Azure 官方文档链接等，不依赖 LLM 记忆
  - **建议格式固定**：要求按 4 行小标题输出
  - **失败兜底**：LLM 不可用时回退到纯规则模板，不让整个 skill 挂掉
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from openai import AzureOpenAI

from app.config import get_settings
from app.tools.vm_flow_limits import VM_FLOW_LIMIT_DOC, ac_flow_limit, recommended_flow_limit

logger = logging.getLogger(__name__)


# ─────────────────── 领域规则（硬编码，不靠 LLM 记忆） ───────────────────
_BASE_OUTPUT_FORMAT = """
请严格按以下 4 行输出（每行一条，不要寒暄/前缀）：
- 是否异常：[✅正常 / ⚠️接近 / ❌异常] + 一句话依据
- 风险判断：[低 / 中 / 高] + 简短原因
- 建议动作：1) ... 2) ...（可选：升级 SKU / 提配额工单 / 升 SSD / 开 MANA / 提单 Azure Support）
- 参考文档：<URL>（如无具体 URL 则填 N/A）

如数据不足以判断，请回复「当前数据不足以判断，建议补齐监控后再排查」而不要编造。
""".strip()


_RULES_CPU = """
判断标准：
- CPU 峰值 < 80%：正常
- 80% ≤ 峰值 < 90%：接近，关注业务高峰
- 峰值 ≥ 90% 且持续多分钟：异常，建议升级 SKU 或排查进程占用
参考文档：https://learn.microsoft.com/azure/virtual-machines/sizes
"""

_RULES_MEMORY = """
判断标准：
- 内存使用率 < 85%：正常
- 85% ≤ 使用率 < 90%：接近，关注 OOM 风险
- 使用率 ≥ 90%：异常，建议升级到更大内存 SKU 或排查内存泄漏
若指标缺失：提示客户在 VM 上开启 Azure Monitor Agent / VM 诊断扩展。
参考文档：https://learn.microsoft.com/azure/azure-monitor/agents/azure-monitor-agent-overview
"""

_RULES_DISK = """
判断标准（按"实际峰值 / SKU 上限"百分比）：
- 单盘合计 IOPS 或吞吐 ≥ SKU 上限的 90%：异常，建议升级该盘 SKU 或扩容到下一档容量
- 80% ≤ 比例 < 90%：接近瓶颈，提醒客户关注
- VM 级合计 IOPS 或吞吐 ≥ VM 未缓存上限的 90%：VM 级瓶颈，需升级 VM SKU
- 任一磁盘 family 为 Standard HDD 且业务对延迟敏感：建议升级到 Standard SSD 或 Premium SSD
- 磁盘延迟峰值 > 30 ms 且 SKU 利用率不高：可能是 HDD 限制，建议升 SSD
参考文档：https://learn.microsoft.com/azure/virtual-machines/disks-types
"""


def _network_rules_text(vcpus: int | None, ac_sku: str | None = None) -> str:
    flow = recommended_flow_limit(vcpus)
    ac_limit = ac_flow_limit(ac_sku)
    if ac_limit is not None:
        return f"""
判断标准（连接数）：
- NIC 已开 Accelerated Connections：auxiliarySku = {ac_sku}
- AC 档位推荐上限：{ac_limit:,} flows（会覆盖 vCPU 默认档位）
- 实际峰值 / AC 档位上限 ≥ 90%：建议不同增加连接数限、考虑升级到更高 AC 档位 (A8) 或拆分流量；≥ 70%：关注
- 参考文档：{VM_FLOW_LIMIT_DOC}
""".strip()
    return f"""
判断标准（连接数）：
- 当前 vCPU 档位：{flow.tier_text}
- 非 MANA 推荐上限：{flow.limit_non_mana or 'N/A'} flows
- MANA 推荐上限：{flow.limit_mana or 'N/A'} flows
- 64+ vCPU 才有 MANA/非 MANA 的差异（开 MANA = 200 万；不开 = 100 万）；其他规格无差别
- 若未启用加速网卡：明确提示客户开启可显著降低延迟、提升 PPS
- 若连接数经常接近上限：可为 NIC 开启 Accelerated Connections（auxiliarySku A1/A2/A4/A8）获取更高连接能力
- 实际峰值 / 推荐上限 ≥ 90%：建议升级 SKU 或拆分流量；≥ 70%：关注
- 网络带宽峰值参考：https://learn.microsoft.com/azure/virtual-network/virtual-machine-network-throughput
参考文档：{VM_FLOW_LIMIT_DOC}
""".strip()


_RULES_RESOURCE_HEALTH = """
判断标准（**只看事件列表第一条，即最新事件**；事件已按时间倒序）：
- 最新事件 availability_state == Available：判定"正常"，风险=低；不要因为历史出现过 Unavailable / Unknown
  就给出有风险的结论。如客户对历史事件有疑问，建议进一步提单 Azure Support 跟进。
- 最新事件为 Planned（计划维护）：告知客户这是 Azure 例行 Storage / Host 更新，通常持续秒级。
- 最新事件为 Unavailable / Degraded：建议提单 Azure Support 跟进 Root Cause。
- 最新事件为 Unknown：通常是 RH 暂时无法判定，建议观察并核对业务实际感知。
参考文档：https://learn.microsoft.com/azure/service-health/resource-health-overview
"""


_RULES_FULL = """
整体诊断的输出规则：
- 只列【有异常】的指标（CPU / 内存 / 磁盘 / 网络 / 资源运行状态），数据正常的不列
- 若所有指标都在阈值内，给出一句"所有指标在阈值内，未发现明显异常"
- 异常指标按"严重程度：高→中→低"排序
- 末尾给出综合建议（可能涉及：升级 SKU、提配额、提单 Azure Support、开 MANA、升 SSD 等）
""".strip()


_RULES_MAP = {
    "cpu": _RULES_CPU,
    "memory": _RULES_MEMORY,
    "disk": _RULES_DISK,
    "resource_health": _RULES_RESOURCE_HEALTH,
}


# ─────────────────── LLM 客户端（懒加载） ───────────────────
# 与主 Agent 统一走 Azure OpenAI Chat Completions，api_version 复用 settings，
# 默认 GA 版 "2024-10-21"（跨厨商可移植的 OpenAI 兼容协议）。
_client: AzureOpenAI | None = None


def _get_client() -> AzureOpenAI | None:
    global _client
    if _client is not None:
        return _client
    s = get_settings()
    if not (s.azure_openai_endpoint and s.azure_openai_api_key):
        return None
    _client = AzureOpenAI(
        api_key=s.azure_openai_api_key,
        api_version=(s.azure_openai_api_version or "2024-10-21"),
        azure_endpoint=s.azure_openai_endpoint,
    )
    return _client


def _advice_deployment() -> str | None:
    s = get_settings()
    return (s.azure_openai_advice_deployment or s.azure_openai_deployment or "").strip() or None


# ─────────────────── 兜底建议（LLM 不可用时用） ───────────────────
def _fallback_advice(scope: str, facts: dict[str, Any]) -> str:
    if scope == "cpu":
        peak = facts.get("cpu_peak_pct")
        if peak is None:
            return "- 是否异常：N/A 未采集到 CPU 数据\n- 风险判断：低\n- 建议动作：检查监控配置后重试\n- 参考文档：N/A"
        verdict = "✅正常" if peak < 80 else ("⚠️接近" if peak < 90 else "❌异常")
        return (
            f"- 是否异常：{verdict}（CPU 峰值 {peak:.1f}%）\n"
            f"- 风险判断：{'低' if peak < 80 else '中' if peak < 90 else '高'}\n"
            f"- 建议动作：{'持续监控即可' if peak < 80 else '关注业务高峰；如持续 ≥90% 可考虑升级 SKU'}\n"
            f"- 参考文档：https://learn.microsoft.com/azure/virtual-machines/sizes"
        )
    return "- 是否异常：N/A（建议生成器暂不可用）\n- 风险判断：低\n- 建议动作：参考数据自行评估\n- 参考文档：N/A"


# ─────────────────── 主入口 ───────────────────
def generate_advice(
    scope: str,
    facts: dict[str, Any],
    extra_rules: str | None = None,
) -> str:
    """生成「结论」段。

    scope: cpu / memory / disk / network / resource_health / full
    facts: 关键事实字典（结构化指标），LLM 据此写建议
    """
    if scope == "network":
        rules = _network_rules_text(facts.get("vcpus"), facts.get("accelerated_connections_sku"))
    elif scope == "full":
        rules = _RULES_FULL
    else:
        rules = _RULES_MAP.get(scope, "")

    if extra_rules:
        rules = f"{rules}\n\n额外要求：\n{extra_rules}"

    client = _get_client()
    deployment = _advice_deployment()
    if client is None or deployment is None:
        logger.warning("建议生成器：Azure OpenAI 未配置，使用兜底文本")
        return _fallback_advice(scope, facts)

    system = (
        "你是 Azure VM 运维资深 SRE。基于给定的真实指标数据和领域规则，"
        "用中文写一段【结论】。不要复述数据本身，只给结论和动作。"
    )
    user = (
        f"## 关键事实（结构化指标）\n```json\n{json.dumps(facts, ensure_ascii=False, default=str)}\n```\n\n"
        f"## 领域规则（必须遵守）\n{rules}\n\n"
        f"## 输出格式（必须严格遵守）\n{_BASE_OUTPUT_FORMAT}"
    )
    try:
        t0 = time.perf_counter()
        resp = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        elapsed = time.perf_counter() - t0
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None) if usage else None
        out_tok = getattr(usage, "completion_tokens", None) if usage else None
        total_tok = getattr(usage, "total_tokens", None) if usage else None
        logger.info(
            "[perf] advice LLM scope=%s 耗时=%.2fs tokens 输入=%s 输出=%s 合计=%s",
            scope, elapsed, in_tok, out_tok, total_tok,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or _fallback_advice(scope, facts)
    except Exception as exc:
        logger.warning("建议生成器 LLM 调用失败：%s，回退到模板", exc)
        return _fallback_advice(scope, facts)
