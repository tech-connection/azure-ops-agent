---
name: vm-full-diagnosis
description: >-
  对 Azure 虚拟机做一次整体健康体检：一次性采集 CPU / 内存 / 磁盘 / 网络 / 运行状况（Resource Health）
  五类指标（只读），汇总成一份综合报告。当用户要求「诊断 / 体检 / 整体排查 / 看看 xxx 主机有没有问题 /
  全面检查」某台 VM 时使用。典型问法：「诊断下主机 xxx」「这台机器整体有没有异常」「全面体检一下」。
---

# VM 整体诊断技能（标准 skill 模式 · 由 run_az 工具执行）

本技能对一台 Azure 虚拟机做一次**全面只读体检**：采集 CPU、内存、磁盘、网络、运行状况
五类数据，汇总成一份综合报告，结论第一行用一行汇总五个维度是否异常，便于快速定位。

你（模型）**不写代码、不调用任何脚本**，而是严格按本文件给出的 `az` 命令，用工具 **`run_az`**
逐条执行，拿到结果后按「判断标准」「输出格式」组装中文报告。

> 本技能**不进入 VM 操作系统内部**，只读取 Azure 控制面的平台指标与健康事件。
> 它是单项技能（CPU / 内存 / 磁盘 / 网络 / Resource Health）的合集，但**共享步骤只跑一次**（`az vm show`、
> SKU 查询各一次），各维度指标合并查询，比逐个跑单项技能轻得多。
> 若用户**明确只问某一类**（如只问 CPU、只问磁盘），用对应单项技能更快更省，不要用本技能。

## 工具：run_az

- 作用：执行**一条只读** `az` 命令。
- 入参 `args`：去掉开头 `az` 之后的参数数组，**每个参数一个元素**。
  例如 `az vm show -d -g rg -n vm -o json` 对应 `args = ["vm","show","-d","-g","rg","-n","vm","-o","json"]`。
- 返回：命令的 JSON 结果字符串；失败时返回 `{"error": ..., "message": ...}`。
- 订阅已由后端自动注入，**普通命令不要带 `--subscription`**；但 `az rest` 的 URL 里需要订阅 ID（见步骤 7）。
- `run_az` 只解析 JSON，**不要用 `-o tsv`**。

## 参数解析（从用户消息 + 对话历史得到）

- `vm_name`：必填，逐字照抄用户主机名，结尾的 `-1`/`-01` 等是名字的一部分，不可截断。
- `resource_group`：用户给了就用；**没给则默认 `xiaomi-azure`**，不要反问。
- 时间窗（消息开头 `[当前北京时间: ...]` 即“现在”）：
  - 「近 X 分钟/小时」→ 用现在往前推；具体区间 → 用用户给的起止；未提供 → 默认近 30 分钟。
  - 算出 `start` / `end` 后**换算成 UTC**（北京时间 − 8 小时），格式 `YYYY-MM-DDTHH:MM:SSZ`。

## 执行步骤（依次调用 run_az；步骤 1、2 是五个维度共享的基础数据，只跑一次）

### 步骤 1：VM 基础信息 + 盘列表 + 主网卡（共享）

```
az vm show -d -g <rg> -n <vm> --query "{id:id, name:name, location:location, vmSize:hardwareProfile.vmSize, osType:storageProfile.osDisk.osType, powerState:powerState, osDisk:storageProfile.osDisk.{name:name, sku:managedDisk.storageAccountType}, dataDisks:storageProfile.dataDisks[].{lun:lun, name:name, sku:managedDisk.storageAccountType}, nics:networkProfile.networkInterfaces[].{id:id, primary:primary}}" -o json
```

取：`name`、`location`、`vmSize`、`osType`、`powerState`（展示去前缀 `VM ` → `running`）、OS 盘名与 `sku`、各数据盘的 `lun` / 盘名 / `sku`、主网卡 `id`（优先 `primary==true`，否则第一张）、顶层 `id`（资源 ID，形如 `/subscriptions/<subId>/resourceGroups/...`，步骤 2 与 7 都从中取 `<subId>`）。

