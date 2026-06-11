---
name: vm-network-check
description: >-
  诊断 Azure 虚拟机的网络使用情况：入/出带宽、入/出连接数（Flows）峰值，结合是否启用
  加速网卡 / 加速连接（AC）给出连接数上限对比与建议（基于 Azure Monitor 平台指标，只读）。
  当用户询问某台 VM 的「网络 / 带宽 / 流量 / 连接数 / flow」、网络告警、或反馈网络慢/丢包时使用。
  典型问法：「主机 xxx 网络带宽多少」「这台机器连接数高不高」「近 30 分钟流量有没有突增」。
---

# VM 网络诊断技能（标准 skill 模式 · 由 run_az 工具执行）

本技能对一台 Azure 虚拟机的网络使用情况做**只读**诊断。
你（模型）**不写代码、不调用任何脚本**，而是严格按本文件给出的 `az` 命令，
用工具 **`run_az`** 逐条执行，拿到结果后按「连接数上限对照表」「判断标准」「输出格式」组装中文报告。

> 只读取 Azure 控制面的网络配置与平台指标（带宽 `Network In/Out Total`、连接数 `Inbound/Outbound Flows`），
> **不进入 VM 操作系统内部**（不执行 run-command、不看 netstat / ss）。OS 级连接根因不在本技能范围。
> 启用加速连接（AC）的 VM，连接数指标改从主 NIC 资源读取（VM 聚合值在 AC 模式下不再代表真实并发）。

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

### 步骤 1：VM 基础信息 + 主网卡引用

```
az vm show -d -g <rg> -n <vm> --query "{id:id, name:name, location:location, vmSize:hardwareProfile.vmSize, osType:storageProfile.osDisk.osType, powerState:powerState, nics:networkProfile.networkInterfaces[].{id:id, primary:primary}}" -o json
```

从结果取：`name`、`location`、`vmSize`、`powerState`（展示时去掉前缀 `VM ` → `running`）、`id`（资源 ID，形如 `/subscriptions/<subId>/resourceGroups/...`，下一步取 `<subId>`）。
从 `nics` 里挑出**主网卡**：优先 `primary==true` 的那张；若都没标 primary 就取第一张。记下它的 `id`（完整资源 ID，下一步用）。

确认 VM 存在后，再执行一条取**当前主机名**（Guest Agent 上报的 OS 主机名，对应门户「计算机名称」，与实例 ID / 资源名不同）：

```
az vm get-instance-view -g <rg> -n <vm> --query "instanceView.computerName" -o json
```

返回形如 `"ams3-mife-fe47.aznl.idc.xiaomi.com"`，记为 `<computerName>`，填入「一、主机信息」的主机名行；取不到写 N/A。

> ⚠️ 若 `powerState` 不是 `VM running`（如 `VM deallocated`），网络指标可能缺失，应如实告知用户。
> 若结果是 `{"error": "NOT_FOUND", ...}`，说明 VM 不存在/已释放，直接如实告知用户，结束。

### 步骤 2：SKU 规格（vCPU / 内存，必查）

