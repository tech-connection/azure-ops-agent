---
name: service-health-check
description: >-
  查询 Azure 订阅的 Service Health 平台事件（服务故障 / 计划维护 / 健康公告 / 安全公告，只读），
  确认是否存在 Azure 平台级别的故障（不针对单台资源）。当用户询问「Azure 是不是挂了 / 平台有没有故障 /
  service health / 服务运行状况 / 最近几天有没有平台事件 / 当前有没有活跃的 outage / 某区域是否中断」时使用。
  典型问法：「最近 2 天 Azure 有没有平台故障」「现在有没有活跃的 outage」「East Asia 区域是不是出问题了」
  「今天有没有 VM 相关故障」「今天有没有 AOAI / OpenAI / 存储 相关故障」。
---

# Azure Service Health 诊断技能（标准 skill 模式 · 由 run_az 工具执行）

本技能查询**订阅级别**的 Azure Service Health 事件——平台侧的服务问题（Service Issue / Outage）、
计划维护（Planned Maintenance）、健康公告（Health Advisory）、安全公告（Security Advisory）等，
用于判断「**是不是 Azure 平台自己出故障了**」，与单台资源的 Resource Health 不同（那是查具体 VM）。

你（模型）**不写代码、不调用任何脚本**，而是严格按本文件给出的 `az` 命令，用工具 **`run_az`**
逐条执行，拿到结果后按「判断标准」「输出格式」组装中文报告。

> 本技能查的是**整个订阅范围**的平台健康事件（Service Health 没有原生 az 子命令，用 `az rest` 调 ARM REST API）。
> 它**不针对某一台 VM**；若用户问的是「某台主机当前是否可用 / 被重启 / 计划维护」，应改用 vm-resource-health-check。

## 工具：run_az

- 作用：执行**一条只读** `az` 命令。
- 入参 `args`：去掉开头 `az` 之后的参数数组，**每个参数一个元素**。
  例如 `az account show --query id -o json` 对应 `args = ["account","show","--query","id","-o","json"]`。
- 返回：命令的 JSON 结果字符串；失败时返回 `{"error": ..., "message": ...}`。
- 订阅已由后端自动注入，**普通命令里不要带 `--subscription`**；但 `az rest` 的 URL 里需要订阅 ID（见步骤 1）。
- `run_az` 只解析 JSON，**不要用 `-o tsv`**。

## 参数解析（从用户消息 + 对话历史得到）

- 时间窗（消息开头 `[当前北京时间: ...]` 即“现在”）：
  - 「最近 X 天 / X 小时」「近一周」「某区间」→ 据此算出起止**北京时间**，记为 `[起, 止]`；
  - 「当前活跃 / 现在有没有 outage / 正在进行的故障」→ 视为**只看活跃事件**（`status==Active`），时间窗可不限；
  - **未显式给时间范围 → 默认最近 7 天**（同时把仍 `Active` 的事件一并纳入，哪怕开始于 7 天前）。
  - 把起止北京时间各 **− 8 小时换算成 UTC**，格式与 CPU 技能一致：`YYYY-MM-DDTHH:MM:SSZ`
    （例 `2026-06-11T00:00:00Z`）。**该 UTC 时间直接裸写进 OData `$filter`，前后不加引号**
    （OData 里 datetime 是字面量、不加引号；只有字符串值如 `'Active'` 才加单引号）。
- 可选过滤条件（用户提到才用，仅用于「命中后筛选/呈现」，不必写进 URL）：
  - **区域**（如 `East Asia` / `Southeast Asia`）→ 只呈现 `impact` 命中该区域的事件；
  - **服务**（如 `Virtual Machines` / `Storage`）→ 只呈现命中该服务的事件；
  - **事件类型**（服务故障 / 计划维护 / 公告）→ 只呈现对应 `eventType`。

## 执行步骤（依次调用 run_az）

### 步骤 1：取当前订阅 ID（拼 REST URL 用）

```
az account show --query id -o json
```

返回形如 `"483ab1e0-xxxx-xxxx-xxxx-xxxxxxxxxxxx"` 的字符串，记为 `<subId>`，下一步拼 URL 用。

### 步骤 2：查询 Service Health 事件（events）