> ⚠️ **NIC 资源 id 必须从本命令返回的 `nics[].id` 原值逐字照抄**（包括大小写）。Azure 资源名**区分大小写**，且 NIC 名与 VM 名没有固定命名规律（常见如 `<vm名小写>_z1`，但不可靠）。**绝不要用 VM 名拼接 / 小写化去“猜” NIC 名，也不要在本命令返回前就发起任何依赖 NIC id 的命令（步骤 5a / 5c 的 AC 分支）**——否则会 NOT_FOUND。

确认 VM 存在后，再执行一条取**当前主机名**（Guest Agent 上报的 OS 主机名，对应门户「计算机名称」，与实例 ID / 资源名不同）：

```
az vm get-instance-view -g <rg> -n <vm> --query "instanceView.computerName" -o json
```

返回形如 `"ams3-mife-fe47.aznl.idc.xiaomi.com"`，记为 `<computerName>`，填入「一、主机信息」的主机名行；取不到写 N/A。

> 若结果是 `{"error": "NOT_FOUND", ...}` → VM 不存在/已释放，如实告知用户并结束。
> 若 `powerState` 不是 `VM running` → 指标可能缺失，照常采集但在结论里说明。

### 步骤 2：SKU 规格 + 磁盘 VM 级未缓存上限（共享）

