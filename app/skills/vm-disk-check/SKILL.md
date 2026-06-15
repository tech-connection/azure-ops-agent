---
name: vm-disk-check
description: >-
  诊断 Azure 虚拟机所有磁盘的 SKU / 上限 / 实测 IOPS / 吞吐 / 延迟（基于 Azure Monitor 平台指标，只读）。
  当用户询问某台 VM 的「磁盘 / IO / IOPS / 吞吐 / 延迟 / 慢盘」、磁盘性能告警、或反馈读写慢时使用。
  典型问法：「主机 xxx 磁盘 IO 高不高」「这台机器有没有慢盘」「近 30 分钟有 IO 突增」。
---

# VM 磁盘诊断技能

本技能对一台 Azure 虚拟机的全部磁盘（OS 盘 + 各数据盘）做**只读**诊断：读取每块盘的
SKU 上限（IOPS / 吞吐），从 Azure Monitor 拉取实测读写 IOPS、吞吐、延迟峰值，并汇总
VM 级合计与 VM 未缓存上限对比，给出判断与建议。

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
然后按「SKU 上限对照表」「判断标准」「输出格式」自行组装中文报告。

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

### 步骤 2：查询 VM 未缓存磁盘上限（VM 级瓶颈基准、**必须执行**）

用 `run_az` 执行（先从步骤 1 的 `id` 里取 `<subId>`——`/subscriptions/` 之后、下一个 `/` 之前那段；`<loc>`、`<vmSize>` 用步骤 1 的值），调 Compute SKUs API（服务端按 location 过滤，比 `az vm list-skus` 快几十倍）：

```bash
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/providers/Microsoft.Compute/skus?api-version=2021-07-01&$filter=location eq '<loc>'" --query "value[?name=='<vmSize>'] | [0].capabilities[?name=='vCPUs' || name=='MemoryGB' || name=='UncachedDiskIOPS' || name=='UncachedDiskBytesPerSecond'].{name:name,value:value}" -o json
```

返回四个值：`vCPUs`（vCPU 数）、`MemoryGB`（内存 GB）、`UncachedDiskIOPS`（VM 未缓存 IOPS 上限）、`UncachedDiskBytesPerSecond`（未缓存吞吐上限，单位字节/秒，÷1000000 得 MB/s，如 1735000000 → 1735 MB/s）。其中 `vCPUs` / `MemoryGB` 用于「一、主机信息」的机型 SKU 行（**必须填，不得省略**）。

> ⚠️ 这一步**不能跳过**。该接口一定能返回这几个值（已验证 Standard_D64s_v5 返回 80000 / 1735000000）。报告里的「[VM 级未缓存上限]」必须填真实数值，**不得写 N/A**；若某次调用真的返回空，重试一次再填。

### 步骤 3：读取每块盘的档位与容量

对步骤 1 列出的**每块盘**（OS 盘 + 各数据盘）各执行一次（`<diskName>` 换成盘名）：

```bash
az disk show -g <rg> -n <diskName> --query "{sku:sku.name, sizeGB:diskSizeGB, tier:tier, iops:diskIOPSReadWrite, mbps:diskMBpsReadWrite}" -o json
```

得到该盘的 `sku`（如 `Premium_LRS` / `StandardSSD_LRS` / `Standard_LRS` / `PremiumV2_LRS` / `UltraSSD_LRS`）、容量 `sizeGB`（单位 GB）、档位 `tier`（如 `P4`）。
其中 `iops`/`mbps`（对应字段 `diskIOPSReadWrite`/`diskMBpsReadWrite`，注意 IOPS 全大写）：**Premium SSD v2 / Ultra 盘会直接返回该盘的自定义上限**（如 iops=3000, mbps=125），其他类型返回 null。

### 步骤 4：查 SKU 上限对照表

用步骤 3 的结果，按下方「SKU 上限对照表」查出每块盘的 **IOPS 上限 / 吞吐上限**：
- 若 `tier` 有值（如 `P4`/`E20`/`S30`）→ 直接按 tier 查表。
- 若 `tier` 为空 → 按 `sku` 判断系列（Premium→P 系、StandardSSD→E 系、Standard_LRS→S 系），再用 `sizeGB` 向上取整到最近档位查表。
- 若 `sku` 为 `PremiumV2_LRS` / `UltraSSD_LRS` → 上限由用户在盘上自定义，**直接用步骤 3 返回的 `iops`/`mbps`**（即 `diskIOPSReadWrite`/`diskMBpsReadWrite`）作为上限，不查表。若这两个值为 null，说明字段名写错了（注意 IOPS 全大写）。

### 步骤 5：查询 OS 盘实测指标（逐区间明细，用于同区间合计）

