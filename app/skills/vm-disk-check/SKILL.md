---
name: vm-disk-check
description: >-
  诊断 Azure 虚拟机所有磁盘的 SKU / 上限 / 实测 IOPS / 吞吐 / 延迟（基于 Azure Monitor 平台指标，只读）。
  当用户询问某台 VM 的「磁盘 / IO / IOPS / 吞吐 / 延迟 / 慢盘」、磁盘性能告警、或反馈读写慢时使用。
  典型问法：「主机 xxx 磁盘 IO 高不高」「这台机器有没有慢盘」「近 30 分钟有 IO 突增」。
---

# VM 磁盘诊断技能

本技能对一台 Azure 虚拟机的全部磁盘（OS 盘 + 各数据盘）做**只读**诊断：从 Azure Monitor
直接读取每块盘及 VM 级的 IOPS / 吞吐**消耗百分比峰值**与延迟峰值，与阈值对比给出判断与建议；
同时根据磁盘 SKU 查出每块盘的 IOPS / 吞吐**上限**一并展示（触顶判定以消耗百分比为准，上限仅供直观对照，无需逐分钟读写累加）。

> 本技能**不进入 VM 操作系统内部**，只读取 Azure 控制面的磁盘配置与平台指标。
> 文件系统级别（哪个目录/文件 IO 高）属于 OS 级深诊断，不在本技能范围内。

## 何时使用

满足任一条件即可使用本技能：

- VM 触发了磁盘性能 / 延迟告警
- 用户反馈读写慢、IO 卡顿，怀疑磁盘瓶颈
- 需要确认某块盘或 VM 整体是否触达 IOPS / 吞吐上限
- 用户直接询问「磁盘 / IO / IOPS / 吞吐 / 延迟 / 慢盘」相关情况

## 执行步骤

本技能为 **SKILL.md 驱动型**（无脚本）。你将通过全局工具 `run_az` 逐条执行下面的 az CLI 命令，
然后按「计算口径」「判断标准」「输出格式」自行组装中文报告。

> 默认资源组：`xiaomi-azure`（用户未显式给出资源组时直接采用，不要追问）。
> 所有命令都不要带 `--subscription`（后端已注入订阅）。
> 时间为 UTC（北京时间 − 8h）。例如北京 14:00 → UTC 06:00，格式 `2025-01-15T06:00:00Z`；未指定时间默认回看 30 分钟。

### 步骤 1：读取 VM 基本信息与盘列表

用 `run_az` 执行（把 `<rg>`、`<vm>` 换成实际值）：

```bash
az vm show -d -g <rg> -n <vm> --query "{id:id, name:name, location:location, vmSize:hardwareProfile.vmSize, osType:storageProfile.osDisk.osType, powerState:powerState, osDisk:storageProfile.osDisk.{name:name, sku:managedDisk.storageAccountType}, dataDisks:storageProfile.dataDisks[].{lun:lun, name:name, sku:managedDisk.storageAccountType}}" -o json
```

提取：`name`、`location`、`vmSize`、`powerState`、OS 盘名、各数据盘的 `lun` 与盘名、`id`（资源 ID，形如 `/subscriptions/<subId>/resourceGroups/...`，下一步取 `<subId>`）。

确认 VM 存在后，再执行一条取**当前主机名**（Guest Agent 上报的 OS 主机名，对应门户「计算机名称」，与实例 ID / 资源名不同）：

```
az vm get-instance-view -g <rg> -n <vm> --query "instanceView.computerName" -o json
```

返回形如 `"ams3-mife-fe47.aznl.idc.xiaomi.com"`，记为 `<computerName>`，填入「一、主机信息」的主机名行；取不到写 N/A。

> ⚠️ 若 `powerState` 不是 `VM running`（如 `VM deallocated`），磁盘指标可能缺失，应如实告知用户。

### 步骤 2：SKU 规格（vCPU / 内存，必查）

「一、主机信息」机型 SKU 行的 vCPU / 内存来自这里，不得省略或臆造。先从步骤 1 的 `id` 里取 `<subId>`（`/subscriptions/` 之后、下一个 `/` 之前那段），再用 `<loc>`、`<vmSize>` 调 Compute SKUs API（服务端按 location 过滤，比 `az vm list-skus` 快几十倍）：

```bash
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/providers/Microsoft.Compute/skus?api-version=2021-07-01&$filter=location eq '<loc>'" --query "value[?name=='<vmSize>'] | [0].capabilities[?name=='vCPUs' || name=='MemoryGB'].{name:name,value:value}" -o json
```

返回 `vCPUs`、`MemoryGB`，填入「一、主机信息」的机型 SKU 行（**必须填，不得省略**；真返回空才重试一次）。

