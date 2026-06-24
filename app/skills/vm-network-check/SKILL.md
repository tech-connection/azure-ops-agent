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
> 连接数：**未开 AC** 从 VM 资源取（`Inbound Flows` / `Outbound Flows`）；**已开 AC** 从主 NIC 资源取（`CurrentTotalFlowsIn` / `CurrentTotalFlowsOut`）。
> ⚠️ 用完整 NIC `id` 作 `--resource` 时**不要再带 `--resource-type`**（两者同时给会触发 az usage error）。

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

> ⚠️ 主网卡 `id` 必须从本命令返回的 `nics[].id` 原值逐字照抄（大小写敏感）。Azure 资源名区分大小写，NIC 名与 VM 名没有固定规律（常见如 `<vm名小写>_z1`，但不可靠），**绝不要用 VM 名拼接 / 小写化猜 NIC 名**，否则步骤 3 会 NOT_FOUND。

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
az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Network In Total" "Network Out Total" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, peak:max_by(timeseries[0].data[?maximum!=null], &maximum)}" -o json
```

**注意：查询里用 `max_by(...)` 让 az 服务端直接算出峰值点，不要改成返回整段逐分钟序列**（长时间窗下整段序列会有上千个点，返回体过大会被截断，导致你误以为“没取到数据”而填 N/A）。

返回每个指标的峰值点 `peak = {timeStamp, maximum}`（单位**字节**，为该分钟内总量）。
- 取 `peak.maximum` 为峰值字节数，`peak.timeStamp` 为峰值时间（UTC +8h → 北京时间）。
- **带宽单位自适应**（避免小值被四舍五入成 0.0 看起来像无数据）：峰值字节数 `b`——若 `b >= 1048576` 用 `b/1048576` 表为 `MB`（保留 1–2 位小数）；若 `1024 <= b < 1048576` 用 `b/1024` 表为 `KB`；若 `b < 1024` 直接用 `B`。单位随值标在数字后。
- **只有查询确实没返回任何数据点（`peak` 为空/null）才写 N/A**；只要有值，哪怕很小也要按上面单位换算如实展示，不得因“太小”写 N/A。

### 步骤 5：查询入/出连接数（Flows）峰值

**先看步骤 3：是否已开 AC（`auxMode==AcceleratedConnections` 且 `auxSku` 有效）。**

- **未开 AC** → 连接数从 **VM** 资源取（指标名 `Inbound Flows` / `Outbound Flows`）：

  ```
  az monitor metrics list --resource <vm> --resource-group <rg> --resource-type Microsoft.Compute/virtualMachines --metric "Inbound Flows" "Outbound Flows" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, peak:max_by(timeseries[0].data[?maximum!=null], &maximum)}" -o json
  ```

  数据来源标记为 **VM**。

- **已开 AC** → 连接数改从**主 NIC** 资源取（指标名不同：`CurrentTotalFlowsIn` / `CurrentTotalFlowsOut`），
  用步骤 1 的主网卡 `id` 作为 `--resource`：

  ```
  az monitor metrics list --resource <primaryNicId> --metric "CurrentTotalFlowsIn" "CurrentTotalFlowsOut" --start-time <start-utc> --end-time <end-utc> --interval PT1M --aggregation Maximum --query "value[].{m:name.value, peak:max_by(timeseries[0].data[?maximum!=null], &maximum)}" -o json
  ```

  ⚠️ 这里用的是**完整 NIC `id`**，所以**不带 `--resource-type`**（完整 ID + `--resource-type` 会报 usage error）。
  把 `CurrentTotalFlowsIn` 当作入站连接、`CurrentTotalFlowsOut` 当作出站连接，数据来源标记为 **NIC**。

**同样用 `max_by(...)` 让 az 服务端直接返回峰值点，不要改成返回整段序列。**
取 `peak.maximum` 为入/出站连接峰值（单位 flows，取整），`peak.timeStamp` 为峰值时间（UTC +8h → 北京）。
**仅当 `peak` 为空/null（查询确实无数据）才写 N/A。**

### 步骤 6：运行状况（Resource Health，判断异常是否与底层平台有关）

时间窗与上面网络查询**保持一致**。先从步骤 1 的 `id` 里取 `<subId>`（`/subscriptions/` 之后、下一个 `/` 之前那段），调 ARM REST（整条 URL 是 `--url` 的单个元素，`?`/`&`/`$` 原样写）：

```
az rest --method get --url "https://management.azure.com/subscriptions/<subId>/resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2024-02-01&$expand=recommendedactions" --query "value[].{time:properties.occuredTime, state:properties.availabilityState, title:properties.title, cause:properties.healthEventCause, summary:properties.summary}" -o json
```

数组按时间倒序，每条是一次**健康状态变化**（Resource Health 只在状态变化时记一条，不是逐分钟连续数据；状态会一直延续到下次变化）。`time` 是 UTC，+8 小时换算北京时间。**注意**：少数记录是计划维护描述（如 `Freeze Update Succeeded`），其 `state`(availabilityState) 为 **null**，并不代表“状态未知”，不可当可用性状态用。

判定只看**诊断时间窗 `[start, end]`** 内的记录，窗口外的历史一律不展示、不参与判断：
- **诊断窗内平台事件**：只列 `time` 落在 `[start, end]` 内的记录（含计划维护与可用性变化）；窗内无任何记录 → 写「无」。
- **窗口内是否正常**：窗内无记录 → 平台侧无事件，判正常（不要回看窗外去找“当前状态”，那会把几周前的旧状态 / `Unknown` 误当成本次结果）；窗内有记录 → 看窗内**最新一条 `state` 非 null** 的记录，`Available` = 正常，`Unavailable` / `Degraded` = 异常。
- **用途**：网络指标若判为异常，且窗内有非 `Available` 事件 → 在结论里点明异常**可能与底层平台有关**；窗内无事件或均 `Available` → 可排除平台因素，问题更可能在业务/系统侧。

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
| ⚠️ 无法判定 | 连接数峰值为 N/A（两个方向的连接数查询都无数据） |
| ✅ 正常 | 拿到连接数数据，且峰值**未达到**有效上限 |
| ❌ 异常 | 拿到连接数数据，且峰值**达到或超过**有效上限（已触顶，可能丢连接 / 建连失败） |

> **重要：没拿到连接数指标（峰值 N/A）时绝不能判“✅ 正常”。**无数据就是“⚠️ 无法判定”，结论里如实说明“本次未取到连接数指标，无法给出正常/异常结论”，并提示可能原因（该时间窗无数据 / VM 当时未运行 / 时间窗太早超出保留期）和建议（换个时间窗重试）。不要“无数据但当作正常”。

- **连接数瓶颈**：峰值达到/超过有效上限 → 建议开启或升级加速连接（AC，auxSku A1→A8 上限递增）、或拆分流量到多 VM。
- **加速网卡未启用**：若 `accel==false`，在建议里提示开启加速网卡可显著降低延迟、提升 PPS（需停机改配）。
- **带宽**：入/出带宽峰值仅作**参考展示**（Azure 不在平台指标里给硬性带宽上限）；若带宽峰值明显偏高且伴随业务变慢，建议结合 VM SKU 的网络带宽规格评估是否升配。带宽本身不单独触发“异常”。

## 输出格式（严格照此组装，逐项填值，不要加寒暄/表情/多余前后缀）

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
- 是否异常：<✅ 正常 / ❌ 异常 / ⚠️ 无法判定>，并简述依据（连接数峰值多少 / 有效上限多少 / 是否触顶；**未取到连接数指标时写“无法判定”并说明原因，不得写正常**）。
- 平台关联：<仅在网络判异常时写：诊断窗内有平台事件/当前非 Available → 可能与底层平台有关；无事件且 Available → 已排除平台因素。网络正常时此行可省略>
- 风险判断：<低 / 中 / 高>，一句话说明（如连接数远低于上限，网络运行稳定）。
- 建议动作：1) <可执行建议> 2) <可执行建议>（正常则写「无需处理，继续观察」）。
- 参考文档：<由你根据结论自行推荐 1 条最相关的 Microsoft Learn 官方文档链接，
  不要写死；连接数偏高可给网络吞吐/加速连接文档，带宽偏高可给 VM 网络带宽文档，正常可给网络指标监控文档>

———— 详细数据 ————
三、网络指标
  入站带宽峰值：<数值><单位 MB/s或KB/s或B/s>  时间=<峰值北京时间>
  出站带宽峰值：<数值><单位 MB/s或KB/s或B/s>  时间=<峰值北京时间>
  入站连接（Inbound Flows）峰值：<n> flows  时间=<峰值北京时间>  数据来源=<VM/NIC>
  出站连接（Outbound Flows）峰值：<n> flows  时间=<峰值北京时间>  数据来源=<VM/NIC>
  加速网卡：<已启用 / 未启用 / 未知>  AC：<见下方说明>
  连接数有效上限：<有效上限> flows（<档位说明，如 “AC A2” 或 “16–31 vCPU 档”>）

四、运行状况（Resource Health）
  诊断窗内平台事件：<无 / 有：仅列 time∈[start,end] 的记录，简述最相关一条（北京时间 + 计划内/计划外 + 简要说明）>
  窗口内平台状态：<窗内有记录→取窗内最新一条 state（Available=正常 / Unavailable / Degraded）；窗内无记录→无平台事件（视为正常）>
```