先从步骤 1 的顶层 `id` 里取 `<subId>`（`/subscriptions/` 之后、下一个 `/` 之前那段），再调 Compute SKUs API（服务端按 location 过滤，比 `az vm list-skus` 快几十倍；整条 URL 是 `--url` 的单个元素，`?`/`&`/`$` 原样写）：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/providers/Microsoft.Compute/skus?api-version=2021-07-01&$filter=location eq '<loc>'" --query "value[?name=='<vmSize>'] | [0].{vCPUs: capabilities[?name=='vCPUs'].value | [0], MemoryGB: capabilities[?name=='MemoryGB'].value | [0]}" -o json
```

> 返回一个对象，例如 `{"vCPUs":"64","MemoryGB":"256"}`。

取两个 value：`vCPUs`、`MemoryGB`（→ 主机信息机型 SKU 行）。`vCPUs` 同时用于网络连接数落档。**这一步必须执行，机型 SKU 行不得省略规格、不得臆造**。（磁盘维度改用消耗百分比指标判断触顶，不再需要 VM 未缓存上限数值。）

### 步骤 3：CPU（一次拿峰值/均值/高位占比）

```
az monitor metrics list --resource <vm> --resource-group <rg> --resource-namespace Microsoft.Compute --resource-type virtualMachines --metric "Percentage CPU" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Average Maximum --query "{avg: avg(value[0].timeseries[0].data[?average!=null].average), peak: max(value[0].timeseries[0].data[?maximum!=null].maximum), high_minutes: length(value[0].timeseries[0].data[?maximum>=\`90\`]), total_minutes: length(value[0].timeseries[0].data[?maximum!=null])}" -o json
```

得 `avg`（均值）、`peak`（峰值%）、`high_minutes`、`total_minutes`。**高位占比 = high_minutes / total_minutes ×100%**（total 为 0 记 0）。

### 步骤 4：内存（一次拿峰值/均值/高位占比）

```
az monitor metrics list --resource <vm> --resource-group <rg> --resource-namespace Microsoft.Compute --resource-type virtualMachines --metric "Available Memory Percentage" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Average Minimum --query "{avg_avail: avg(value[0].timeseries[0].data[?average!=null].average), min_avail: min(value[0].timeseries[0].data[?minimum!=null].minimum), high_used_minutes: length(value[0].timeseries[0].data[?minimum<=\`10\`]), total_minutes: length(value[0].timeseries[0].data[?minimum!=null])}" -o json
```

平台是「可用」口径，换算成「使用率」：**内存使用率均值 = 100 − avg_avail**，**峰值 = 100 − min_avail**。**高位占比 = high_used_minutes / total_minutes ×100%**。
> 若返回 null/空/error（多为未装 Azure Monitor Agent）→ 内存各值写 N/A，结论该维度标「N/A（未采集到内存指标）」，不臆造。

### 步骤 5：网络（主网卡 AC 配置 + 带宽 + 连接数）

> ⚠️ **本步骤的 NIC 命令（5a 与 5c 的 AC 分支）依赖步骤 1 返回的真实 `nics[].id`，必须等步骤 1 的 `az vm show` 返回后再发起，不可与步骤 1 并行。** `<primaryNicId>` = 步骤 1 `nics[]` 中 `primary==true`（只有一张时即该张）的 `id` **原值照抄**（大小写敏感），**禁止用 VM 名拼接 / 小写化生造 NIC 名**。

5a. 用步骤 1 的主网卡 `id` 查加速配置：

```
az network nic show --ids <primaryNicId> --query "{accel:enableAcceleratedNetworking, auxMode:auxiliaryMode, auxSku:auxiliarySku}" -o json
```

判断是否开 **AC**：`auxMode==AcceleratedConnections` 且 `auxSku` ∈ {A1,A2,A4,A8} → 已开 AC。

5b. 带宽峰值（始终从 VM 取）：

```
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Network In Total" "Network Out Total" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, peak:max(timeseries[0].data[?maximum!=null].maximum)}" -o json
```

每个值 ÷ 1048576 → MB/s。

5c. 连接数峰值：
- **未开 AC** → 从 VM 取，数据来源标 **VM**：
  ```
  az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Inbound Flows" "Outbound Flows" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, peak:max(timeseries[0].data[?maximum!=null].maximum)}" -o json
  ```
- **已开 AC** → 从主 NIC 取（指标名不同），数据来源标 **NIC**：
  ```
  az monitor metrics list --resource <primaryNicId> --metric "CurrentTotalFlowsIn" "CurrentTotalFlowsOut" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, peak:max(timeseries[0].data[?maximum!=null].maximum)}" -o json
  ```
  ⚠️ 用完整 NIC `id` 作 `--resource` 时**不要带 `--resource-type`**（两者同给会 usage error）。`CurrentTotalFlowsIn`→入站、`CurrentTotalFlowsOut`→出站。

**连接数有效上限**（与 vm-network-check 一致）：
- 未开 AC，按步骤 2 的 `vCPUs` 落档（非 MANA）：2–7→100,000；8–15→500,000；16–31→700,000；32–63→800,000；64+→1,000,000。所有 SKU 保底 500K；**仅 64+ vCPU 档**有 MANA 差异（开 MANA = 2,000,000），其余档位两者相同。
- 已开 AC，按 `auxSku`（覆盖 vCPU 默认档）：A1→1,000,000；A2→2,000,000；A4→4,000,000；A8→8,000,000。

### 步骤 6：磁盘（IOPS / 吞吐消耗百分比 + 延迟峰值）

> 本维度与单项技能 **vm-disk-check 采用完全相同的指标与口径**：直接读 Azure 平台「消耗百分比」指标判触顶，同时根据 SKU 查出每块盘 IOPS / 吞吐**上限**一并展示（上限仅供对照，不参与判定，无需逐分钟读写累加）。

6a. 读取每块盘的档位、容量与上限——对步骤 1 列出的**每块盘**（OS 盘 + 各数据盘）各执行一次（`<diskName>` 换成盘名）：

```
az disk show -g <rg> -n <diskName> --query "{sku:sku.name, sizeGB:diskSizeGB, tier:tier, iops:diskIOPSReadWrite, mbps:diskMBpsReadWrite}" -o json
```

得该盘 `sku`、容量 `sizeGB`、档位 `tier`（可能为空），以及 `iops`/`mbps`（仅 Premium SSD v2 / Ultra 返回自定义上限，其他类型为 null）。**确定每块盘 IOPS / 吞吐上限（仅展示，不参与判定）**：`PremiumV2_LRS` / `UltraSSD_LRS` → 上限即 `iops`/`mbps`；其他类型 → 用下方「磁盘 SKU 上限对照表」查（`tier` 有值按 tier 查；为空时按 `sku` 判系列 Premium→P/StandardSSD→E/Standard_LRS→S，用 `sizeGB` 向上取整到最近档位，该行 IOPS/MB/s 即上限、档位名即「等级」）。

> **采样粒度 `<interval>` 按时间窗长短选**：窗口 ≤ 2h → `PT1M`；2–12h → `PT5M`；12–48h → `PT15M`；> 48h → `PT1H`。`Maximum` 聚合 + `--query` 里 `max(...)` 只回峰值点，返回体很小。OS / 数据盘 / VM 三条磁盘查询用同一个 `<interval>`。（CPU/内存/网络已在服务端聚合为标量，继续用 `PT1M`。）

6b. OS 盘 IOPS / 吞吐消耗百分比 + 延迟峰值：

```
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "OS Disk IOPS Consumed Percentage" "OS Disk Bandwidth Consumed Percentage" "OS Disk Latency" --start-time <start-utc> --end-time <end-utc> --interval <interval> --aggregation Maximum --query "value[].{m:name.value, peak:max(timeseries[0].data[?maximum!=null].maximum), t:max_by(timeseries[0].data[?maximum!=null], &maximum).timeStamp}" -o json
```

前两个是 IOPS / 吞吐消耗百分比峰值（%），`OS Disk Latency` 是延迟峰值（毫秒）。

6c. 全部数据盘 IOPS / 吞吐消耗百分比 + 延迟峰值（`LUN eq '*'` 一次拿全部 LUN，`<interval>` 同 6b）：

```
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Data Disk IOPS Consumed Percentage" "Data Disk Bandwidth Consumed Percentage" "Data Disk Latency" --start-time <start-utc> --end-time <end-utc> --interval <interval> --aggregation Maximum --filter "LUN eq '*'" --query "value[].{m:name.value, series:timeseries[].{lun:metadatavalues[0].value, peak:max(data[?maximum!=null].maximum), t:max_by(data[?maximum!=null], &maximum).timeStamp}}" -o json
```

每个指标下按 LUN 给出该盘峰值与峰值时间 `t`：前两个是 IOPS / 吞吐消耗百分比峰值（%），`Data Disk Latency` 是该盘延迟峰值（毫秒）。用 `lun` 把同一块盘的三个值与时间对应起来。

6d. VM 未缓存 IOPS / 吞吐消耗百分比峰值（`<interval>` 同 6b）：

```
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "VM Uncached IOPS Consumed Percentage" "VM Uncached Bandwidth Consumed Percentage" --start-time <start-utc> --end-time <end-utc> --interval <interval> --aggregation Maximum --query "value[].{m:name.value, peak:max(timeseries[0].data[?maximum!=null].maximum), t:max_by(timeseries[0].data[?maximum!=null], &maximum).timeStamp}" -o json
```

**计算口径（消耗百分比，直读峰值；与 vm-disk-check 一致）**：
- **每块盘**：取该盘 IOPS 消耗百分比峰值、吞吐消耗百分比峰值（均 %）、`Latency` 延迟峰值（毫秒）。
- **盘级是否触顶**：该盘任一消耗百分比峰值 **≥ 95%** 即触顶。
- **VM 级是否触顶**：`VM Uncached IOPS / Bandwidth Consumed Percentage` 峰值 **≥ 95%** 即 VM 规格层面触顶。
- **延迟**：任一盘 `Latency` 峰值 > 200ms 即延迟异常。

> 消耗百分比已是「实测 ÷ 上限 ×100%」，触顶判定直取百分比、无需「同分钟读+写合计」、无需 VM 合计累加；VM 维度由 `VM Uncached *Consumed Percentage` 直接给出。步骤 6a 查到的 IOPS / 吞吐上限仅用于报告展示对照，不参与触顶判定。

### 步骤 7：运行状况（Resource Health）

复用步骤 2 已从步骤 1 的 `id` 取出的 `<subId>`，调 ARM REST（整条 URL 是 `--url` 的单个元素，`?`/`&`/`$` 原样写）：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2024-02-01&$expand=recommendedactions" --query "value[].{time:properties.occuredTime, state:properties.availabilityState, title:properties.title, cause:properties.healthEventCause, summary:properties.summary}" -o json
```