> **采样粒度 `<interval>` 按时间窗长短动态选（磁盘查询返回逐区间序列，窗口越长点越多；点太多会把返回体撞爆、被截断导致误判 N/A）：**
> - 窗口 ≤ 2 小时 → `PT1M`；2–12 小时 → `PT5M`；12–48 小时 → `PT15M`；> 48 小时 → `PT1H`。
> - 聚合用 `Maximum`，**更大 interval 仍取区间内最大瞬时值，不漏峰值**，只是峰值时间定位变粗；读/写仍同时间戳对齐，可照常同区间相加。OS 盘与数据盘查询用同一个 `<interval>`。

用 `run_az` 执行（返回每个指标的逐区间序列，含时间戳，供同区间对齐）：

```bash
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "OS Disk Read Operations/Sec" "OS Disk Write Operations/Sec" "OS Disk Read Bytes/sec" "OS Disk Write Bytes/sec" "OS Disk Latency" --start-time <start-utc> --end-time <end-utc> --interval <interval> --aggregation Maximum --query "value[].{m:name.value, data:timeseries[0].data[?maximum!=null].{t:timeStamp, v:maximum}}" -o json
```

返回 OS 盘 5 个指标的逐区间 `{t, v}` 序列（读 IOPS、写 IOPS、读吞吐字节/秒、写吞吐字节/秒、延迟毫秒）。按「计算口径」做同区间对齐求峰。

### 步骤 6：查询全部数据盘实测指标（一次拿全部 LUN）

用 `run_az` 执行**一次**（`LUN eq '*'` 一次返回每个 LUN 一条序列，比逐 LUN 多次调用少很多往返）：

```bash
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Data Disk Read Operations/Sec" "Data Disk Write Operations/Sec" "Data Disk Read Bytes/sec" "Data Disk Write Bytes/sec" "Data Disk Latency" --start-time <start-utc> --end-time <end-utc> --interval <interval> --aggregation Maximum --filter "LUN eq '*'" --query "value[].{m:name.value, series:timeseries[].{lun:metadatavalues[0].value, data:data[?maximum!=null].{t:timeStamp, v:maximum}}}" -o json
```

返回每个指标下、按 LUN 分组的逐区间 `{t, v}` 序列（`<interval>` 同步骤 5 的选取规则）。用 `lun` 把同一块盘的 5 个指标对应起来，再按「计算口径」同区间对齐求峰。

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

## 计算口径

对每块盘（「同分钟合计」口径，与截图一致）：
- **合计 IOPS 峰值**：把读 IOPS 与写 IOPS 两个序列**按时间戳 `t` 对齐**，逐分钟求和（读 v + 写 v），取和最大的那一分钟为合计峰值；该分钟的读值、写值即报告里的「读 x / 写 y」，该分钟的时间戳即峰值时间。
- **合计吞吐峰值（MB/s）**：同理对齐读吞吐与写吞吐序列逐分钟求和，取最大分钟，再 ÷ 1000000 转为 MB/s。
- **延迟峰值**：延迟是单一序列，无需读写合计，直接取该盘 `Latency` 序列的最大 v（毫秒）及其时间戳。
- **盘级是否触顶**：把合计 IOPS 峰值与该盘 IOPS 上限直接比较、合计吞吐峰值与该盘吞吐上限直接比较，任一峰值 ≥ 对应上限即触顶（不计算百分比利用率）。

VM 级：
- **VM 合计 IOPS 峰值** = 所有盘（OS + 各数据盘）合计 IOPS 峰值之和。
- **VM 合计吞吐峰值** = 所有盘合计吞吐峰值之和。
- **VM 是否触顶**：VM 合计 IOPS 峰值与 `UncachedDiskIOPS` 比较、VM 合计吞吐峰值与 (`UncachedDiskBytesPerSecond` ÷ 1000000) MB/s 比较，任一峰值 ≥ 对应上限即触顶。

> IOPS / 吞吐峰值一律取「同一分钟内读+写之和的最大值」（同分钟合计），不要用“读峰值+写峰值”跨分钟相加；延迟是单序列，直接取最大。判断只比较「峰值 vs 上限」，报告里不展示利用率百分比。

## SKU 上限对照表

单盘 IOPS / 吞吐(MB/s) 上限（数据来源：https://learn.microsoft.com/azure/virtual-machines/disks-types）。
**「等级」反推规则**：Standard/Premium SSD v1 / HDD 盘的 `tier` 字段常为空，此时用 `sizeGB` 在下表里**向上取整到第一个 ≥ 容量的档位**，该行的「档位」即为等级名（如 512 GB 的 Premium → P20，512 GB 的 Standard HDD → S20），同时取该行 IOPS / MB/s 为上限。

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

