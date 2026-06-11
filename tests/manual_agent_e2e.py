"""端到端手动测试：直接打 agent_invoke（模拟飞书 worker 入口），真连 Azure + LLM。

用途：复现/验证“gpt-4.1 偶发只复读技能名、不执行 skill”的问题。
逐条用例 × 多轮重复调用真实 agent，自动判定每轮是否真正命中 skill（而非复读技能名）。

运行（需先配好 .env 的 AZURE_OPENAI_* / AZURE_SUBSCRIPTION_ID 等）：
    cd /opt/azure-vm-diagnosis-agent
    .venv/bin/python -m tests.manual_agent_e2e                       # 默认 VM 名，每例跑 3 轮
    .venv/bin/python -m tests.manual_agent_e2e --vm <主机名> --rounds 5
    .venv/bin/python -m tests.manual_agent_e2e --only network,qa     # 只跑部分用例

注意：这是“手动/集成”脚本，不在 pytest 自动收集范围内（文件名不以 test_ 开头）。
"""
from __future__ import annotations

import argparse
import sys
import time

from app.agent.vm_agent import agent_invoke, reset_session

# 失败模式识别：模型把这些“内部选路标识”当成了最终回复
_SKILL_TOKENS = {
    "vm-full-diagnosis", "vm-cpu-check", "vm-memory-check", "vm-disk-check",
    "vm-network-check", "vm-resource-health-check", "vm-unsupported-metric", "azure-qa",
    "vm_full_diagnosis", "vm_cpu_check", "vm_memory_check", "vm_disk_check",
    "vm_network_check", "vm_resource_health_check", "vm_unsupported_metric", "azure_qa",
    "run_skill_script",
}


def _looks_like_skill_name_echo(reply: str) -> bool:
    """判定回复是否为“只复读技能名/脚本名”的失败模式。

    正常 skill 返回是长报告（诊断带分节标题、azure-qa 带“知识问答模式”头），
    失败模式则是极短文本且整体就是一个技能/脚本名。
    """
    r = (reply or "").strip()
    if not r:
        return True  # 空回复也算异常
    # 整段就是一个技能名/脚本名
    if r in _SKILL_TOKENS:
        return True
    # 很短(<40字)且包含技能 token，且不含正常回复的特征头
    if len(r) < 40 and any(tok in r for tok in _SKILL_TOKENS):
        return True
    return False


# (用例 key, 用户消息, 期望命中的 skill 关键词用于人工核对)
def _build_cases(vm: str) -> list[tuple[str, str, str]]:
    return [
        ("network", f"{vm} 这台主机的网络间歇丢包，什么原因", "vm-network-check"),
        ("cpu", f"{vm} 最近一小时 CPU 高不高", "vm-cpu-check"),
        ("memory", f"看下 {vm} 的内存使用情况", "vm-memory-check"),
        ("disk", f"{vm} 磁盘 IOPS 是不是打满了", "vm-disk-check"),
        ("health", f"{vm} 最近有没有平台维护事件", "vm-resource-health-check"),
        ("full", f"帮我整体体检一下 {vm}", "vm-full-diagnosis"),
        ("qa", "Azure D 系列和 E 系列虚拟机有什么区别", "azure-qa"),
        ("unsupported", "帮我查下 my-redis-cache 这个 Redis 的命中率", "vm-unsupported-metric"),
        ("chitchat", "你好啊，今天天气不错", "azure-qa"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm", default="centralindia-1779781834287-dwsip1JR-1",
                        help="测试用 VM 主机名（真打 Azure；不存在也没关系，仍能验证路由）")
    parser.add_argument("--rounds", type=int, default=3, help="每个用例重复轮数")
    parser.add_argument("--only", default="", help="逗号分隔的用例 key，仅跑这些；默认全跑")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    cases = _build_cases(args.vm)
    if args.only:
        keys = {k.strip() for k in args.only.split(",") if k.strip()}
        cases = [c for c in cases if c[0] in keys]

    total = 0
    failures: list[str] = []
    print(f"=== Agent E2E 测试  vm={args.vm}  rounds={args.rounds}  用例数={len(cases)} ===\n")

    for key, msg, expect in cases:
        # 每个用例用独立 session，且每轮前重置，避免历史串扰，纯测单条路由
        session_id = f"e2e:test:{key}"
        for rnd in range(1, args.rounds + 1):
            reset_session(session_id)
            total += 1
            t0 = time.perf_counter()
            try:
                reply = agent_invoke(msg, session_id=session_id, timeout=args.timeout)
                elapsed = time.perf_counter() - t0
                bad = _looks_like_skill_name_echo(reply)
                status = "❌ 复读技能名/异常" if bad else "✅ 命中 skill"
                if bad:
                    failures.append(f"[{key} r{rnd}] 回复={reply!r}")
                preview = reply.replace("\n", " ")[:80]
                print(f"[{key:11s} r{rnd}] {status}  期望={expect:24s} 耗时={elapsed:5.1f}s  回复={preview}")
            except Exception as exc:  # noqa: BLE001
                elapsed = time.perf_counter() - t0
                failures.append(f"[{key} r{rnd}] 异常={exc!r}")
                print(f"[{key:11s} r{rnd}] 💥 异常  耗时={elapsed:5.1f}s  {exc!r}")

    print("\n=== 汇总 ===")
    fail_n = len(failures)
    print(f"总轮数={total}  成功={total - fail_n}  失败={fail_n}")
    if failures:
        print("\n失败明细：")
        for f in failures:
            print("  - " + f)
        return 1
    print("全部命中 skill ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