数组按时间倒序，每条是一次**健康状态变化**（Resource Health 只在状态变化时记一条，不是逐分钟连续数据；状态延续到下次变化）。`time` 是 UTC，+8 小时换算北京时间。**注意**：少数记录是计划维护描述（如 `Freeze Update Succeeded`），其 `state`(availabilityState) 为 **null**，不是“状态未知”，不可当可用性状态用。

判断一律以**诊断时间窗 `[start, end]`** 为准（见「输出格式·七」）：
- **诊断窗内事件** = `time` 落在 `[start, end]` 内的记录（含计划维护与可用性变化）；窗口外的历史记录**一律不展示、不参与判断**。
- **窗口内是否正常**：窗内无任何记录 → 平台侧无事件，判正常（不要回看窗外找“当前状态”，会把几周前旧状态 / `Unknown` 误当本次结果）；窗内有记录 → 看窗内**最新一条 `state` 非 null** 的记录，`Available` = 正常，`Unavailable` / `Degraded` = 异常。
- 窗内无事件或最新一条 `Available` → 运行状况判正常，详细数据区只输出一行「诊断时间窗内无平台健康事件」。
- 窗内最新一条为 `Unavailable` / `Degraded`，或窗内有事件 → 展开「窗内事件 / 说明」，只呈现窗内最相关的那条。

