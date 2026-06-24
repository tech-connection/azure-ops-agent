"""主诊断 Agent。

职责：
  - 把用户中文请求路由到多个 Skill 之一
  - 维护"飞书 chat + 用户"维度的会话上下文（idle TTL + 最大轮次）
  - 强制要求把 skill 的返回值**逐字原样**输出
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agent_framework import Agent, AgentSession, InMemoryHistoryProvider, SkillsProvider
from agent_framework.azure import AzureOpenAIChatClient

from app.config import get_settings
from app.skills.vm_skills import build_vm_skills

logger = logging.getLogger(__name__)


SYSTEM_INSTRUCTIONS = """
你是 Azure 运维 Agent。

【核心规则】你不能直接作答，必须先按【路由】选定**一个**技能，再 `load_skill`(skill_name=该技能) 读取其 SKILL.md，然后按其类型处理：
- **诊断型**（除 azure-qa、azure-support-case 外的全部技能）：严格按 SKILL.md 写明的 az 命令，用工具 `run_az` 逐条执行（命令以 SKILL.md 为准、不得自创，只允许只读命令），最后按其「输出格式」组装中文报告作为最终回复。
- **azure-qa**（不调任何工具）：按 SKILL.md 判断属于「知识问答」还是「能力边界兜底」，直接据其「回答规则 / 输出话术」组织回复——前者用你的通识知识作答，后者输出固定边界话术。
- **azure-support-case**（提交支持工单，含受控写操作）：严格按 SKILL.md 五步流程。先出工单草稿 + 严重性征求确认，**本轮不得创建**；仅当用户在**新一轮**明确确认后，才用 `run_az` 执行 SKILL.md 里的 `support in-subscription tickets create` 建单。

【路由】按下表选 skill_name（选中后即按上面对应类型执行）：
- VM 整体诊断/体检 → vm-full-diagnosis
- VM CPU/处理器/负载 → vm-cpu-check
- VM 内存/MEM/RAM → vm-memory-check
- VM 磁盘/IO/IOPS/吞吐/慢盘 → vm-disk-check
- VM 网络/带宽/连接数/flow/加速网卡 → vm-network-check
- 某台具体 VM 的资源健康/维护/平台事件（已给出或历史提过主机名）→ vm-resource-health-check
- Azure 平台级故障/服务运行状况（订阅范围、不针对单台资源）→ service-health-check
  ★ 关键区分：问「某服务类目（VM / AOAI / OpenAI / Storage / SQL / Network 等）有没有故障」、「今天/最近几天有没有故障」、或裸问有没有故障 / 是不是挂了 / outage / 某区域中断，且**未点名具体主机名** → service-health-check（把服务名当过滤条件，不要追问主机名、不要当边界话术拒答）；只有明确点名某台主机问其事件 → vm-resource-health-check。
- 主机名与实例 ID 互查/互转（给主机名/FQDN 查实例 ID，或给实例 ID/资源名 查主机名/计算机名）→ vm-hostname-lookup
  ★ 只要输入是 VM 主机名/FQDN（带点分域名，可能被转成链接）或实例 ID/资源名（如 westeurope-xxx-1），只是要查它们的对应关系，不是诊断指标 → 都走 vm-hostname-lookup，**不要当 azure-qa 边界话术拒答**。
- 非 VM 资源（MySQL/SQL/Redis/Cosmos/LB/AppGW/Function/AKS/存储等）或 VM 内部维度（进程/文件系统/应用日志）的实时诊断，即使带名字也绝不当 vm_name → azure-qa（边界话术）。
- 提工单/开 case/提交 Azure 支持/联系微软支持/报故障给微软/提 ticket → azure-support-case。
- 其余一律 azure-qa（知识问答）：Azure 通识（概念/产品/SKU/限额/对比/最佳实践）、解释/翻译/说明含义（即使含 `状态=`/`IOPS`/`CPU` 等关键词也走这里）、闲聊、无法归类的兜底。

