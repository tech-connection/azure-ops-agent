---
name: vm-memory-check
description: >-
  诊断 Azure 虚拟机的内存使用情况（基于 Azure Monitor 平台指标，只读、不进入 VM 内部）。
  当用户询问某台 VM 的「内存 / MEM / RAM」近况、内存告警触发、或反馈主机变慢且怀疑内存不足时使用。
  典型问法：「帮我查下主机 xxx 的内存」「这台机器内存够不够」。
---

# VM 内存诊断技能（标准 skill 模式 · 由 run_az 工具执行）

本技能对一台 Azure 虚拟机的内存使用情况做**只读**诊断。
你（模型）**不写代码、不调用任何脚本**，而是严格按本文件给出的 `az` 命令，
用工具 **`run_az`** 逐条执行，拿到结果后按「输出格式」组装中文报告。

> 只读取 Azure 控制面平台指标，不进入 VM 操作系统内部（不执行 run-command、不看进程列表）。
> 进程级内存占用归因不在本技能范围。

> ⚠️ 关键：Azure 平台内存指标是**「可用内存」**口径，不是「使用率」。
> **内存使用率 = 100 − 可用内存百分比**。本技能对客户统一用「内存使用率」表达。
> 内存指标依赖 VM 已启用 Azure Monitor Agent / 诊断扩展；未启用则平台无内存数据，需如实告知客户。

## 工具：run_az

- 作用：执行**一条只读** `az` 命令。
- 入参 `args`：是去掉开头 `az` 之后的参数数组，**每个参数一个元素**。
  例如命令 `az vm show -d -g rg -n vm -o json` 对应
  `args = ["vm","show","-d","-g","rg","-n","vm","-o","json"]`。
- 返回：命令的 JSON 结果字符串；失败时返回 `{"error": ..., "message": ...}`。
- 订阅已由后端自动注入，**命令里不要带 `--subscription`**。

## 参数解析（从用户消息 + 对话历史得到）

- `vm_name`：必填，逐字照抄用户主机名，结尾的 `-1`/`-01` 等是名字的一部分，不可截断。
- `resource_group`：用户给了就用用户给的；**没给则默认 `xiaomi-azure`**，不要反问客户。
  （若用默认资源组查不到该 VM，再提示用户确认资源组。）
- 时间窗（消息开头 `[当前北京时间: ...]` 即“现在”）：
  - 「近 X 分钟/小时」→ 用现在往前推得到起止时间；
  - 「9 点到 10 点」「某区间」→ 用用户给的起止；
  - 未提供 → 默认近 30 分钟。
  - 计算出 `start` / `end` 后**换算成 UTC**（北京时间 − 8 小时），格式 `YYYY-MM-DDTHH:MM:SSZ`。

## 执行步骤（依次调用 run_az）

### 步骤 1：VM 基础信息

```
az vm show -d -g <resource_group> -n <vm_name> -o json
```

`args = ["vm","show","-d","-g","<rg>","-n","<vm>","-o","json"]`

从结果取：`name`、`location`、`hardwareProfile.vmSize`、`storageProfile.osDisk.osType`、
`powerState`（形如 `VM running`，展示时去掉前缀 `VM ` → `running`）、`id`
（资源 ID，形如 `/subscriptions/<subId>/resourceGroups/...`，下一步取其中的 `<subId>`）。
若结果是 `{"error": "NOT_FOUND", ...}`，说明 VM 不存在/已释放，直接如实告知用户，结束。

确认 VM 存在后，再执行一条取**当前主机名**（Guest Agent 上报的 OS 主机名，对应门户「计算机名称」，与实例 ID / 资源名不同）：

```
az vm get-instance-view -g <resource_group> -n <vm_name> --query "instanceView.computerName" -o json
```

`args = ["vm","get-instance-view","-g","<rg>","-n","<vm>","--query","instanceView.computerName","-o","json"]`

返回形如 `"ams3-mife-fe47.aznl.idc.xiaomi.com"`，记为 `<computerName>`，填入「一、主机信息」的主机名行；取不到写 N/A。

### 步骤 2：SKU 规格（vCPU / 内存）