### 步骤 8：回填校验（组装报告前必做，防止误填 N/A）

前面每条 `run_az` 都已成功返回 JSON。组装报告前，**逐维度核对你是否已从对应返回值读出真实数字**：
- CPU：步骤 3 的 `avg` / `peak` / `high_minutes` / `total_minutes`。
- 内存：步骤 4 的 `avg_avail` / `min_avail`（再换算成使用率）。
- 磁盘：步骤 6b/6c 各盘的消耗百分比 `peak`（IOPS% / 吞吐%）与延迟 `peak`，以及 6d 的 VM 未缓存 `peak`。
- 网络：步骤 5 的带宽与连接数 `peak`。

**只有当某条命令的返回是 `null` / 空数组 / `{"error":...}` 时，该维度才写 N/A。**
正常运行的 VM，CPU 与磁盘是平台 host 指标，必然返回有效数字——若你手里已有数字却在报告里写了 N/A，那是回填遗漏，必须改回真实值。不要因为并行调用多、结果多而丢掉任何一维的数据。

## 磁盘 SKU 上限对照表（步骤 6a 查表用，数据同 vm-disk-check）

单盘 IOPS / 吞吐(MB/s) 上限（来源：https://learn.microsoft.com/azure/virtual-machines/disks-types）。
**「等级」反推**：Standard / Premium SSD v1 / HDD 盘 `tier` 常为空，用 `sizeGB` 向上取整到第一个 ≥ 容量的档位，该行「档位」即等级名，取该行 IOPS/MB/s 为上限。Premium SSD v2 / Ultra 不查表，直接用盘自定义 `diskIOPSReadWrite`/`diskMBpsReadWrite`。

**Premium SSD（P 系）**

| 档位 | 容量(GB) | IOPS | MB/s |
| --- | --- | --- | --- |
| P1 | 4 | 120 | 25 |
| P2 | 8 | 120 | 25 |
| P3 | 16 | 120 | 25 |
| P4 | 32 | 120 | 25 |
| P6 | 64 | 240 | 50 |
| P10 | 128 | 500 | 100 |
| P15 | 256 | 1100 | 125 |
| P20 | 512 | 2300 | 150 |
| P30 | 1024 | 5000 | 200 |
| P40 | 2048 | 7500 | 250 |
| P50 | 4096 | 7500 | 250 |
| P60 | 8192 | 16000 | 500 |
| P70 | 16384 | 18000 | 750 |
| P80 | 32767 | 20000 | 900 |