连接数档位要用 vCPU，且「一、主机信息」机型 SKU 行的 vCPU / 内存都来自这里，不得省略或臆造。
先从步骤 1 的 `id` 里取 `<subId>`（`/subscriptions/` 之后、下一个 `/` 之前那段），
再用 `<location>`、`<vmSize>` 调 Compute SKUs API（服务端按 location 过滤，比 `az vm list-skus` 快几十倍）：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/providers/Microsoft.Compute/skus?api-version=2021-07-01&$filter=location eq '<location>'" --query "value[?name=='<vmSize>'] | [0].capabilities[?name=='vCPUs' || name=='MemoryGB'].{name:name,value:value}" -o json
```

从结果取两个 value：`vCPUs`（整数，同时用于「连接数上限对照表」落档）、`MemoryGB`（内存 GB），填入「一、主机信息」的机型 SKU 行。只有当调用确实返回空时才写 N/A。

### 步骤 3：读取主网卡的加速网卡 / 加速连接（AC）配置

用步骤 1 选出的主网卡 `id`：

```
az network nic show --ids <primaryNicId> --query "{accel:enableAcceleratedNetworking, auxMode:auxiliaryMode, auxSku:auxiliarySku}" -o json
```

- `accel`：主网卡是否启用**加速网卡**（true / false）。
- `auxMode` / `auxSku`：判断是否启用**加速连接（AC）**——当 `auxMode` 等于 `AcceleratedConnections` 且 `auxSku` ∈ {A1, A2, A4, A8} 时，视为已开 AC，`auxSku` 即 AC 档位；否则未开 AC。

### 步骤 4：查询入/出带宽峰值

```
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Network In Total" "Network Out Total" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, data:timeseries[0].data[?maximum!=null].{t:timeStamp, v:maximum}}" -o json
```

返回入/出带宽的逐分钟 `{t, v}` 序列（单位**字节**）。各取序列里最大的 `v` 为峰值字节数，
**÷ 1048576 转为 MB/s**，并记录该峰值点的时间戳（UTC +8h → 北京时间）。

### 步骤 5：查询入/出连接数（Flows）峰值

**先看步骤 3：是否已开 AC（`auxMode==AcceleratedConnections` 且 `auxSku` 有效）。**

- **未开 AC** → 连接数从 **VM** 资源取（指标名 `Inbound Flows` / `Outbound Flows`）：

  ```
  az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Inbound Flows" "Outbound Flows" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, data:timeseries[0].data[?maximum!=null].{t:timeStamp, v:maximum}}" -o json
  ```

  数据来源标记为 **VM**。

- **已开 AC** → 连接数改从**主 NIC** 资源取（指标名不同：`CurrentTotalFlowsIn` / `CurrentTotalFlowsOut`），
  用步骤 1 的主网卡 `id` 作为 `--resource`：

  ```
  az monitor metrics list --resource <primaryNicId> --resource-type Microsoft.Network/networkInterfaces --metric "CurrentTotalFlowsIn" "CurrentTotalFlowsOut" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, data:timeseries[0].data[?maximum!=null].{t:timeStamp, v:maximum}}" -o json
  ```

  把 `CurrentTotalFlowsIn` 当作入站连接、`CurrentTotalFlowsOut` 当作出站连接，数据来源标记为 **NIC**。

各取序列里最大的 `v` 为入/出站连接峰值（单位 flows，取整），并记录峰值时间。

### 步骤 6：运行状况（Resource Health，判断异常是否与底层平台有关）

时间窗与上面网络查询**保持一致**。先从步骤 1 的 `id` 里取 `<subId>`（`/subscriptions/` 之后、下一个 `/` 之前那段），调 ARM REST（整条 URL 是 `--url` 的单个元素，`?`/`&`/`$` 原样写）：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2024-02-01&$expand=recommendedactions" --query "value[].{time:properties.occuredTime, state:properties.availabilityState, title:properties.title, cause:properties.healthEventCause, summary:properties.summary}" -o json
```

数组已按时间倒序，**第 1 条为当前/最新状态**；`time` 是 UTC，+8 小时换算北京时间。
- **当前平台状态** = 第 1 条的 `state`（`Available` = 平台侧正常）。
- **诊断窗内是否有平台事件**：逐条看 `time`，只要有任意一条落在本次诊断时间窗 `[start, end]` 内，即「窗内有平台事件」（计划维护 / Unavailable / Degraded 等）。
- **用途**：网络指标若判为异常，且窗内有平台事件或当前非 `Available` → 在结论里点明异常**可能与底层平台有关**；若窗内无事件且当前 `Available` → 可排除平台因素，问题更可能在业务/系统侧。

## 连接数上限对照表

单 VM 推荐并发连接（Flows）上限（数据来源：https://learn.microsoft.com/azure/virtual-network/virtual-machine-network-throughput）。

**按 vCPU 档位（未开 AC 时用这张表，按步骤 2 的 vCPUs 落档）**

| vCPU 档位 | 非 MANA 上限 | MANA 上限 |
| --- | --- | --- |
| 2–7 vCPU | 100,000 | 100,000 |
| 8–15 vCPU | 500,000 | 500,000 |
| 16–31 vCPU | 700,000 | 700,000 |
| 32–63 vCPU | 800,000 | 800,000 |
| 64+ vCPU | 1,000,000 | 2,000,000 |

> 所有 SKU 保底 500K 总连接。**仅 64+ vCPU 档**才有 MANA / 非 MANA 差异（开 MANA = 200 万、未开 = 100 万），其余档位两者相同。

**按 AC 档位（已开 AC 时用这张表，按步骤 3 的 auxSku 落档；会覆盖 vCPU 默认档位）**

| AC 档位（auxSku） | 连接数上限 |
| --- | --- |
| A1 | 1,000,000 |
| A2 | 2,000,000 |
| A4 | 4,000,000 |
| A8 | 8,000,000 |

**有效上限（判断时用）**：
- 已开 AC → 取 AC 档位上限（A1/A2/A4/A8 对应上表）。
- 未开 AC → 取 vCPU 档位的**非 MANA 上限**（64+ vCPU 若确认开了 MANA 可用 200 万，否则按 100 万）。