> 本技能用 Azure 平台「消耗百分比」指标判断是否触顶（步骤 4–6）；每块盘的绝对上限（IOPS / 吞吐）由步骤 3 根据 SKU 查出后一并展示，仅用于直观对照，不参与触顶判定。VM 未缓存维度直接看 `VM Uncached *Consumed Percentage`，无需另算 VM 上限数值。

### 步骤 3：读取每块盘的档位、容量与上限

对步骤 1 列出的**每块盘**（OS 盘 + 各数据盘）各执行一次（`<diskName>` 换成盘名）：

```bash
az disk show -g <rg> -n <diskName> --query "{sku:sku.name, sizeGB:diskSizeGB, tier:tier, iops:diskIOPSReadWrite, mbps:diskMBpsReadWrite}" -o json
```

得该盘 `sku`（如 `Premium_LRS` / `StandardSSD_LRS` / `Standard_LRS` / `PremiumV2_LRS` / `UltraSSD_LRS`）、容量 `sizeGB`、档位 `tier`（如 `P4`，可能为空），以及 `iops`/`mbps`（字段 `diskIOPSReadWrite`/`diskMBpsReadWrite`，IOPS 全大写；**仅 Premium SSD v2 / Ultra 盘返回该盘自定义上限**，其他类型为 null）。

**确定每块盘的 IOPS / 吞吐上限（仅用于展示，触顶判定仍以消耗百分比为准）**：
- `sku` 为 `PremiumV2_LRS` / `UltraSSD_LRS` → 上限即返回的 `iops` / `mbps`（盘自定义值），不查表。
- 其他类型 → 用下方「磁盘 SKU 上限对照表」查：`tier` 有值（如 P30/E20/S30）直接按 tier 查；`tier` 为空时按 `sku` 判系列（Premium→P 系、StandardSSD→E 系、Standard_LRS→S 系），用 `sizeGB` 向上取整到第一个 ≥ 容量的档位查表，该行 IOPS / MB/s 即上限，档位名即「等级」。

### 步骤 4：查询 OS 盘 IOPS / 吞吐消耗百分比 + 延迟峰值

> **采样粒度 `<interval>` 按时间窗长短选**：窗口 ≤ 2 小时 → `PT1M`；2–12 小时 → `PT5M`；12–48 小时 → `PT15M`；> 48 小时 → `PT1H`。聚合用 `Maximum`，`--query` 里用 `max(...)` 让结果只回峰值点，返回体很小（不再因点多被截断）。OS / 数据盘 / VM 三条查询用同一个 `<interval>`。

```bash
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "OS Disk IOPS Consumed Percentage" "OS Disk Bandwidth Consumed Percentage" "OS Disk Latency" --start-time <start-utc> --end-time <end-utc> --interval <interval> --aggregation Maximum --query "value[].{m:name.value, peak:max(timeseries[0].data[?maximum!=null].maximum), t:max_by(timeseries[0].data[?maximum!=null], &maximum).timeStamp}" -o json
```

返回 OS 盘三个指标的峰值 `peak` 与峰值时间 `t`：`OS Disk IOPS Consumed Percentage`、`OS Disk Bandwidth Consumed Percentage` 是 IOPS / 吞吐**消耗百分比**（%），`OS Disk Latency` 是**延迟**（毫秒）。`peak` 为 null/空 → 该窗无数据，写 N/A。

### 步骤 5：查询全部数据盘 IOPS / 吞吐消耗百分比 + 延迟峰值（一次拿全部 LUN）

用 `run_az` 执行**一次**（`LUN eq '*'` 一次返回每个 LUN 一条序列；`<interval>` 同步骤 4）：

```bash
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Data Disk IOPS Consumed Percentage" "Data Disk Bandwidth Consumed Percentage" "Data Disk Latency" --start-time <start-utc> --end-time <end-utc> --interval <interval> --aggregation Maximum --filter "LUN eq '*'" --query "value[].{m:name.value, series:timeseries[].{lun:metadatavalues[0].value, peak:max(data[?maximum!=null].maximum), t:max_by(data[?maximum!=null], &maximum).timeStamp}}" -o json
```

每个指标下按 LUN 给出该盘峰值 `peak` 与峰值时间 `t`：前两个是 IOPS / 吞吐**消耗百分比峰值**（%），`Data Disk Latency` 是该盘**延迟峰值**（毫秒）。用 `lun` 把同一块盘的三个值与时间对应起来。

### 步骤 6：查询 VM 未缓存 IOPS / 吞吐消耗百分比峰值

用 `run_az` 执行**一次**（`<interval>` 同步骤 4）：

```bash
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "VM Uncached IOPS Consumed Percentage" "VM Uncached Bandwidth Consumed Percentage" --start-time <start-utc> --end-time <end-utc> --interval <interval> --aggregation Maximum --query "value[].{m:name.value, peak:max(timeseries[0].data[?maximum!=null].maximum), t:max_by(timeseries[0].data[?maximum!=null], &maximum).timeStamp}" -o json
```