**Standard SSD（E 系，基线上限）**

| 档位 | 容量(GB) | IOPS | MB/s |
| --- | --- | --- | --- |
| E1 | 4 | 500 | 60 |
| E2 | 8 | 500 | 60 |
| E3 | 16 | 500 | 60 |
| E4 | 32 | 500 | 60 |
| E6 | 64 | 500 | 60 |
| E10 | 128 | 500 | 60 |
| E15 | 256 | 500 | 60 |
| E20 | 512 | 500 | 60 |
| E30 | 1024 | 500 | 60 |
| E40 | 2048 | 500 | 60 |
| E50 | 4096 | 500 | 60 |
| E60 | 8192 | 2000 | 400 |
| E70 | 16384 | 4000 | 600 |
| E80 | 32767 | 6000 | 750 |

**Standard HDD（S 系）**

| 档位 | 容量(GB) | IOPS | MB/s |
| --- | --- | --- | --- |
| S4 | 32 | 500 | 60 |
| S6 | 64 | 500 | 60 |
| S10 | 128 | 500 | 60 |
| S15 | 256 | 500 | 60 |
| S20 | 512 | 500 | 60 |
| S30 | 1024 | 500 | 60 |
| S40 | 2048 | 500 | 60 |
| S50 | 4096 | 500 | 60 |
| S60 | 8192 | 1300 | 300 |
| S70 | 16384 | 2000 | 500 |
| S80 | 32767 | 2000 | 500 |

## 判断标准（每个维度独立判定，汇总到结论第一行）

**CPU（两档）**：✅ 正常=高位（≥90%）占比<20%；❌ 异常=高位（≥90%）占比≥20%（均值/峰值仅参考展示）。
**内存（两档）**：✅ 正常=高位（≥90%）占比<20%；❌ 异常=高位（≥90%）占比≥20%（均值/峰值仅参考展示）。
**磁盘（两档，口径同 vm-disk-check）**：✅ 正常=所有盘及 VM 未缓存的 IOPS / 吞吐消耗百分比峰值均 <95%，且所有盘延迟峰值 ≤200ms；❌ 异常=任一盘或 VM 未缓存 IOPS / 吞吐消耗百分比峰值 ≥95%，或任一盘延迟峰值 >200ms。
**网络（两档）**：✅ 正常=入/出连接数峰值中较大者未达有效上限；❌ 异常=达到或超过有效上限（带宽仅参考展示，不单独触发异常）。
**运行状况（两档）**：✅ 正常=诊断窗内无事件，或窗内最新一条 `state` 为 `Available`（含已恢复的计划维护）；❌ 异常=窗内最新一条 `state` 为 `Unavailable` / `Degraded`。只看窗内记录、不取窗外历史；用户对历史事件有疑问则建议提单 Azure Support。

## 输出格式（严格照此组装，逐项填值，不要加寒暄/表情/多余前后缀）