用步骤 1 的 `<subId>` 与「参数解析」算出的起始 UTC `<start-utc>` 拼出 ARM REST URL（**整条 URL 是 `--url`
的单个参数元素**，里面的 `?`、`&`、`$` 原样写，不要做 shell 转义）。用 `properties/impactStartTime ge`
做服务端粗过滤，减少返回量；更精确的「命中时间窗」由步骤 3 在结果上判断：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/providers/Microsoft.ResourceHealth/events?api-version=2022-10-01&$filter=properties/impactStartTime ge <start-utc>" --query "value[].{type:properties.eventType, status:properties.status, level:properties.level, title:properties.title, start:properties.impactStartTime, mitigate:properties.impactMitigationTime, updated:properties.lastUpdateTime, summary:properties.summary, impact:properties.impact[].{service:impactedService, regions:impactedRegions[].impactedRegion}}" -o json
```

> **OData 过滤格式要点**：`<start-utc>` 用 `YYYY-MM-DDTHH:MM:SSZ` 形式**裸写、不加引号**
> （如 `...impactStartTime ge 2026-06-11T00:00:00Z`）。给时间值加单引号会报 `InvalidODataQueryOptions`。
> 字段名要带 `properties/` 前缀（events 接口不支持按 `lastUpdateTime` 过滤）。
> 若用户**只问当前活跃 outage**，可改用 `$filter=properties/status eq 'Active'`（不带时间；`'Active'`
> 是字符串、需加单引号），其余字段不变。
> 若仍返回 `InvalidODataQueryOptions` 或其它过滤报错，可去掉整个 `&$filter=...`（取全量后由步骤 3 在结果上筛时间窗）。
> 若返回 `{"error": ...}`（如订阅无权限读 ResourceHealth），如实告知用户并结束。

返回一个事件数组。每条字段含义：

- `type`（`eventType`）：`ServiceIssue`（服务问题 / 故障 / Outage）、`PlannedMaintenance`（计划维护）、
  `HealthAdvisory`（健康公告）、`SecurityAdvisory`（安全公告）、`RCA`（根因分析报告）。
- `status`：`Active`（进行中）/ `Resolved`（已解决）。
- `level`：严重级别（如 `Warning` / `Error` / `Critical` / `Informational`）。
- `title`：事件标题。
- `start`（`impactStartTime`）：影响开始时间（**UTC**，+8 小时换算北京时间）。
- `mitigate`（`impactMitigationTime`）：影响缓解 / 结束时间（**UTC**；为空表示**尚未结束 / 仍在进行**）。
- `updated`（`lastUpdateTime`）：最近更新时间（UTC，+8 小时换算）。
- `summary`：事件说明（可能含 HTML，呈现时取纯文本要点即可）。
- `impact`：受影响的服务与区域列表（`service` + `regions`）。

### 步骤 3：按时间窗 + 可选条件筛出「命中」事件

对每条事件判断是否**命中本次诊断窗** `[起, 止]`（北京时间口径，与事件时间换算后比较）：

- **活跃事件**（`status==Active`）→ **始终命中**（当前仍在进行，无论开始多早）。
- **已解决事件**（`status==Resolved`）→ 当其影响区间 `[start, mitigate]` 与查询窗 `[起, 止]` **有交集**即命中
  （`mitigate` 为空按“至今”处理）。
- 若用户给了区域 / 服务 / 类型过滤 → 在命中基础上再筛 `impact` 命中对应区域/服务、或 `type` 对应类型的事件。
- **没命中任何事件**（数组为空或筛完为空）→ 说明该时间窗内订阅范围无平台事件，结论按「✅ 无平台级故障」。

按 `updated`（或 `start`）**时间倒序**排列命中事件，最新在前。

## 判断标准（写“结论”时参考）

只分**无 / 有 平台级故障**两档，**只看“当前是否还在发生”**——即仅 `status==Active` 的服务问题（`ServiceIssue`）
才算异常；**已解决（`Resolved`）的服务问题视为正常**，只作为历史事件列在详细数据里供对齐时间，不计入故障判定：

| 结论 | 判定条件 |
| --- | --- |
| ✅ 无平台级故障 | 命中事件里**没有活跃的** `ServiceIssue`（`Active`）。即便有已解决的服务问题、计划维护、健康/安全公告，都算正常 |
| ❌ 有平台级故障 | 命中事件里存在**活跃**（`status==Active`）的 `ServiceIssue`，即平台**当前正在发生**故障 / Outage |

- **活跃服务问题**（`ServiceIssue` 且 `Active`）→ 平台正在发生故障，影响对应服务/区域；告知用户这是 Azure 侧问题，
  建议关注 Service Health 门户的实时更新，业务侧通常只能等待平台缓解或做跨区域容灾切换。
- **已解决服务问题**（`ServiceIssue` 且 `Resolved`）→ **按正常处理**：平台曾发生故障但已恢复，当前不影响业务。
  在结论里以一句话提示“该时间窗内曾发生 N 条已恢复的服务问题（当前已缓解）”，并把它们列在详细数据里；
  若用户当时受影响，可据 `start`/`mitigate` 对齐业务异常时间，必要时凭 `trackingId` 提单 Azure Support 索取 RCA。
- **计划维护 / 公告**（`PlannedMaintenance` / `HealthAdvisory` / `SecurityAdvisory`）→ 不是故障，作为提示列出，
  提醒用户关注可能的维护窗口或所需操作。
- 本技能不臆断根因；用户要根因报告时建议查看对应事件的 RCA 或提单 Azure Support。

## 输出格式（严格照此组装，逐项填值，不要加寒暄/表情/多余前后缀）

```
🔧 诊断模式（数据来源：Azure Service Health 实时查询）
诊断时间范围：<起始北京时间> ~ <结束北京时间>（北京时间）