得 VM 级未缓存 IOPS / 吞吐**消耗百分比峰值**（%）。这是 VM 规格对全部磁盘 IO 的总瓶颈：单盘没满但此处接近 100%，说明受 VM 规格限制。

### 步骤 7：运行状况（Resource Health，判断异常是否与底层平台有关）

时间窗与上面磁盘查询**保持一致**。先从步骤 1 的 `id` 里取 `<subId>`（`/subscriptions/` 之后、下一个 `/` 之前那段），调 ARM REST（整条 URL 是 `--url` 的单个元素，`?`/`&`/`$` 原样写）：

```bash
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2024-02-01&$expand=recommendedactions" --query "value[].{time:properties.occuredTime, state:properties.availabilityState, title:properties.title, cause:properties.healthEventCause, summary:properties.summary}" -o json
```

数组按时间倒序，每条是一次**健康状态变化**（Resource Health 只在状态变化时记一条，状态延续到下次变化，不是逐分钟数据）。`time` 是 UTC，+8 小时换算北京时间。少数记录是计划维护描述（如 `Freeze Update Succeeded`），其 `state`(availabilityState) 为 **null**，不是“状态未知”。

**判定只看诊断时间窗 `[start, end]` 内的记录，窗口外的历史一律不展示、不参与判断**：
- **诊断窗内平台事件** = `time` 落在 `[start, end]` 内的记录（含计划维护与可用性变化）；窗内无任何记录 → 写「无」。
- **窗口内是否正常**：窗内无记录 → 平台侧无事件，判正常（不要回看窗外去找“当前状态”，那会把几周前的旧状态 / `Unknown` 误当成本次结果）；窗内有记录 → 看窗内**最新一条 `state` 非 null** 的记录，`Available` = 正常，`Unavailable` / `Degraded` = 异常。
- **用途**：磁盘指标若判为异常，且窗内有非 `Available` 事件 → 在结论里点明异常**可能与底层平台有关**；窗内无事件或均 `Available` → 可排除平台因素，问题更可能在业务/系统侧。

## 计算口径（消耗百分比，直读峰值）

Azure「消耗百分比」指标本身就是「实测 IO ÷ 对应上限 ×100%」，**直接取峰值与阈值比较即可，无需再查上限、无需逐分钟读写累加、也无需汇总 VM 合计**：
- **每块盘**：取该盘 IOPS 消耗百分比峰值、吞吐消耗百分比峰值（均 %），以及 `Latency` 延迟峰值（毫秒）。
- **盘级是否触顶**：该盘任一消耗百分比峰值 **≥ 95%** 即视为已达到 / 接近 IOPS 或吞吐上限（触顶）。
- **VM 级是否触顶**：`VM Uncached IOPS Consumed Percentage` 或 `VM Uncached Bandwidth Consumed Percentage` 峰值 **≥ 95%** 即 VM 规格层面触顶。
- **延迟**：任一盘 `Latency` 峰值 > 200ms 视为延迟异常。

> 消耗百分比是 Azure host 侧实测占用率，封顶 100%（持续 100% 即处于限速）；阈值 95% 表示「已达到或非常接近上限」。VM 维度由 `VM Uncached *Consumed Percentage` 直接给出，不再做「同分钟读+写合计」或「各盘累加」。
> 步骤 3 查到的 IOPS / 吞吐上限仅用于报告展示对照（让用户看到「消耗 X% 对应绝对 ≤ N IOPS」），不参与触顶判定。

## 磁盘 SKU 上限对照表（步骤 3 查表用）

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

## 判断标准

磁盘只分**正常 / 异常**两档（不设中间态），以下任一命中即判异常：

| 结论 | 判定条件 |
| --- | --- |
| ✅ 正常 | 所有盘的 IOPS / 吞吐消耗百分比峰值、以及 VM 未缓存 IOPS / 吞吐消耗百分比峰值均 < 95%，且所有盘延迟峰值 ≤ 200ms |
| ❌ 异常 | 任一盘或 VM 未缓存的 IOPS / 吞吐消耗百分比峰值 ≥ 95%（触顶），**或** 任一盘延迟峰值 > 200ms |

- **盘级瓶颈**：某块盘 IOPS 或吞吐消耗百分比峰值 ≥ 95% → 该盘已触顶，需升 SKU / 升容量档位。
- **VM 级瓶颈**：单盘没满但 VM 未缓存 IOPS 或吞吐消耗百分比峰值 ≥ 95% → VM 规格限制了总 IO，需升 VM SKU。
- **延迟风险**：任一盘延迟峰值 > 200ms 即判为异常（即使消耗百分比未满）→ 可能是 HDD / 低档 SSD 或后端拥塞，建议升 Premium SSD 并排查。

## 输出格式