磁盘只分**正常 / 异常**两档（不设「关注」中间态），以下任一条件命中即判为异常：

| 结论 | 判定条件 |
| --- | --- |
| ✅ 正常 | 所有盘级与 VM 级的 IOPS / 吞吐峰值均**未达到各自上限**，且所有盘延迟峰值 ≤ 200ms |
| ❌ 异常 | 任一盘级或 VM 级的 IOPS / 吞吐峰值**达到或超过上限**（已触顶），**或** 任一盘延迟峰值 > 200ms |

- **盘级瓶颈**：某块盘 IOPS 或吞吐峰值达到自身上限 → 该盘已触顶，需升 SKU / 升容量档位。
- **VM 级瓶颈**：**IOPS 与吞吐两个维度都要判断**——单盘没满但 VM 合计 IOPS 或吞吐任一达到未缓存上限 → VM 规格限制了总 IO，需升 VM SKU。
- **延迟风险**：任一盘延迟峰值 > 200ms 即判为异常（即使 IOPS / 吞吐未满）→ 可能是 HDD / 低档 SSD 或后端拥塞，建议升 Premium SSD 并排查。

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
- 是否异常：<✅ 正常 / ❌ 异常>，并简述依据（哪块盘/哪个维度触顶、峰值多少/上限多少、最大延迟多少）。
- 平台关联：<仅在磁盘判异常时写：诊断窗内有平台事件/当前非 Available → 可能与底层平台有关；无事件且 Available → 已排除平台因素。磁盘正常时此行可省略>
- 风险判断：<低 / 中 / 高>，一句话说明（如各盘均远低于上限，运行稳定）。
- 建议动作：1) <可执行建议> 2) <可执行建议>（正常则写「无需处理，继续观察」）。
- 参考文档：<由你根据结论自行推荐 1 条最相关的 Microsoft Learn 官方文档链接，
  不要写死；触顶可给磁盘/VM 升配文档，延迟高可给磁盘性能排查文档，正常可给磁盘指标监控文档>

———— 详细数据 ————
三、磁盘指标
  [OS盘] 名称=<盘名>  SKU=<sku>  容量=<sizeGB> GB
    SKU 上限：IOPS=<上限>，吞吐=<上限> MB/s，等级=<tier>  磁盘类型=<family>
    IOPS峰值（同分钟合计）：<r+w> IOPS（读 <r> / 写 <w>）时间=<峰值时间>
    吞吐峰值（同分钟合计）：<rm+wm> MB/s（读 <rm> / 写 <wm>）时间=<峰值时间>
    磁盘延迟峰值：<lat> ms（时间=<时间>）

  [数据盘·<tier> / <family> ×<块数>]
    SKU 上限：IOPS=<上限>，吞吐=<上限> MB/s，等级=<tier>  磁盘类型=<family>
    · ✅ 全部 <n> 块正常 LUN=<逐块列出 LUN 及容量，如 0(512GB), 1(512GB)>（IOPS 峰值=<值>，延迟峰值=<值>ms）

  [数据盘·<tier> / <family> ×<块数>] 共 <n> 块：⚠️ 异常 <异常数> / ✅ 正常 <正常数>
    SKU 上限：IOPS=<上限>，吞吐=<上限> MB/s  磁盘类型=<family>（每盘上限）
    · ⚠️ LUN <lun> 名称=<盘名> 容量=<sizeGB> GB [异常：延迟<lat>ms(>200ms)]
       IOPS峰值（同分钟合计）：<r+w> IOPS（读 <r> / 写 <w>）时间=<峰值时间>
       吞吐峰值（同分钟合计）：<rm+wm> MB/s（读 <rm> / 写 <wm>）时间=<峰值时间>
       磁盘延迟峰值：<lat> ms（时间=<时间>）

  【每盘上限可能不同的类型（Premium SSD v2 / Ultra Disk）——逐盘展开，每块标自己的上限，不共用组头上限】
  [数据盘·<family> ×<块数>] 共 <n> 块：⚠️ 异常 <异常数> / ✅ 正常 <正常数>
    磁盘类型=<family>（每块盘 IOPS/吞吐由用户单独配置，下方逐盘标注）
    · ✅ LUN <lun> 名称=<盘名> 容量=<sizeGB> GB（上限 <iops> IOPS / <mbps> MB/s）
       峰值 IOPS=<实测峰值>，吞吐峰值=<实测> MB/s，延迟峰值=<实测>ms
    · ⚠️ LUN <lun> 名称=<盘名> 容量=<sizeGB> GB（上限 <iops> IOPS / <mbps> MB/s）[异常：IOPS峰值<r+w>≥上限<iops>]
       IOPS峰值（同分钟合计）：<r+w> IOPS（读 <r> / 写 <w>）时间=<峰值时间>
       吞吐峰值（同分钟合计）：<rm+wm> MB/s（读 <rm> / 写 <wm>）时间=<峰值时间>
       磁盘延迟峰值：<lat> ms（时间=<时间>）

  [VM 级合计] IOPS 峰值=<所有盘合计> IOPS，吞吐峰值=<所有盘合计> MB/s
  [VM 级未缓存上限] IOPS=<UncachedDiskIOPS>，吞吐=<未缓存吞吐 MB/s> MB/s