一、结论
- 是否存在平台级故障：<✅ 无 / ❌ 有>。<一句话：判定**只看当前活跃**的服务问题——
  有活跃 `ServiceIssue` 才写“❌ 有，平台当前正在发生故障”；否则写“✅ 无，平台当前无活跃故障”，
  若期间有已解决的服务问题再补一句“该时间窗内曾发生 <m> 条已恢复的服务问题（当前已缓解，不影响当前业务）”>
- 影响范围：<有**活跃**故障时列出受影响的服务与区域；当前无活跃故障则写“不适用（当前无活跃平台故障）”>
- 建议动作：1) <可执行建议，如关注 Service Health 门户实时更新 / 评估跨区域切换> 2) <…>
  （无故障则写“无需处理；如业务仍异常，建议改用资源级 Resource Health 或指标诊断进一步排查”）
- 参考：Azure Service Health 门户（https://portal.azure.com/#view/Microsoft_Azure_Health/AzureHealthBrowseBlade）

———— 详细数据 ————
二、Service Health 事件（命中 <实际命中条数> 条，按时间倒序）
  1. [<类型中文：服务问题/计划维护/健康公告/安全公告/RCA> · <状态：进行中/已解决> · <level>]  <title>
     影响时间：<start 北京时间> ~ <mitigate 北京时间，未结束则写“进行中”>
     受影响：服务=<service，多个用中文逗号分隔> ；区域=<regions，多个用中文逗号分隔>
     受影响细节：<从 title / summary 中提取的具体受影响对象——模型/版本（如 gpt-4.1、gpt-4o）、
       功能/能力（如 image generation、embeddings、fine-tuning）、API/SKU 等；多个用中文逗号分隔；无则写“无更细粒度信息”>
     最近更新：<updated 北京时间>
     说明：<summary 译成简体中文后的纯文本要点；专有名词/型号/区域名（如 Sora 2、gpt-4.1、East Asia）保留原文>
  2. ...
```

- 时间格式 `YYYY-MM-DD HH:MM:SS`（北京时间）；UTC 一律 +8 小时换算后再呈现。取不到的值写 N/A，不臆造。
- **诊断时间范围行**：填本次查询窗的起止北京时间，格式 `YYYY-MM-DD HH:MM:SS ~ YYYY-MM-DD HH:MM:SS（北京时间）`，
  与 CPU 等指标技能完全一致。仅当用户「只问当前活跃 outage、未给任何时间窗」时，改写为「当前活跃事件（不限时间窗）」。
  若带区域/服务过滤，可在该行末尾补「，过滤：区域=East Asia / 服务=Virtual Machines」。
- **不写“采样间隔 / 采集粒度”**——Service Health 是事件型数据，不是按分钟采样的指标。
- **受影响细节行**：很多事件会在 `title` / `summary` 里点名具体的受影响对象（如模型 `gpt-4.1` / `gpt-4o`、
  功能 `image generation` / `embeddings`、某 API 或 SKU）。务必把这些**具体名词原样**抽出来填到「受影响细节」行，
  方便用户判断是否命中自己用到的型号/能力；若 summary 只是泛指、没有更细信息，则写“无更细粒度信息”。
  结论的「影响范围」也可在服务/区域后追加一句概括（如“主要影响 gpt-4.1 图像生成能力”）。
- 若命中为空 → 第二段写「该时间窗内订阅范围无 Service Health 事件」，结论按「✅ 无平台级故障」。
- `summary` 可能含 HTML 标签，呈现时去掉标签、保留要点文字，不要原样贴标签。
- **`summary` 与 `title` 原文多为英文 → 「说明」必须翻译成简体中文要点输出**，不要直接贴英文原文；
  但专有名词、模型/版本号、区域名、API 名等（如 `Sora 2`、`gpt-4.1`、`East Asia`、`image generation`）保留原文不译。
- 直接把组装好的中文报告输出给用户，**不要展示命令或 JSON**。

## 注意事项

- 本技能为**只读诊断**，不修改任何资源，不进入任何 VM 内部；`run_az` 只允许只读命令。
- 数据为**订阅级**平台健康事件；不同订阅看到的事件可能不同；新事件可能有数分钟延迟。
- 时间一律按北京时间向用户呈现；REST 返回的时间是 UTC，需 +8 小时换算。
- 这是「Azure 平台是否故障」的判断；单台资源（某 VM 是否可用 / 被维护）请用 vm-resource-health-check。