```
🔧 诊断模式（数据来源：Azure Monitor / Resource Health 实时查询）
诊断时间范围：<起始北京时间> ~ <结束北京时间>（北京时间）

一、主机信息
  实例 ID：<name>
  主机名：<computerName 或 N/A>
  资源组：<resource_group>
  区域：<location>
  机型 SKU：<vmSize>（vCPU=<n>，内存=<x.x> GB）
  操作系统类型：<osType 或 N/A>
  当前状态：<powerState，如 running>

二、结论
- 是否异常：CPU <✅ 正常/❌ 异常> / 内存 <✅ 正常/❌ 异常> / 磁盘 <✅ 正常/❌ 异常> / 运行状况 <✅ 正常/❌ 异常> / 网络 <✅ 正常/❌ 异常>。<一句话整体判断，点名异常维度；全部正常则说整体健康>
- 风险判断：<低 / 中 / 高>，一句话说明（如各维度均在正常范围，运行稳定）。
- 建议动作：1) <可执行建议> 2) <可执行建议>（全部正常则写「无需处理，继续观察」）。
- 参考文档：<由你根据最突出的异常维度自行推荐 1 条最相关的 Microsoft Learn 官方文档链接，
  不要写死；全部正常可给 VM 监控总览文档>

———— 详细数据 ————
三、CPU 指标
  CPU 利用率峰值：<峰值>%
  CPU 利用率均值：<均值>%
  高位占比（≥90%）：<高位占比>%（<high_minutes>/<total_minutes> 分钟）

四、内存指标
  内存使用率峰值：<峰值>%
  内存使用率均值：<均值>%
  高位占比（≥90%）：<高位占比>%（<high_used_minutes>/<total_minutes> 分钟）

五、磁盘指标
  [OS盘] 名称=<盘名>  SKU=<sku>  容量=<sizeGB> GB  等级=<tier 或反推档位>
    SKU 上限：IOPS=<上限>，吞吐=<上限> MB/s
    IOPS 消耗峰值：<p>%（时间=<峰值时间>）
    吞吐消耗峰值：<p>%（时间=<峰值时间>）
    延迟峰值：<lat> ms（时间=<峰值时间>）

  [数据盘·<tier> / <family> ×<块数>]
    SKU 上限：IOPS=<上限>，吞吐=<上限> MB/s
    · ✅ 全部 <n> 块正常 LUN=<逐块列出 LUN 及容量，如 0(512GB), 1(512GB)>（<单块直接写 IOPS 消耗峰值=…%，吞吐消耗峰值=…%，延迟峰值=…ms；多块写 组内最高 IOPS 消耗=…%，最高吞吐消耗=…%，最高延迟=…ms>）

  [数据盘·<tier> / <family> ×<块数>] 共 <n> 块：⚠️ 异常 <异常数> / ✅ 正常 <正常数>
    SKU 上限：IOPS=<上限>，吞吐=<上限> MB/s
    · ⚠️ LUN <lun> 名称=<盘名> 容量=<sizeGB> GB [异常：<只列超标项，如 IOPS消耗 98%≥95%、延迟 320ms>200ms>]
       IOPS 消耗峰值：<p>%（时间=<峰值时间>）  吞吐消耗峰值：<p>%（时间=<峰值时间>）  延迟峰值：<lat> ms（时间=<峰值时间>）（上限 <iops> IOPS / <mbps> MB/s）

  [VM 级未缓存消耗] IOPS 消耗峰值=<p>%，吞吐消耗峰值=<p>%

六、网络指标
  入站带宽峰值：<x.x> MB/s
  出站带宽峰值：<x.x> MB/s
  入站连接峰值：<n> flows  数据来源=<VM/NIC>
  出站连接峰值：<n> flows  数据来源=<VM/NIC>
  加速网卡：<已启用 / 未启用 / 未知>  AC：<见下方说明>
  连接数有效上限：<有效上限> flows（<档位说明，如 “16–31 vCPU 档” 或 “AC A2”>）

七、运行状况（Resource Health）
  <二选一：>
  窗口内平台状态：<可用（窗内无记录或最新为 Available）>  诊断时间窗内无平台健康事件
  <或，仅当窗口期异常或窗内确有事件时：>
  窗内事件：<事件北京时间>  状态=<state 或 title>  原因=<平台发起 / 用户发起 / N/A>
  说明：<summary>
```

> 「七、运行状况」二选一：窗内无事件或窗内最新为 `Available` → 只输出「窗口内平台状态 + 诊断时间窗内无平台健康事件」一行，**不要**贴窗外的历史维护事件；窗内最新为异常或窗内有事件 → 展开「窗内事件 / 说明」。

- **网络 `AC：` 行**（与 vm-network-check 一致）按实际情况写：
  - 已开 AC（`auxMode==AcceleratedConnections` 且 `auxSku` 有效）→ `已启用（<auxSku>：连接数上限 <上限/10000> 万）`，例如 `已启用（A2：连接数上限 200 万）`。
  - 未开 AC → `未启用（按 <vCPU 档位> 档位，连接数上限 <非 MANA 上限>`，64+ vCPU 再补 `，开 MANA 可到 2,000,000`，最后加 `）`。