- `AC：` 这一行按实际情况写：
  - 已开 AC → `已启用（<auxSku>：连接数上限 <上限/10000> 万）`，例如 `已启用（A2：连接数上限 200 万）`。
  - 未开 AC → `未启用（按 <vCPU 档位> 档位，连接数上限 <非 MANA 上限>`，64+ vCPU 再补 `，开 MANA 可到 2,000,000`，最后加 `）`。
- 数值：带宽按单位自适应表示（MB/s 保留 1–2 位小数，KB/s 同理，B/s 取整），**小值也要如实转 KB/s 或 B/s 展示，不得因太小写 N/A**；连接数（flows）取整，可用千分位。
- 时间格式 `YYYY-MM-DD HH:MM:SS`；**只有查询确实无数据时才写 N/A**，不臆造也不要把小值当 N/A。
- **「一、主机信息」的机型 SKU 行必须完整列出括号内的 vCPU / 内存**（来自步骤 2 的查询），不得只写 SKU 名而省略规格；只有查不到才写 N/A。
- 直接把组装好的中文报告输出给用户，**不要展示命令或 JSON**。

## 注意事项

- 本技能为**只读诊断**，不修改任何资源、不进入 VM 内部；`run_az` 只允许只读命令。
- 连接数：未开 AC 取 VM 的 `Inbound/Outbound Flows`；已开 AC 取主 NIC 的 `CurrentTotalFlowsIn/Out`（用完整 NIC `id` 作 `--resource`，**不带 `--resource-type`**）；
- 时间一律按北京时间向用户呈现；调用 az 时换算为 UTC。
- 若指标缺失（VM 已释放 / 未装 Azure Monitor Agent / 该 SKU 不暴露连接数指标），如实告知用户，相应数值写 N/A，不要编造。
- 主机名必填；资源组缺省时使用默认资源组 `xiaomi-azure`，不要追问。
