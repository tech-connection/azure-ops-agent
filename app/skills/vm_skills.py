"""VM 诊断 skill 的注册入口。

所有 skill 均为纯 content 型：只提供各自目录下的 SKILL.md（含可执行的 az 命令清单），
不绑定脚本。模型按标准 skill 流程 load_skill 读取正文，再用全局只读工具 `run_az`
执行 SKILL.md 里写明的 az 命令并产出报告——命令不写死在代码里，完全以 SKILL.md 为准。
  app/skills/vm-cpu-check/SKILL.md
  app/skills/vm-memory-check/SKILL.md
  ...共七个。

本文件只负责把每个 skill 目录装配成 Microsoft Agent Framework 的 Skill 对象：
  - SKILL.md 的 frontmatter 提供 name / description（用于技能广告与路由）；
  - SKILL.md 正文作为 content（给 LLM 的“何时用 / 流程 / 判断标准”说明）。

非 VM 的指标查询与 Azure 通识问答都走 azure-qa（合并了原 vm-unsupported-metric 的能力边界兜底）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from agent_framework import Skill

logger = logging.getLogger(__name__)


# 社区标准 SKILL.md 目录所在位置（与本文件同级）
_SKILLS_DIR = Path(__file__).resolve().parent


def _content_skill_from_md(dir_name: str) -> Skill:
    """纯 content 型 Skill：只提供 SKILL.md（含可执行 az 命令清单）。

    模型按标准 skill 流程 load_skill 读取正文，再用全局只读工具 `run_az`
    执行 SKILL.md 里写明的 az 命令并产出报告——命令不写死在代码里，完全以 SKILL.md 为准。
    """
    from agent_framework._skills import _read_and_parse_skill_file

    parsed = _read_and_parse_skill_file(str(_SKILLS_DIR / dir_name))
    if parsed is None:
        raise RuntimeError(f"加载 SKILL.md 失败：{dir_name}（检查 frontmatter 是否合法）")
    name, description, content = parsed
    return Skill(name=name, description=description, content=content)


# ─────────────────── 注册到 Microsoft Agent Framework ───────────────────
def build_vm_skills() -> list[Skill]:
    """构造 9 个纯 content 型 Skill 对象，注册给 SkillsProvider。

    全部为 content 型：文档为社区标准 SKILL.md（位于 app/skills/<name>/SKILL.md，
    可交付、客户可编辑），执行由模型按 SKILL.md 写明的 az 命令经全局 `run_az` 工具完成，
    不绑定 check.py 脚本。每个 Skill 的 name/description/content 由 SKILL.md 的 frontmatter 与正文解析得到。
    """
    return [
        _content_skill_from_md("vm-cpu-check"),  # 标准 skill 模式：SKILL.md 命令 + run_az 工具，不绑脚本
        _content_skill_from_md("vm-memory-check"),  # 同上：内存诊断走 SKILL.md + run_az
        _content_skill_from_md("vm-disk-check"),  # 同上：磁盘诊断走 SKILL.md + run_az（含内置 SKU 上限表）
        _content_skill_from_md("vm-network-check"),  # 同上：网络诊断走 SKILL.md + run_az（含连接数上限表）
        _content_skill_from_md("vm-resource-health-check"),  # 同上：Resource Health 走 SKILL.md + run_az（az rest 调 ARM REST API）
        _content_skill_from_md("service-health-check"),  # 同上：订阅级 Service Health 平台事件（az rest 调 ResourceHealth/events，判断是否有平台级故障）
        _content_skill_from_md("vm-full-diagnosis"),  # 同上：整体体检走 SKILL.md + run_az（合并编排五维指标，共享步骤只跑一次）
        _content_skill_from_md("azure-qa"),  # 轻量：SKILL.md 驱动的通识问答 + 能力边界兜底（合并自原 vm-unsupported-metric），LLM 直接回答，无脚本
        _content_skill_from_md("azure-support-case"),  # 提交 Azure 支持工单：SKILL.md 驱动，用户确认后经 run_az 调 az support 建单（唯一受控写操作）
    ]