先从步骤 1 的 `id` 里取 `<subId>`（`/subscriptions/` 之后、下一个 `/` 之前那段），
再用 `<location>`、`<vmSize>` 调 Compute SKUs API（服务端按 location 过滤，比 `az vm list-skus` 快几十倍）：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/providers/Microsoft.Compute/skus?api-version=2021-07-01&$filter=location eq '<location>'" --query "value[?name=='<vmSize>'] | [0].capabilities[?name=='vCPUs' || name=='MemoryGB'].{name:name,value:value}" -o json
```

`args = ["rest","--method","get","--url","https://management.azure.com/subscriptions/<subId>/providers/Microsoft.Compute/skus?api-version=2021-07-01&$filter=location eq '<location>'","--query","value[?name=='<vmSize>'] | [0].capabilities[?name=='vCPUs' || name=='MemoryGB'].{name:name,value:value}","-o","json"]`

返回形如 `[{"name":"vCPUs","value":"32"},{"name":"MemoryGB","value":"64"}]`。
取 `vCPUs`、`MemoryGB` 对应的 value，填入「一、主机信息」的机型 SKU 行。**这一步必须执行**，不得跳过或臆造；只有当调用确实返回空时才写 N/A。记下 `MemoryGB` 备用。

### 步骤 3：内存使用率峰值（可用内存最低的一分钟）

```
az monitor metrics list --resource <vm_name> --resource-group <resource_group> \
  --resource-namespace Microsoft.Compute --resource-type virtualMachines \
  --metric "Available Memory Percentage" --start-time <start_utc> --end-time <end_utc> \
  --interval PT1M --aggregation Minimum \
  --query "sort_by(value[0].timeseries[0].data[?minimum!=null], &minimum)[0]" -o json
```

`args = ["monitor","metrics","list","--resource","<vm>","--resource-group","<rg>","--resource-namespace","Microsoft.Compute","--resource-type","virtualMachines","--metric","Available Memory Percentage","--start-time","<start_utc>","--end-time","<end_utc>","--interval","PT1M","--aggregation","Minimum","--query","sort_by(value[0].timeseries[0].data[?minimum!=null], &minimum)[0]","-o","json"]`

结果形如 `{"timeStamp": "...", "minimum": 83.0}`（可用百分比的最小值）。
**内存使用率峰值 = 100 − minimum**（如 100 − 83.0 = 17.0%）；`timeStamp`+8 小时为峰值北京时间。

### 步骤 4：内存使用率最低（可用内存最高的一分钟）

```
az monitor metrics list --resource <vm_name> --resource-group <resource_group> \
  --resource-namespace Microsoft.Compute --resource-type virtualMachines \
  --metric "Available Memory Percentage" --start-time <start_utc> --end-time <end_utc> \
  --interval PT1M --aggregation Maximum \
  --query "reverse(sort_by(value[0].timeseries[0].data[?maximum!=null], &maximum))[0]" -o json
```

`args = ["monitor","metrics","list","--resource","<vm>","--resource-group","<rg>","--resource-namespace","Microsoft.Compute","--resource-type","virtualMachines","--metric","Available Memory Percentage","--start-time","<start_utc>","--end-time","<end_utc>","--interval","PT1M","--aggregation","Maximum","--query","reverse(sort_by(value[0].timeseries[0].data[?maximum!=null], &maximum))[0]","-o","json"]`

结果形如 `{"timeStamp": "...", "maximum": 83.0}`（可用百分比的最大值）。
**内存使用率最低 = 100 − maximum**；`timeStamp`+8 小时为对应北京时间。

### 步骤 5：均值 + 高位占比（一次调用拿三个数，判断“持续性”）

只看峰值会把“偶发尖刺”误判为异常、把“长期高位但没破阈值”漏判。
本步用**一次**查询同时取：可用百分比均值、可用 ≤10%（即使用率 ≥90%）的分钟数、有效总分钟数。

```
az monitor metrics list --resource <vm_name> --resource-group <resource_group> \
  --resource-namespace Microsoft.Compute --resource-type virtualMachines \
  --metric "Available Memory Percentage" --start-time <start_utc> --end-time <end_utc> \
  --interval PT1M --aggregation Average Minimum \
  --query "{avg_avail: avg(value[0].timeseries[0].data[?average!=null].average), high_used_minutes: length(value[0].timeseries[0].data[?minimum<=\`10\`]), total_minutes: length(value[0].timeseries[0].data[?minimum!=null])}" -o json
```

`args = ["monitor","metrics","list","--resource","<vm>","--resource-group","<rg>","--resource-namespace","Microsoft.Compute","--resource-type","virtualMachines","--metric","Available Memory Percentage","--start-time","<start_utc>","--end-time","<end_utc>","--interval","PT1M","--aggregation","Average","Minimum","--query","{avg_avail: avg(value[0].timeseries[0].data[?average!=null].average), high_used_minutes: length(value[0].timeseries[0].data[?minimum<=\`10\`]), total_minutes: length(value[0].timeseries[0].data[?minimum!=null])}","-o","json"]`

结果形如 `{"avg_avail": 83.0, "high_used_minutes": 0, "total_minutes": 88}`。
- **内存使用率均值 = 100 − avg_avail**（如 100 − 83.0 = 17.0%）。
- **高位占比** = `high_used_minutes / total_minutes`（你自己做这一次除法，换算成百分比；`total_minutes` 为 0 时占比记为 0）。
  例：`high_used_minutes=25, total_minutes=30` → 高位占比 ≈ 83%。

### 步骤 6：运行状况（Resource Health，判断异常是否与底层平台有关）

时间窗与上面内存查询**保持一致**。先从步骤 1 的 `id` 里取 `<subId>`（`/subscriptions/` 之后、下一个 `/` 之前那段），调 ARM REST（整条 URL 是 `--url` 的单个元素，`?`/`&`/`$` 原样写）：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/resourceGroups/<resource_group>/providers/Microsoft.Compute/virtualMachines/<vm_name>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2024-02-01&$expand=recommendedactions" --query "value[].{time:properties.occuredTime, state:properties.availabilityState, title:properties.title, cause:properties.healthEventCause, summary:properties.summary}" -o json
```