四、运行状况（Resource Health）
  诊断窗内平台事件：<无 / 有：简述 time∈[start,end] 的最相关一条（北京时间 + 计划内/计划外 + 简要说明）>
  窗口内平台状态：<窗内有记录→取窗内最新一条 state（Available=正常 / Unavailable / Degraded）；窗内无记录→无平台事件（视为正常）>
```

- 数据盘**按「档位 + 磁盘类型」分组**：每组标题行 `[数据盘·<等级> / <类型> ×<块数>]`。
  - 组内全部正常 → 标题行不加「共 N 块」后缀，正文聚合成一行 `· ✅ 全部 N 块正常 LUN=0(512GB), 1(512GB)…`，**逐块列出 LUN 号与容量**。括号里的峰值：**组内只有 1 块时**直接写该盘的「IOPS 峰值=…，延迟峰值=…ms」；**组内多于 1 块时**才写「组内最高 IOPS 峰值=…，最高延迟峰值=…ms」（表示这组里最高的一块）。
  - 组内有异常 → 标题行加 `共 N 块：⚠️ 异常 X / ✅ 正常 Y`，把**异常盘逐块展开**（每块以 `· ⚠️ LUN …` 起头），正常盘可省略。
  - `等级`：Premium SSD v2 / Ultra 等无 tier 的类型，SKU 上限行省略「等级=」，标题写为 `[数据盘·<类型> ×<块数>]`。
- **Premium SSD v2 / Ultra Disk 特别处理**：这两类盘的 IOPS/吞吐是每块盘由用户单独配置的（同一组内可能 LUN1=3000、LUN2=5000 不同），所以：
  - **不在组头写统一 SKU 上限**（组头只标磁盘类型）；
  - **一律逐盘展开**（即使全部正常也不聚合成一行），每块盘在本行括号里标出**自己的上限** `（上限 <iops> IOPS / <mbps> MB/s）`；
  - 上限取自步骤 3 该盘的 `diskIOPSReadWrite`/`diskMBpsReadWrite`；是否触顶用该盘实测峰值与该盘自己的上限直接比较。
- 异常盘的 `[异常：…]` 只列**真正超标的维度**（延迟 >200ms、IOPS 峰值达到/超过上限、吞吐峰值达到/超过上限），写成「峰值≥上限」的直观对比，**不要用百分比利用率**。多项用中文逗号分隔。
- **不要在报告里展示「利用率」百分比**；是否异常只靠「峰值 vs 上限」和「延迟 vs 200ms」判定，呈现给用户的是峰值与上限的原始数值。
- IOPS / 吞吐峰值一律取「同分钟合计」（同一分钟内读+写之和的最大值），读/写明细为该峰值分钟的读值与写值；延迟为单序列峰值。
- **逐块盘明细里的 IOPS / 吞吐 / 延迟峰值是该盘的确定实测值，一律用「=」直接写数值，禁止用「≤」**（「≤」无意义：峰值就是峰值）。只有把**多块盘**聚合成一行时，才用「组内最高 …峰值=<组内最大值>」表示这组里最高的一块；单块盘直接写该盘峰值，不用「组内最高」。
- 数值保留 1 位小数；时间格式 `YYYY-MM-DD HH:MM:SS`；取不到的值写 N/A，不臆造。
- **「一、主机信息」的机型 SKU 行必须完整列出括号内的 vCPU / 内存**（来自步骤 2），不得只写 SKU 名而省略规格；取不到才写 N/A。

## 注意事项

- 本技能为**只读诊断**，不修改任何资源，不进入 VM 内部。同时覆盖 OS 盘与所有数据盘，按 LUN 区分。
- 所有命令只读（`az ... show` / `list` / `monitor metrics list`），绝不执行变更类命令。
- 时间一律按北京时间向用户呈现；调用 az 时换算为 UTC。
- 若指标缺失（VM 已释放 / 未装 Azure Monitor Agent），如实告知用户，不要编造数据。
- 主机名必填；资源组缺省时使用默认资源组 `xiaomi-azure`，不要追问。