## 判断标准（写“结论”时参考）

连接数只分**正常 / 异常**两档（不设中间态），以**入/出站连接数峰值中的较大者**与有效上限比较：

| 结论 | 判定条件 |
| --- | --- |
| ✅ 正常 | 连接数峰值**未达到**有效上限 |
| ❌ 异常 | 连接数峰值**达到或超过**有效上限（已触顶，可能丢连接 / 建连失败） |

- **连接数瓶颈**：峰值达到/超过有效上限 → 建议开启或升级加速连接（AC，auxSku A1→A8 上限递增）、或拆分流量到多 VM。
- **加速网卡未启用**：若 `accel==false`，在建议里提示开启加速网卡可显著降低延迟、提升 PPS（需停机改配）。
- **带宽**：入/出带宽峰值仅作**参考展示**（Azure 不在平台指标里给硬性带宽上限）；若带宽峰值明显偏高且伴随业务变慢，建议结合 VM SKU 的网络带宽规格评估是否升配。带宽本身不单独触发“异常”。

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
- 是否异常：<✅ 正常 / ❌ 异常>，并简述依据（连接数峰值多少 / 有效上限多少 / 是否触顶）。
- 平台关联：<仅在网络判异常时写：诊断窗内有平台事件/当前非 Available → 可能与底层平台有关；无事件且 Available → 已排除平台因素。网络正常时此行可省略>
- 风险判断：<低 / 中 / 高>，一句话说明（如连接数远低于上限，网络运行稳定）。
- 建议动作：1) <可执行建议> 2) <可执行建议>（正常则写「无需处理，继续观察」）。
- 参考文档：<由你根据结论自行推荐 1 条最相关的 Microsoft Learn 官方文档链接，
  不要写死；连接数偏高可给网络吞吐/加速连接文档，带宽偏高可给 VM 网络带宽文档，正常可给网络指标监控文档>

———— 详细数据 ————
三、网络指标
  入站带宽峰值：<x.x> MB/s  时间=<峰值北京时间>
  出站带宽峰值：<x.x> MB/s  时间=<峰值北京时间>
  入站连接（Inbound Flows）峰值：<n> flows  时间=<峰值北京时间>  数据来源=<VM/NIC>
  出站连接（Outbound Flows）峰值：<n> flows  时间=<峰值北京时间>  数据来源=<VM/NIC>
  加速网卡：<已启用 / 未启用 / 未知>  AC：<见下方说明>
  连接数有效上限：<有效上限> flows（<档位说明，如 “AC A2” 或 “16–31 vCPU 档”>）

四、运行状况（Resource Health）
  当前平台状态：<Available=正常 / Unavailable / Degraded / Unknown>
  诊断窗内平台事件：<无 / 有：简述最相关一条（北京时间 + 计划内/计划外 + 简要说明）>
```

- `AC：` 这一行按实际情况写：
  - 已开 AC → `已启用（<auxSku>：连接数上限 <上限/10000> 万）`，例如 `已启用（A2：连接数上限 200 万）`。
  - 未开 AC → `未启用（按 <vCPU 档位> 档位，连接数上限 <非 MANA 上限>`，64+ vCPU 再补 `，开 MANA 可到 2,000,000`，最后加 `）`。
- 数值：带宽（MB/s）保留 1 位小数；连接数（flows）取整，可用千分位。
- 时间格式 `YYYY-MM-DD HH:MM:SS`；取不到的值写 N/A，不臆造。
- **「一、主机信息」的机型 SKU 行必须完整列出括号内的 vCPU / 内存**（来自步骤 2 的查询），不得只写 SKU 名而省略规格；只有查不到才写 N/A。
- 直接把组装好的中文报告输出给用户，**不要展示命令或 JSON**。

## 注意事项

- 本技能为**只读诊断**，不修改任何资源、不进入 VM 内部；`run_az` 只允许只读命令。
- 启用加速连接（AC）的 VM，连接数取自**主 NIC** 指标（`CurrentTotalFlowsIn/Out`），已映射为 VM 端口径（入/出站连接）；
  此时不要再用 VM 上的 `Inbound/Outbound Flows`（AC 模式下不代表真实并发）。
- 时间一律按北京时间向用户呈现；调用 az 时换算为 UTC。
- 若指标缺失（VM 已释放 / 未装 Azure Monitor Agent / 该 SKU 不暴露连接数指标），如实告知用户，相应数值写 N/A，不要编造。
- 主机名必填；资源组缺省时使用默认资源组 `xiaomi-azure`，不要追问。
