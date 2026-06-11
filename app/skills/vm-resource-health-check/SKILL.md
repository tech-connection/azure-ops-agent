---
name: vm-resource-health-check
description: >-
  查询 Azure 虚拟机的 Resource Health 事件（平台维护 / Unavailable / Degraded 等，只读）。
  当用户询问某台 VM 的「资源运行状况 / 平台事件 / 计划维护 / 是否被重启 / Resource Health」时使用。
  典型问法：「主机 xxx 最近有什么平台事件」「这台机器最近 2 条异常事件」「是不是 Azure 侧维护」。
---

# VM Resource Health 诊断技能（标准 skill 模式 · 由 run_az 工具执行）

本技能查询一台 Azure 虚拟机的 **Resource Health** 状态与历史事件（计划维护、平台导致的
Unavailable / Degraded、重新部署等），按时间倒序列出，并区分「业务/系统问题」与「Azure 平台侧问题」。

你（模型）**不写代码、不调用任何脚本**，而是严格按本文件给出的 `az` 命令，用工具 **`run_az`**
逐条执行，拿到结果后按「判断标准」「输出格式」组装中文报告。

> 本技能**不进入 VM 操作系统内部**，只读取 Azure 控制面的 Resource Health 数据
> （Resource Health 没有原生 az 子命令，改用 `az rest` 调 ARM REST API）。

## 工具：run_az

- 作用：执行**一条只读** `az` 命令。
- 入参 `args`：是去掉开头 `az` 之后的参数数组，**每个参数一个元素**。
  例如命令 `az vm show -d -g rg -n vm -o json` 对应
  `args = ["vm","show","-d","-g","rg","-n","vm","-o","json"]`。
- 返回：命令的 JSON 结果字符串；失败时返回 `{"error": ..., "message": ...}`。
- 订阅已由后端自动注入，**普通命令里不要带 `--subscription`**；
  但 `az rest` 的 URL 里需要订阅 ID，见步骤 1。
- `run_az` 只解析 JSON，**不要用 `-o tsv`**；需要取单值也用 `-o json`。

## 参数解析（从用户消息 + 对话历史得到）

- `vm_name`：必填，逐字照抄用户主机名，结尾的 `-1`/`-01` 等是名字的一部分，不可截断。
- `resource_group`：用户给了就用用户给的；**没给则默认 `xiaomi-azure`**，不要反问客户。
  （若用默认资源组查不到该 VM，再提示用户确认资源组。）
- `top_n`：返回事件条数，默认 **5**；用户说「最近 N 条 / 最新 N 条 / 最近 2 条」就把 N 传入（上限 20）。
- 时间窗（可选，消息开头 `[当前北京时间: ...]` 即“现在”）：
  - 用户**显式**给了「近 X 分钟/小时」或具体区间 → 算出起止北京时间，后续按此过滤事件；
  - **未显式给时间范围 → 不按时间过滤**，直接取最近 `top_n` 条（健康事件稀疏，最近一条可能在数周前）。

## 执行步骤（依次调用 run_az）

### 步骤 1：取当前订阅 ID（拼 REST URL 用）

```
az account show --query id -o json
```

返回形如 `"483ab1e0-xxxx-xxxx-xxxx-xxxxxxxxxxxx"` 的字符串，记为 `<subId>`，下一步拼 URL 用。

### 步骤 2：VM 基础信息

```
az vm show -d -g <rg> -n <vm> --query "{name:name, location:location, vmSize:hardwareProfile.vmSize, osType:storageProfile.osDisk.osType, powerState:powerState}" -o json
```

从结果取：`name`、`location`、`vmSize`、`osType`、`powerState`（展示时去掉前缀 `VM ` → `running`）。

> 若结果是 `{"error": "NOT_FOUND", ...}`，说明 VM 不存在/已释放，直接如实告知用户，结束。

确认 VM 存在后，再执行一条取**当前主机名**（Guest Agent 上报的 OS 主机名，对应门户「计算机名称」，与实例 ID / 资源名不同）：

```
az vm get-instance-view -g <rg> -n <vm> --query "instanceView.computerName" -o json
```

返回形如 `"ams3-mife-fe47.aznl.idc.xiaomi.com"`，记为 `<computerName>`，填入「一、主机信息」的主机名行；取不到写 N/A。

### 步骤 3：查询 Resource Health 事件（availabilityStatuses）

