---
name: azure-support-case
description: >-
  帮用户向微软提交 Azure 技术支持工单（Support Case / Ticket）。当用户在诊断出问题后说「帮我提个 case /
  开个工单 / 提交 Azure 支持 / 联系微软支持 / 报个故障给微软 / 提 ticket」时使用。
  本技能会先匹配问题分类、组装工单草稿、确认严重性，并在用户明确确认后才真正创建工单。
  典型问法：「帮我给这台 VM 提个工单」「这个问题提交给微软支持」「开个 case，严重性 B」「提单」。
---

# Azure 技术支持工单提交技能（标准 skill 模式 · 由工具执行）

本技能把「在 Azure Portal 里开技术支持工单」的流程自动化：匹配 Service + Problem Classification、
组装工单详情、确认严重性，最后调用工具真正创建工单。流程对齐 Azure Portal「帮助 + 支持 → 新建支持请求」
的五步向导（问题描述 → 解决方案 → 详细信息 → 查看 + 创建）。

> ⚠️ **创建工单是写操作，会真的向微软发起支持请求。必须等用户明确确认后，才能在「下一轮回复」里调用
> `run_az` 执行建单命令。禁止在出草稿的同一轮就创建工单。**

## 可用工具

本技能只用 **`run_az`** 一个工具（入参 `args` 是去掉开头 `az` 之后的参数数组，每个参数一个元素）：

- **只读查询**：`vm show` 取资源 ID、`support services list` / `support services problem-classifications list` 动态查分类。
- **受控写（建单）**：`support in-subscription tickets create`。这是 run_az 唯一放行的写操作，仅能在用户确认后调用。

> 建单命令的**全部参数都由你（模型）提供**，`run_az` 不会自动补任何参数（仅 `--subscription` 会由底层自动注入，你不要写）。
> **联系邮箱必须由用户提供**；若未给出，**不要建单**，先停下追问。**联系人姓名默认从邮箱 @ 前缀推导**，不必单独追问（用户显式给了姓名则用用户的）。
> 联系方式固定为 email、时区中国、地区中国、语言中文（见步骤 5 模板）。

## 整体流程（对齐 Portal 开单向导）

1. 收集问题（由用户填写，对应 Portal「问题描述」）
2. 选择 Service + Problem Classification（对应「问题类型」）
3. 组装工单详情（对应「详细信息」）
4. 确认严重性 + **停下等用户确认**（对应严重性选择 + 「查看 + 创建」前的确认）
5. 用户确认后 → 调 `run_az` 执行 `support in-subscription tickets create` 创建（对应点击「创建」）

---

## 步骤 1：收集问题（由用户填写）

本步信息**一律由用户提供**，不要从对话历史自动提炼/脑补；缺什么就停下追问用户。需要收集：

- **受影响资源**：用户给主机名即可。**资源组默认 `xiaomi-azure`**（用户未特别指定时），不要追问资源组。
  拿到主机名后用 `run_az` 执行 `vm show` 自动拼出完整 Resource ID：
  `args = ["vm","show","-g","xiaomi-azure","-n","<主机名>","--query","id","-o","json"]`（用户给了其他资源组就用其他的）。
- **联系邮箱**（必填）：用于接收工单回复的邮箱。**必须由用户提供**；没有时停下追问，不要编造也不要用占位邮箱。用户给的完整邮箱（如 `sanzhao@microsoft.com`）原样使用，不要截断。
- **联系人姓名**：默认**从邮箱 @ 前缀推导**（如 `sanzhao@...` → 姓名用 `sanzhao`），无需单独追问；若用户显式给了姓/名则用用户的。
- **问题时间**：用户给北京时间即可，**你自己减 8 小时换算为 UTC**（格式 `YYYY-MM-DD HH:MM UTC`），不要就转换结果再向用户确认。
- **现象**：用户描述的症状（如 CPU 持续 100%、磁盘延迟 >200ms、网络连接数触顶、Resource Health Unavailable 等）。
- **已完成的排查**（可选）：用户已做过的排查与结论；用户未提供则填「无」。

> 信息不足以提单时（缺主机名/资源、问题描述或联系邮箱），不要硬提单，先一次性请用户补齐缺的项（资源组、姓名、时间都有默认/可推导，不要为这些追问）。

## 步骤 2：选择 Service + Problem Classification

优先用下方「Problem Classification 速查表」按症状匹配。**匹配不到时必须动态查询，绝不要猜 ID。**

动态查询（用 `run_az`）：

