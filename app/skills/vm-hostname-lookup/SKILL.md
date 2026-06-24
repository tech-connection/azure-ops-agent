---
name: vm-hostname-lookup
description: >-
  在「主机名（计算机名 / computerName）」与「实例 ID（VM 资源名）」之间互转（只读），只看输入形态定方向：
  输入是主机名/FQDN（带点分域名，如 ams1-hadoop-pegasus-srv-st98.aznl.idc.xiaomi.com）→ 转成实例 ID（用法一）；
  输入是实例 ID/资源名（无域名后缀，如 westeurope-1764573798338-wQVvVZaH-1）→ 转成主机名（用法二）。
  典型问法：「查下面主机名对应的实例 ID」「查下面实例 ID 对应的主机名/计算机名」。
  「主机名」「计算机名」computerName 是同义词；主机名可能被飞书转成 Markdown 链接或带 http(s):// 前缀。
  只要输入是 VM 主机名或实例 ID，**都走本技能，不要回退到 azure-qa**。
---

# 主机名 ⇄ 实例 ID 互查技能（由 run_az 工具执行）

本技能在 **主机名（计算机名 computerName）** 与 **实例 ID（VM 资源名）** 之间做**只读**互转，
不查指标、不进 VM 内部。你不写代码，只按本文给出的 `az` 命令用工具 **`run_az`** 执行，再按「输出格式」组织中文回复。

- **计算机名一律取 `instanceView.computerName`**（Guest Agent 实时上报的 FQDN，等同门户「计算机名」筛选）；
  不要用 `az vm list` 的 `osProfile.computerName`（会被截断为 15 字符、与实时主机名不一致）。
- `run_az`：执行**一条只读** `az` 命令，`args` 是去掉开头 `az` 后的参数数组（每个参数一个元素）；
  订阅后端自动注入，**命令里不要带 `--subscription`**。

## 判方向（只看输入形态）

| 输入形态 | 识别特征 | 样例 | 方向 | 返回 |
| --- | --- | --- | --- | --- |
| **主机名 / FQDN** | 带点分域名（含 `.` 与域名后缀） | `ams1-hadoop-pegasus-srv-st98.aznl.idc.xiaomi.com` | **用法一** | 实例 ID |
| **实例 ID / 资源名** | 无域名后缀，形如 `<region>-<数字>-<随机串>-N` | `westeurope-1764573798338-wQVvVZaH-1` | **用法二** | 主机名 |

- 用户可能一次给多个（多行/逗号/空格分隔），逐个按同一方向处理。
- 预处理：主机名可能被转成 Markdown 链接或带 `http(s)://` 前缀（如 `[ams1-xxx](http://ams1-xxx)`），
  **先剥离成裸 FQDN**（取 `[]` 内文本、去掉 `http(s)://` 与结尾 `/`）再判方向。
- 「主机名」「计算机名」是同一个东西，用户常混用；只要输入是主机名或实例 ID，**始终走本技能，不得回退 azure-qa**。

---

## 用法一：主机名（计算机名）→ 实例 ID（等同门户「计算机名」筛选）

门户「计算机名」筛选背后是 **Azure Resource Graph** 按 `instanceView.computerName` 检索。
本技能用 `az rest` 调 Resource Graph 查询端点（只读）实现同样效果。

### 步骤 1：取当前订阅 ID（用于限定查询范围，与门户选定订阅一致）

```
az account show --query id -o json
```

`args = ["account","show","--query","id","-o","json"]`

返回形如 `"483ab1e0-a746-4f34-8276-53e640d6ab09"`（带引号的 JSON 字符串），去掉引号取其值，记为 `<subId>`。
> 注意：**必须用 `-o json`，不要用 `-o tsv`**。`run_az` 会对命令输出做 JSON 解析，`-o tsv` 返回纯文本会触发 `PARSE_ERROR`。

### 步骤 2：用 Resource Graph 按计算机名反查（支持一次多台）

把 `<subId>` 与用户给的一个或多个主机名填入下面的 **`--body`（单个 JSON 字符串元素）**。
用 KQL 的 **`in~(...)`**（忽略大小写的多值精确匹配）一次查多台：单台就只写一个元素，多台就逗号隔开，每个主机名用单引号包起。照抄结构只替换占位符：

```
az rest --method post --url "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01" --body "{\"subscriptions\":[\"<subId>\"],\"query\":\"Resources | where type =~ 'microsoft.compute/virtualmachines' | extend cn = tostring(properties.extended.instanceView.computerName) | where cn in~ ('<hostname1>','<hostname2>') | project computerName=cn, instanceId=tolower(tostring(split(id,'/')[-1])), resourceGroup, location, id | limit 50\"}" -o json
```

`args = ["rest","--method","post","--url","https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01","--body","{\"subscriptions\":[\"<subId>\"],\"query\":\"Resources | where type =~ 'microsoft.compute/virtualmachines' | extend cn = tostring(properties.extended.instanceView.computerName) | where cn in~ ('<hostname1>','<hostname2>') | project computerName=cn, instanceId=tolower(tostring(split(id,'/')[-1])), resourceGroup, location, id | limit 50\"}","-o","json"]`