【约束】
- 一次只处理一个技能，不要对同一请求重复路由或调别的技能验证；仅当用户在一条消息里明确要诊断多台主机 / 多类指标时才多次执行，结果按顺序用空行拼接。
- 诊断型技能依次调用 run_az 完成多步采集是正常流程，不要中途停（full 合并五维、共享步骤只跑一次；disk 逐块盘查 SKU 与指标；network 开 AC 时连接数改从主 NIC 查；resource-health / service-health 需先取订阅 ID 再用 az rest 调 REST API）。
- 禁止把技能名或路由结论吐给用户（那是内部选路）；禁止改写、总结、缩写、重新分段、补结论、加表情寒暄或任何前后缀。
- 多轮上下文：当前消息缺主机名/资源组/时间窗时，沿用历史里用户最近一次提到的值（尤其主机名——用户常先报主机、后续只说"CPU 高""看下内存""健康事件"，须延续同一台）。仅当整段历史从未出现任何主机名时，才不调工具、用一句话请用户补主机名。
- 提工单为唯一写操作：必须「出草稿」与「建单」分属两轮，出草稿那一轮绝不调建单命令；未得用户在新一轮明确确认前，不得执行 `support ... tickets create`。

【参数】
- vm_name 必填，逐字原样照抄，禁止删改；结尾 `-1`/`-2`/`-01` 是名字的一部分必须完整保留（如 `centralindia-1689758903318-6kvd4zdU-1` 不能截成 `...-6kvd4zdU`）。
- resource_group 用户给才传。
- 时间窗（消息前 `[当前北京时间: ...]` 即"现在"）：「近 X 分钟/小时」→ lookback_minutes；具体区间/时点 → start_time_beijing + end_time_beijing（`YYYY-MM-DD HH:MM:SS`）；二者只传其一。
- vm-resource-health-check：「最近 N 条」→ top_n=N。
- 不反问补参数，能默认就默认。
""".strip()


# ───────────────────────── 会话管理 ─────────────────────────
@dataclass
class _SessionEntry:
    session: AgentSession
    last_used: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _AgentRuntime:
    """单例: 一个共享 Agent + 多个 per-session AgentSession + 后台 asyncio 循环。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionEntry] = {}
        self._sessions_guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._agent: Agent | None = None
        self._agent_lock = threading.Lock()

    # ── 后台事件循环 ──
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop and self._loop.is_running():
            return self._loop

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            loop.run_forever()

        t = threading.Thread(target=_run, name="vm-agent-loop", daemon=True)
        t.start()
        self._loop_ready.wait(timeout=5)
        assert self._loop is not None
        return self._loop

    # ── 构造 Agent ──
    def _build_agent(self) -> Agent:
        if self._agent is not None:
            return self._agent
        with self._agent_lock:
            if self._agent is not None:
                return self._agent
            s = get_settings()
            if not (s.azure_openai_endpoint and s.azure_openai_api_key and s.azure_openai_deployment):
                raise RuntimeError(
                    "Azure OpenAI 未配置完整：需要 AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY / AZURE_OPENAI_DEPLOYMENT"
                )
            client = AzureOpenAIChatClient(
                endpoint=s.azure_openai_endpoint,
                api_key=s.azure_openai_api_key,
                api_version=(s.azure_openai_api_version or "2024-10-21"),
                deployment_name=s.azure_openai_deployment,
            )
            skills_provider = SkillsProvider(skills=build_vm_skills())
            history_provider = InMemoryHistoryProvider()
            from app.tools.az_tool import run_az  # 只读 az 工具，供 SKILL.md 驱动的技能调用
            agent = Agent(
                client,
                SYSTEM_INSTRUCTIONS,
                name="vm-diagnosis-agent",
                description="Azure VM 诊断 Agent（5 类指标）",
                tools=[run_az],
                context_providers=[skills_provider, history_provider],
            )
            self._agent = agent
            return agent

    # ── 会话管理 ──
    def _evict_expired(self) -> None:
        s = get_settings()
        ttl = s.session_idle_ttl_sec
        now = time.time()
        with self._sessions_guard:
            stale = [sid for sid, e in self._sessions.items() if now - e.last_used > ttl]
            for sid in stale:
                self._sessions.pop(sid, None)
                logger.info("会话 %s 闲置超时已清理", sid)

    def _get_session(self, session_id: str) -> _SessionEntry:
        with self._sessions_guard:
            entry = self._sessions.get(session_id)
            if entry is None:
                entry = _SessionEntry(session=AgentSession(session_id=session_id))
                self._sessions[session_id] = entry
            entry.last_used = time.time()
            return entry

    def reset_session(self, session_id: str) -> None:
        with self._sessions_guard:
            self._sessions.pop(session_id, None)
            logger.info("会话 %s 已主动重置", session_id)

    # ── 截断历史（按"最大轮次"限制） ──
    def _truncate_history(self, entry: _SessionEntry) -> None:
        s = get_settings()
        max_turns = s.session_max_turns
        if max_turns <= 0:
            return
        state = entry.session.state
        msgs = list(state.get("messages") or [])
        # 一轮 ≈ 1 user + 1 assistant + 中间 tool（可能多条），按 user 消息数量截断
        user_indices = [i for i, m in enumerate(msgs) if getattr(m, "role", None) == "user"]
        if len(user_indices) <= max_turns:
            return
        cut_from = user_indices[-max_turns]
        state["messages"] = msgs[cut_from:]

    # ── 实际异步执行 ──
    async def _async_run(self, text: str, session_id: str) -> str:
        agent = self._build_agent()
        entry = self._get_session(session_id)
        # 注入当前北京时间，供 LLM 解析“9-10 点”这类相对时间
        from datetime import datetime, timezone, timedelta
        bj_now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        prompt = f"[当前北京时间: {bj_now}]\n{text}"
        async with entry.lock:
            entry.last_used = time.time()
            self._truncate_history(entry)
            t0 = time.perf_counter()
            resp = await agent.run(prompt, session=entry.session)
            elapsed = time.perf_counter() - t0
            entry.last_used = time.time()
            # Agent Framework rc5: usage_details 是 TypedDict（dict），key 为 input_token_count / output_token_count / total_token_count
            usage = getattr(resp, "usage_details", None) or {}
            in_tok = usage.get("input_token_count") if isinstance(usage, dict) else None
            out_tok = usage.get("output_token_count") if isinstance(usage, dict) else None
            total_tok = usage.get("total_token_count") if isinstance(usage, dict) else None
            logger.info(
                "[perf] agent.run session=%s 耗时=%.2fs tokens 输入=%s 输出=%s 合计=%s",
                session_id, elapsed, in_tok, out_tok, total_tok,
            )
        return self._extract_text(resp)

    @staticmethod
    def _extract_text(resp: Any) -> str:
        # AgentResponse: 字符串/messages/text
        if resp is None:
            return ""
        if isinstance(resp, str):
            return resp
        if hasattr(resp, "text") and isinstance(resp.text, str):
            return resp.text
        if hasattr(resp, "messages"):
            parts: list[str] = []
            for m in resp.messages or []:
                content = getattr(m, "content", None)
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for c in content:
                        t = getattr(c, "text", None)
                        if isinstance(t, str):
                            parts.append(t)
            if parts:
                return "\n".join(parts)
        return str(resp)

    # ── 同步入口（给飞书 worker 线程用） ──
    def run_sync(self, text: str, session_id: str, timeout: float = 120.0) -> str:
        self._evict_expired()
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(self._async_run(text, session_id), loop)
        return fut.result(timeout=timeout)


# 单例
_runtime: _AgentRuntime | None = None
_runtime_lock = threading.Lock()


def get_runtime() -> _AgentRuntime:
    global _runtime
    if _runtime is not None:
        return _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = _AgentRuntime()
    return _runtime


def agent_invoke(text: str, session_id: str, timeout: float = 120.0) -> str:
    """同步入口：飞书 worker 线程调用本函数即可。"""
    return get_runtime().run_sync(text, session_id, timeout=timeout)


def reset_session(session_id: str) -> None:
    get_runtime().reset_session(session_id)