```
# Step 1: 搜索 Service（把关键词填进 contains，如 'Virtual Machine'）
args = ["support","services","list","--query","[?contains(displayName,'Virtual Machine')].{name:name, displayName:displayName}","-o","json"]

# Step 2: 列出该 Service 下所有 Problem Classification（service-name 用上一步拿到的 name）
args = ["support","services","problem-classifications","list","--service-name","<service-name>","-o","json"]
```

- `problem_classification` 最终要用**完整 ARM ID**，形如
  `/providers/Microsoft.Support/services/<serviceId>/problemClassifications/<pcId>`。
- 查到新分类后，请在回复里把「Service / Problem Classification 名称 + ID」一并告诉用户，便于其核对，也方便后续维护本速查表。

### Problem Classification 速查表（VM 常见项）

> Service：Virtual Machine running ... / Virtual Machine（Linux 与 Windows 分属不同 service）。
> 下列为常见症状到分类的映射；**ID 必须以动态查询结果为准**，本表只用于快速定位「该查哪个 Service / 哪类问题」。
> 若表中无对应症状，走上面的动态查询。

| 症状 | Service（搜索关键词） | 问题分类（Problem Type） |
| --- | --- | --- |
| VM 无法启动 / 启动失败 | Virtual Machine running ... | Cannot start or stop my VM |
| VM 性能差 / CPU / 内存 / 卡顿 | Virtual Machine running ... | Performance（CPU / Memory / 等） |
| 磁盘性能 / IOPS / 吞吐 / 延迟 | Virtual Machine running ... | Disk performance / Storage |
| 网络连接 / 带宽 / 连不上 | Virtual Machine running ... | Network connectivity / Cannot connect |
| VM 意外重启 / 不可用 / Resource Health 异常 | Virtual Machine running ... | Availability / Unexpected reboot |
| 计划维护相关 | Virtual Machine running ... | Scheduled events / Maintenance |

> 实际 service `name` / `displayName` 与 problemClassification `id` 因区域/订阅可能不同，
> **首次提单某类问题时，请先用动态查询确认确切的 name 与完整 ID**，不要凭表中文字直接当 ID 用。

## 步骤 3：组装工单详情

按下面模板组装工单的 `title`（摘要）和 `description`（正文）。

`title`（一行摘要，简明扼要）：
```
[VM性能/磁盘/网络/可用性] <资源短名> <核心症状>，请协助排查
```

`description`（正文，照此结构）：
```
== 问题描述 ==
- 受影响资源: <完整 Resource ID>
- 问题时间: <YYYY-MM-DD HH:MM UTC> - <YYYY-MM-DD HH:MM UTC>
- 持续时间: <X 分钟/小时>

== 现象 ==
<描述观察到的症状>

== 请求 ==
<明确需要 MS Support 做什么>
```

- 所有时间一律 UTC；Resource ID 必须完整。
- 各字段一律用**步骤 1 中用户提供的内容**填写，用户没给的项写「无」或「N/A」，不要编造或脑补诊断结论。

## 步骤 4：确认严重性，并停下等用户确认

向用户展示组装好的工单草稿（title + description 全文），并附严重性对照表，请其确认。

### 严重性对照表

| 严重性 | 适用场景 | 响应时间 | severity 值 |
| --- | --- | --- | --- |
| A（Critical） | 影响生产、业务中断 | 7×24（含周末/节假日） | `critical` |
| B（Moderate） | 中等影响（默认） | 仅工作时间（周一至周五） | `moderate` |
| C（Minimal） | 问题咨询、影响轻微 | 仅工作时间，SLA 较长 | `minimal` |

- **默认严重性 = B（moderate）**；用户要 A/C 时按其指定。
- 响应区别：只有 A（critical）提供 7×24 响应（包括周末）；B/C 仅在工作时间（周一至周五）响应。若问题发生在周末且紧急、需要即时响应，建议选 A。
- 展示草稿后，本轮回复**到此为止**，明确告诉用户：「请确认是否创建该工单，并指定严重性（默认 B）。回复『确认创建』即提交。」若还未拿到联系邮箱，同时追问邮箱。
- **本轮绝不调用 `run_az` 执行建单命令。**

## 步骤 5：用户确认后创建工单

仅当用户在**新一轮消息**里明确表示确认（如「确认创建」「提交吧」「就按 B 提」「OK 创建」）后，
才调 `run_az` 执行建单命令：