严格照此组装**中文**报告（逐项填值，**直接输出给用户**，不要展示命令或 JSON，不要加寒暄/多余前后缀）：

```
🔧 诊断模式（数据来源：Azure Monitor + Resource Health 实时查询）
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
- 是否异常：<✅ 正常 / ❌ 异常>，并简述依据（哪块盘/哪个维度触顶、消耗百分比峰值多少、最大延迟多少）。
- 风险判断：<低 / 中 / 高>，一句话说明（如各盘均远低于上限，运行稳定）。
- 建议动作：1) <可执行建议> 2) <可执行建议>（正常则写「无需处理，继续观察」）。
- 参考文档：<由你根据结论自行推荐 1 条最相关的 Microsoft Learn 官方文档链接，
  不要写死；触顶可给磁盘/VM 升配文档，延迟高可给磁盘性能排查文档，正常可给磁盘指标监控文档>

———— 详细数据 ————
三、磁盘指标
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

四、运行状况（Resource Health）
  诊断窗内平台事件：<无 / 有：简述 time∈[start,end] 的最相关一条（北京时间 + 计划内/计划外 + 简要说明）>
  窗口内平台状态：<窗内有记录→取窗内最新一条 state（Available=正常 / Unavailable / Degraded）；窗内无记录→无平台事件（视为正常）>
```

- 数据盘**按「档位 + 磁盘类型」分组**：每组标题行 `[数据盘·<等级> / <类型> ×<块数>]`（Premium SSD v2 / Ultra 等无 tier 的类型，标题写为 `[数据盘·<类型> ×<块数>]`）。标题下一行统一写该组 `SKU 上限：IOPS=<上限>，吞吐=<上限> MB/s`（同档位同类型上限相同）。
  - 组内全部正常 → 标题行不加「共 N 块」后缀，正文聚合成一行 `· ✅ 全部 N 块正常 LUN=0(512GB), 1(512GB)…`，**逐块列出 LUN 号与容量**。括号里峰值：**组内只有 1 块**直接写该盘「IOPS 消耗峰值=…%，吞吐消耗峰值=…%，延迟峰值=…ms」；**组内多于 1 块**写「组内最高 IOPS 消耗=…%，最高吞吐消耗=…%，最高延迟=…ms」。
  - 组内有异常 → 标题行加 `共 N 块：⚠️ 异常 X / ✅ 正常 Y`，把**异常盘逐块展开**（每块以 `· ⚠️ LUN …` 起头），正常盘可省略。
  - **Premium SSD v2 / Ultra**：每块盘上限为用户自定义且可能不同，组头不写统一 `SKU 上限`行，改在每块行末尾标注该盘上限（取 6a 返回的 `iops`/`mbps`）。
  - **异常盘明细行末尾的 `（上限 <iops> IOPS / <mbps> MB/s）` 要填该盘实际上限**：Premium SSD v2 / Ultra 取 6a 的 `iops`/`mbps`；**Premium SSD v1 / Standard SSD / Standard HDD 这些类型 `iops`/`mbps` 为 null，要取「SKU 上限对照表」查出的上限（即组标题 `SKU 上限` 行同一个数），不要写 N/A**。
- 异常盘的 `[异常：…]` 只列**真正超标的维度**（IOPS 消耗 ≥ 95%、吞吐消耗 ≥ 95%、延迟 > 200ms），写成「消耗 X%≥95%」「延迟 Xms>200ms」的直观对比。多项用中文逗号分隔。
- IOPS / 吞吐的**绝对上限**由步骤 3 查 SKU 得出并展示（仅作对照），**触顶判定以消耗百分比为准**；无需逐分钟读写累加、无需 VM 合计。
- 逐块盘明细里的消耗百分比 / 延迟峰值是该盘的确定实测值，一律用「=」直接写数值，禁用「≤」；仅把**多块盘**聚合成一行时才用「组内最高 …=<组内最大值>」。
- 数值保留 1 位小数；时间格式 `YYYY-MM-DD HH:MM:SS`；取不到的值写 N/A，不臆造。
- **「一、主机信息」的机型 SKU 行必须完整列出括号内的 vCPU / 内存**（来自步骤 2），不得只写 SKU 名而省略规格；取不到才写 N/A。

## 注意事项

- 本技能为**只读诊断**，不修改任何资源，不进入 VM 内部。同时覆盖 OS 盘与所有数据盘，按 LUN 区分。
- 所有命令只读（`az ... show` / `list` / `monitor metrics list`），绝不执行变更类命令。
- 时间一律按北京时间向用户呈现；调用 az 时换算为 UTC。
- 若指标缺失（VM 已释放 / 未装 Azure Monitor Agent），如实告知用户，不要编造数据。
- 主机名必填；资源组缺省时使用默认资源组 `xiaomi-azure`，不要追问。