数组已按时间倒序，**第 1 条为当前/最新状态**；`time` 是 UTC，+8 小时换算北京时间。
- **当前平台状态** = 第 1 条的 `state`（`Available` = 平台侧正常）。
- **诊断窗内是否有平台事件**：逐条看 `time`，只要有任意一条落在本次诊断时间窗 `[start, end]` 内，即「窗内有平台事件」（计划维护 / Unavailable / Degraded 等）。
- **用途**：内存指标若判为异常，且窗内有平台事件或当前非 `Available` → 在结论里点明异常**可能与底层平台有关**；若窗内无事件且当前 `Available` → 可排除平台因素，问题更可能在业务/系统侧。

> 数据缺失处理：若步骤 3/4/5 返回 `null` / 空 / `{"error": ...}`（多为 VM 未启用 Azure Monitor Agent，
> 平台无 `Available Memory Percentage` 指标），相应数值显示 N/A，并在结论里明确告知客户
> “该 VM 未采集到内存指标，请确认是否已启用 Azure Monitor Agent / 诊断扩展”。

## 判断阈值（写“结论”时参考）

只分**正常 / 异常**两档（不设「关注」中间态）。以**高位占比**（内存使用率 ≥ 90% 的分钟占比）为准，偏高且持续才算异常（内存比 CPU 更敏感，吃满会触发换页/OOM）：

- **✅ 正常**：高位（≥90%）占比 < 20%。
- **❌ 异常**：高位（≥90%）占比 ≥ 20%（内存长期吃紧，有换页/OOM 风险）。
  建议结合业务排查进程内存占用，或考虑升配到更大内存规格。

> 要点：单分钟使用率高 ≠ 异常；**高位是否持续**（≥90% 占比）才是关键；均值仅作参考展示。

## 输出格式（严格照此组装，逐项填值，不要加寒暄/表情/多余前后缀）

```
🔧 诊断模式（数据来源：Azure Monitor + Resource Health 实时查询）
诊断时间范围：<起始北京时间> ~ <结束北京时间>（采样间隔 1 分钟，北京时间）

一、主机信息
  实例 ID：<name>
  主机名：<computerName 或 N/A>
  资源组：<resource_group>
  区域：<location>
  机型 SKU：<vmSize>（vCPU=<n>，内存=<x.x> GB）
  操作系统类型：<osType 或 N/A>
  当前状态：<powerState，如 running>

二、结论
- 是否异常：<✅ 正常 / ❌ 异常>，并简述依据（高位占比多少、内存使用率均值多少、峰值多少）。
- 平台关联：<仅在内存判异常时写：诊断窗内有平台事件/当前非 Available → 可能与底层平台有关；无事件且 Available → 已排除平台因素。内存正常时此行可省略>
- 风险判断：<低 / 中 / 高>，一句话说明（如当前内存充裕，运行稳定）。
- 建议动作：1) <可执行建议> 2) <可执行建议>
- 参考文档：<由你根据结论自行推荐 1 条最相关的 Microsoft Learn 官方文档链接，
  不要写死；例如内存吃紧可给 VM 规格/升配文档，正常可给监控指标文档>

———— 详细数据 ————
三、内存指标
  内存使用率峰值：<峰值>%  时间=<峰值北京时间>
  内存使用率最低：<谷值>%  时间=<谷值北京时间>
  内存使用率均值：<均值>%
  高位占比（≥90%）：<高位占比>%（<high_used_minutes>/<total_minutes> 分钟）

四、运行状况（Resource Health）
  当前平台状态：<Available=正常 / Unavailable / Degraded / Unknown>
  诊断窗内平台事件：<无 / 有：简述最相关一条（北京时间 + 计划内/计划外 + 简要说明）>
```

- 数值保留 1 位小数（如 `17.0`）。
- 时间格式 `YYYY-MM-DD HH:MM:SS`。
- **「一、主机信息」的机型 SKU 行必须完整列出括号内的 vCPU / 内存**（来自步骤 2），不得只写 SKU 名而省略规格；取不到才写 N/A。
- 不臆造数据：所有数值必须来自 run_az 的真实返回；取不到就写 N/A。

## 注意事项

- 本技能为**只读诊断**，不修改任何资源、不进入 VM 内部；`run_az` 只允许只读命令。
- 时间一律按北京时间展示，与 az 交互时换算成 UTC。
- 平台内存指标为「可用」口径，对客户呈现时务必换算成「使用率 = 100 − 可用百分比」。
- 内存指标依赖 Azure Monitor Agent / 诊断扩展；未启用时如实告知客户，不要编造数据。