```
args = [
  "support", "in-subscription", "tickets", "create",
  "--ticket-name", "<生成一个随机 UUID，如 a1b2c3d4-e5f6-7890-abcd-ef1234567890>",
  "--title", "<步骤 3 的摘要>",
  "--description", "<步骤 3 的正文全文>",
  "--problem-classification", "<步骤 2 得到的完整 Problem Classification ARM ID>",
  "--severity", "moderate",
  "--technical-resource", "<受影响资源的完整 Resource ID>",
  "--contact-first-name", "<联系人名，用户未明给时用邮箱 @ 前缀>",
  "--contact-last-name", "<联系人姓，用户未明给时用邮箱 @ 前缀>",
  "--contact-email", "<用户提供的完整联系邮箱>",
  "--contact-method", "email",
  "--contact-country", "CHN",
  "--contact-language", "zh-hans",
  "--contact-timezone", "China Standard Time",
  "--contact-additional-emails", "<cc邮箱1>", "<cc邮箱2>",
  "--advanced-diagnostic-consent", "Yes",
  "-o", "json"
]
```

- `--ticket-name`：工单的 ARM 资源名，须唯一。生成一个随机 UUID 填入即可（不是给人看的标题）。
- `--severity`：`critical` / `moderate` / `minimal`（默认 `moderate`）。
- `--problem-classification`：必须是步骤 2 的**完整 ARM ID**，不是名称。
- `--technical-resource`：受影响资源的完整 Resource ID（强烈建议带上）。
- `--contact-email`：**必须填用户提供的完整邮箱**（原样，不截断）；未拿到邮箱则不得执行本命令，先追问。
- `--contact-first-name` / `--contact-last-name`：用户明给姓名则用用户的；未给时**用邮箱 @ 前缀**填入两者（不必追问姓名）。
- `--contact-additional-emails`：**CC 抄送邮箱**（参数名就是 `--contact-additional-emails`，别写反）。用户要求 cc 某人时填入；**多个邮箱要作为多个独立数组元素**（每个邮箱一个元素，不要拼成一个字符串），如：`"--contact-additional-emails", "a@x.com", "b@y.com"`；用户没要求 cc 就**整个参数不要写**。
- 联系方式固定：method=email、country=CHN（中国）、language=zh-hans（中文）、timezone=China Standard Time（中国）、consent=Yes。
- **不要**写 `--subscription`：由底层自动注入。

> 仅 `--subscription` 由底层自动注入，其余参数均由你提供（联系邮箱必须用户提供；姓名未明给时用邮箱 @ 前缀）。

### 创建后的回复格式（固定）

命令返回成功后，从返回 JSON 里取字段，**严格按下面格式**回给用户（取不到的字段省略该行，不要编造）：

```
工单已创建成功。
- 工单编号：<supportTicketId>
- Ticket name：<name，即创建时的 UUID>
- 状态：<status，如 Open>
- 严重性：<severity，如 Minimal / Moderate / Critical>
- Service：<serviceDisplayName，如 Virtual Machine running Linux>
- Problem Classification：<problemClassificationDisplayName，如 Planned Maintenance (Azure Platform) / Questions on ...>
```

> 字段对应：工单编号取返回里的 `supportTicketId`；Ticket name 取 `name`；状态取 `status`；严重性取 `severity`；Service 取 `serviceDisplayName`；Problem Classification 取 `problemClassificationDisplayName`。

失败时**严格按下面格式**回复，把真实错误信息填进去，并据错误提示可能原因（如订阅无支持计划、缺少 Support Request Contributor 权限、support 扩展未安装），**不要谎报成功**：

```
工单创建失败。
- 失败原因：<run_az 返回的错误信息原文>
```

---

## 注意事项

- **创建工单必经用户明确确认**，且确认与创建必须分属两轮；禁止自动直接创建。
- **联系邮箱必须由用户提供**，缺邮箱时不得建单，先追问；不要编造、不要用占位邮箱。邮箱原样使用不截断。联系人姓名默认从邮箱 @ 前缀推导，不必追问。
- 资源组默认 `xiaomi-azure`；用户只给主机名即可自动拼 Resource ID，不要追问资源组。
- 问题时间用户给北京时间即可，你自动 −8h 转 UTC，不要就转换结果再确认。
- Resource ID 必须完整；所有时间用 UTC。
- 匹配不到 Problem Classification 时**动态查询**，不要猜 ID。
- 不要伪造问题内容；各字段只填用户在步骤 1 提供的内容，没给的写「无」/「N/A」。
- 创建结果一律用步骤 5「创建后的回复格式」回复：成功列工单编号/Ticket name/状态/严重性/Service/Problem Classification；失败只回「工单创建失败。- 失败原因：<原文>」，不谎报成功。
- 本技能只创建技术支持工单，不做计费/账户类工单。