- `in~ ('a','b',...)` 是**忽略大小写的多值精确匹配**（单台查询就只传一个值，与门户「计算机名 等于」一致）。
- **实例 ID 取 `tolower(split(id,'/')[-1])`**：即资源 ID `/subscriptions/.../virtualMachines/WESTEUROPE-1745825608284-K0NQ9ARF-1` 的**最后一段资源名，并转为小写** `westeurope-1745825608284-k0nq9arf-1`（门户/资源 ID 里该段可能是大写或混合大小写，统一转小写）。
- 返回结构形如 `{"count":N, "data":[{"computerName":"...","instanceId":"...","resourceGroup":"...","location":"...","id":"..."}], ...}`。从 `data` 数组取结果，**每个主机名可能对应 0、1 或多台**，按 `computerName` 分组呈现。
- 用户给了多个主机名但某些没出现在 `data` 里 → 那些未查到（可能已释放 / Guest Agent 未运行 / 名称有出入）。
- **一台都没匹配到**（`data` 为空）→ 见下方「查不到时」。

### 查不到时（data 为空）：改用模糊包含再试一次

主机名可能拼写片段、大小写或后缀不同。把上面 KQL 的 `where cn in~ (...)` 换成
**包含匹配** `where cn contains '<关键片段>'`（`<关键片段>` 用用户给的主机名主体，去掉域名后缀如 `.xiaomi.com` 更易命中），重跑步骤 2（多个关键片段可用 `where cn contains 'a' or cn contains 'b'`）：

```
... | where cn contains '<关键片段>' | project computerName=cn, instanceId=tolower(tostring(split(id,'/')[-1])), resourceGroup, location, id | limit 50 ...
```

- 仍为空 → 如实告知「在当前订阅未查到计算机名匹配 <hostname> 的运行中 VM」，并提示：已释放（deallocated）/ Guest Agent 未运行的 VM 不上报 computerName，门户也查不到；可改用实例 ID 直接查。

---

## 用法二：实例 ID（VM 资源名）→ 主机名（计算机名）

直接对该实例取实时 instanceView 的 computerName（与 vm-cpu-check 取主机名方式一致）。
**一次查多台时：对每个实例各发一条 `az vm get-instance-view`，并发执行（同一轮里一次性发起多个 `run_az` 调用，不要一台等完再查下一台）。**

### 参数

- `<vm_name>`：实例 ID / VM 资源名，逐字照抄，结尾的 `-1` / `-N` 是名字一部分不可截断。
  - 若用户给的是**完整 Resource ID**（`/subscriptions/.../resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<name>`），
    从中解析出 `<rg>` 与 `<name>` 用于下面命令。
- `<resource_group>`：用户给了就用用户的；没给则默认 `xiaomi-azure`（不要反问）。

### 命令（每台一条；多台时并发发起）

```
az vm get-instance-view -g <resource_group> -n <vm_name> --query "instanceView.computerName" -o json
```

`args = ["vm","get-instance-view","-g","<rg>","-n","<vm_name>","--query","instanceView.computerName","-o","json"]`

- 返回形如 `"ams-mione-nacos01.aznl.idc.xiaomi.com"`，即该实例当前的 OS 主机名（计算机名）。
- 多台时：收齐所有并发调用的返回，按「实例 ID → 主机名」逐台列出；单台查不影响其他台。
- 返回 `null` / 空 → 该 VM 可能已释放或 Guest Agent 未运行，无法上报主机名，写 N/A 并说明。
- 若返回 `{"error":"NOT_FOUND",...}` → 该资源组下无此实例名；提示用户确认资源组或实例 ID（默认资源组 `xiaomi-azure` 查不到时尤其要提示换资源组）。

---

## 输出格式（严格照此组织，不加寒暄/表情/多余前后缀）

### 用法一（主机名 → 实例 ID）

单台或多台都用同一种简洁格式（按计算机名逐台列出，只给实例 ID，不带资源组/区域/完整资源 ID）：
```
🔎 计算机名查找（数据来源：Azure Resource Graph，等同门户「计算机名」筛选）

· <hostname1> → 实例 ID：<instanceId1>
· <hostname2> → 实例 ID：<instanceId2>
...
```

- 某个计算机名对应多台时，在该名下分行列出多个实例 ID。
- 用户传了多个主机名、部分未查到时，未查到的另起一行：`未查到：<hostnameX>`。

查不到：
```
🔎 计算机名查找

在当前订阅未查到计算机名匹配「<hostname>」的 VM。
可能原因：实例已释放（deallocated）/ Guest Agent 未运行（不上报计算机名），或名称有出入。
建议：核对计算机名拼写，或直接用实例 ID（资源名）查询。
```

### 用法二（实例 ID → 主机名）

单台：
```
🔎 主机名查找（数据来源：实例 instanceView）

实例 ID（资源名）：<vm_name>
资源组：<resource_group>
OS 主机名（计算机名）：<computerName 或 N/A>
```

多台（并发查询后逐台列出）：
```
🔎 主机名查找（数据来源：实例 instanceView）

· <vm_name1> → OS 主机名：<computerName1 或 N/A>
· <vm_name2> → OS 主机名：<computerName2 或 N/A>
...
```

---

## 注意事项

- 本技能**只读**，不修改资源、不进 VM 内部、不查性能指标。
- 计算机名一律取 **instanceView.computerName**（用法一经 Resource Graph、用法二经 get-instance-view）。
- 已释放 / Guest Agent 未运行的 VM 不上报 computerName，用法一查不到、用法二返回 N/A，均如实告知，不要编造。