- **磁盘按「档位 + 磁盘类型」分组**：每组标题下一行统一写 `SKU 上限：IOPS=<上限>，吞吐=<上限> MB/s`（由 6a 查 SKU 得出，同档位同类型上限相同）。组内全部正常 → 标题不加「共 N 块」，聚合成一行 `· ✅ 全部 N 块正常 LUN=0(512GB), 1(512GB)…`，逐块列 LUN 与容量；括号峰值：组内仅 1 块直接写该盘「IOPS 消耗峰值=…%，吞吐消耗峰值=…%，延迟峰值=…ms」，多于 1 块才写「组内最高 IOPS 消耗=…%，最高吞吐消耗=…%，最高延迟=…ms」。组内有异常 → 标题加 `共 N 块：⚠️ 异常 X / ✅ 正常 Y`，异常盘逐块展开，正常盘可省略。Premium SSD v2 / Ultra 每块上限不同，组头不写统一 `SKU 上限` 行，改在每块行末尾标 `（上限 <iops> IOPS / <mbps> MB/s）`。异常盘明细行末尾的 `（上限 …）` 要填该盘实际上限：v2/Ultra 取 6a 的 `iops`/`mbps`；**其他类型（Premium v1 / Standard SSD / HDD）这些字段为 null，要取 SKU 对照表查出的上限（与组标题 `SKU 上限` 同一个数），不要写 N/A**。
- 异常盘 `[异常：…]` 只列真正超标的维度（IOPS 消耗≥95%、吞吐消耗≥95%、延迟>200ms），写成「消耗 98%≥95%」「延迟 320ms>200ms」直观对比。
- 磁盘 IOPS/吞吐触顶判定一律直读「消耗百分比峰值」（无需同分钟读+写累加、无需 VM 合计）；绝对上限由 6a 查 SKU 得出并展示（仅作对照）；VM 维度直接读 `VM Uncached *Consumed Percentage` 峰值；延迟为单序列峰值；逐块盘明细一律用「=」写确定数值，禁用「≤」。

- 数值：百分比、带宽（MB/s）保留 1 位小数；IOPS / flows 取整，可用千分位。
- 时间格式 `YYYY-MM-DD HH:MM:SS`；取不到的值写 N/A，不臆造。
- **「一、主机信息」的机型 SKU 行必须完整列出括号内的 vCPU / 内存**（来自步骤 2），只有查不到才写 N/A。
- 结论第一行**必须按 CPU / 内存 / 磁盘 / 运行状况 / 网络 的顺序**用单行列出五个维度的是否异常标记。
- 直接把组装好的中文报告输出给用户，**不要展示命令或 JSON**。

## 注意事项

- 本技能为**只读诊断**，不修改任何资源、不进入 VM 内部；`run_az` 只允许只读命令。
- 共享步骤（vm show / SKU 查询）只跑一次，五个维度复用；**磁盘口径与 vm-disk-check 完全一致**（直读 IOPS / 吞吐消耗百分比判触顶 + 逐盘 SKU 上限展示 + 延迟峰值），CPU / 内存 / 网络 的聚合算法亦与对应单项技能产出相同数值。
- 连接数：未开 AC 取 VM 的 `Inbound/Outbound Flows`；已开 AC 取主 NIC 的 `CurrentTotalFlowsIn/Out`（用完整 NIC `id`，不带 `--resource-type`）。
- 时间一律按北京时间向用户呈现；调用 az 时换算为 UTC。
- N/A 只能在对应 `run_az` 返回 `null`/空/`error` 时写（多为 VM 已释放或未装 Agent）；**凡返回带数字的维度一律填真实值，禁止有数据却写 N/A**（参见步骤 8 回填校验）。CPU、磁盘为平台 host 指标，正常 VM 必有数据。
- 运行状况采条件展示：诊断时间窗内无事件时，只提示「诊断时间窗内无平台健康事件」，不展示窗外的历史事件。
- 主机名必填；资源组缺省时使用默认资源组 `xiaomi-azure`，不要追问。