用步骤 1 的 `<subId>`、参数里的 `<rg>` 与 `<vm>` 拼出 ARM REST URL（**整条 URL 是 `--url` 的单个参数元素**，
里面的 `?`、`&`、`$` 原样写，不要做 shell 转义）：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2024-02-01&$expand=recommendedactions" --query "value[].{time:properties.occuredTime, state:properties.availabilityState, title:properties.title, category:properties.category, cause:properties.healthEventCause, reason:properties.reasonType, summary:properties.summary, actions:properties.recommendedActions[].action}" -o json
```

返回一个事件数组（已按时间倒序：第 1 条通常是**当前状态**，后续是历史事件）。每条字段含义：

- `time`：事件发生时间（**UTC**，展示时 +8 小时换算成北京时间）。
- `state`：可用性状态 `Available` / `Unavailable` / `Degraded` / `Unknown`（当前状态条可能为空，此时以 `title` 为准）。
- `title`：短标题（如 `Available`、`Freeze Update Succeeded`）。
- `category`：`Planned`（计划内）/ `Unplanned`（计划外）/ `Not Applicable`。
- `cause`：`PlatformInitiated`（平台侧发起）/ `UserInitiated`（用户操作发起）/ 空。
- `reason`：原因类型（`Planned` / `Unplanned` / 空）。
- `summary`：事件说明文字。
- `actions`：建议动作列表（可能为空或为通用提示）。

### 步骤 4：按时间窗 + top_n 截取

- 若用户**显式**给了时间窗 → 先剔除 `time` 不在 [起始, 结束] 区间内的事件，再取前 `top_n` 条。
- 若用户**未**给时间窗 → 不过滤，直接取数组前 `top_n` 条。
- 始终保持时间倒序（最新在前）。

## 判断标准（写“结论”时参考）

**只看最近一条（时间倒序的第 1 条，即当前/最新状态）来定结论，不要因为历史里有过维护就判异常：**

| 结论 | 判定条件 |
| --- | --- |
| ✅ 正常 | 最近一条事件的 `state` 为 `Available`（或为已成功完成、已恢复的计划内维护）→ 说明当前没问题 |
| ❌ 异常 | 最近一条事件的 `state` 为 `Unavailable` / `Degraded`（VM 当前被判定不可用 / 降级） |

> 历史里的计划维护、Freeze/Storage 更新等只要后续已恢复 `Available`，**不影响“正常”结论**，仅作为详细数据列出供参考。
> 若用户对**历史事件**仍有疑问（如想确认某次停顿/重启的根因、影响范围），统一建议**提单 Azure Support** 进一步核查，本技能不臆断历史根因。

原因归属（写建议时用）：

- **平台侧原因**（`cause==PlatformInitiated` 或 `category==Planned`，如计划维护、Freeze/Storage 更新、平台 Unavailable）
  → 通常无需客户操作，告知客户这是 Azure 侧事件，必要时关注后续通知。
- **客户侧原因**（`cause==UserInitiated`）→ 提示是用户操作（重启 / 释放等）所致，非平台问题。
- **未知 / Degraded** → 建议结合 CPU/内存/磁盘/网络指标进一步诊断；对历史事件根因有疑问则提单 Azure Support。

## 输出格式（严格照此组装，逐项填值，不要加寒暄/表情/多余前后缀）

```
🔧 诊断模式（数据来源：Azure Resource Health 实时查询）
<诊断范围行：用户显式给了时间窗 → 「诊断时间范围：<起始北京时间> ~ <结束北京时间>（北京时间）」；未给时间窗 → 「诊断范围：最近 <top_n> 条事件（不限时间窗）」>

一、主机信息
  实例 ID：<name>
  主机名：<computerName 或 N/A>
  资源组：<resource_group>
  区域：<location>
  机型 SKU：<vmSize>
  操作系统类型：<osType 或 N/A>
  当前状态：<powerState，如 running>

二、结论
- 是否异常：<✅ 正常 / ❌ 异常>，依据**最近一条**事件简述（如：最近一条为 Available / 计划维护已恢复，当前无问题；若历史有维护可补一句已恢复不影响判断）。
- 风险判断：<低 / 中 / 高>，一句话说明（如平台计划维护已恢复，对业务无持续影响）。
- 建议动作：1) <可执行建议> 2) <可执行建议>（正常则写「无需处理，继续观察；若对历史事件仍有疑问，可提单 Azure Support 核查」）。
- 参考文档：<由你根据结论自行推荐 1 条最相关的 Microsoft Learn 官方文档链接，
  如 Resource Health 概述、计划内维护、或 VM 可用性最佳实践，不要写死链接>

———— 详细数据 ————
三、Resource Health 事件（共 <实际取到的条数> 条，按时间倒序）
  1. <事件北京时间>  状态=<state 或取 title>  类别=<category>  原因=<cause/reason，平台侧标“平台发起”、用户侧标“用户发起”>
     说明：<summary>
  2. ...
```

- 每条事件按上面缩进格式列出；`actions` 若有有意义的建议可并入「建议动作」，通用模板提示可省略。- **报告顶部的诊断范围行**：用户给了时间窗 → 写「诊断时间范围：<起> ~ <止>（北京时间）」；未给 → 写「诊断范围：最近 <top_n> 条事件（不限时间窗）」。不写采样间隔。- **机型 SKU 行只写 vmSize 名称本身**（如 `Standard_D64s_v5`）。本技能不查询 vCPU / 内存 / 最大数据盘数，**禁止**在 SKU 名后附加任何括号规格，更不得写出 `（vCPU=N/A，内存=N/A GB，最大数据盘数=N/A）` 这类占位内容。
- 时间格式 `YYYY-MM-DD HH:MM:SS`（北京时间）；取不到的值写 N/A，不臆造。
- 若数组为空（无任何事件记录）→ 第三段写「未查询到 Resource Health 事件记录」，结论按「✅ 正常」处理。
- 直接把组装好的中文报告输出给用户，**不要展示命令或 JSON**。

## 注意事项

- 本技能为**只读诊断**，不修改任何资源，不进入 VM 内部；`run_az` 只允许只读命令。
- 事件来自 Azure Resource Health；新事件可能有数分钟延迟。
- 时间一律按北京时间向用户呈现；REST 返回的 `occuredTime` 是 UTC，需 +8 小时换算。
- 主机名必填；资源组缺省时使用默认资源组 `xiaomi-azure`，不要追问。
