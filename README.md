# Azure VM 诊断 Agent

通过飞书机器人对话，对 Azure 虚拟机做**只读**健康诊断。用户用中文提问，Agent 自动路由到对应技能，按技能内置的 `az` 命令实时采集 Azure Monitor / Resource Health 指标，组装成中文诊断报告返回。

## 一键部署到 Azure VM

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Ftech-connection%2Fazure-ops-agent%2Fmain%2Fdeploy%2Fazuredeploy.json)

点击按钮将跳转到 Azure Portal；如果尚未登录，Azure 会先要求登录。模板复用已有子网及其网络安全组，并创建公网 IP、网卡和 Ubuntu VM。部署过程中会自动安装 Azure CLI、Python 依赖和 Agent，并配置 systemd 开机自启。

部署完成后，从部署输出获取公网 IP，登录 VM：

```bash
ssh azureagent@<公网IP>
```

在 VM 内使用获得目标订阅读取权限的用户完成 Azure CLI 登录，然后重启服务：

```bash
az login --use-device-code --tenant <租户ID>
az account set --subscription <目标订阅ID>
sudo systemctl restart azure-vm-diagnosis-agent
```

模板使用 `AZURE_AUTH_MODE=cli`，不创建或接收 Service Principal。Azure CLI 登录缓存保存在 `azureagent` 用户的 `~/.azure` 中，访问令牌到期后会自动刷新；如果目标租户撤销会话或要求重新认证，需要再次运行上述登录命令。

## 特性

- **纯只读**：只查询 Azure 控制面指标与健康事件，不修改任何资源、不进入 VM 操作系统内部。
- **模型即编排**：基于 Microsoft Agent Framework，LLM 读取 `SKILL.md` 指令后，通过 `run_az` 工具逐条执行只读 az 命令。
- **多维诊断**：CPU、内存、磁盘、网络、VM 运行状况、订阅级服务故障，支持一次性整体体检。
- **飞书长连接**：无需公网回调地址，启动即连。

## 技能（Skills）

| 技能 | 用途 |
| --- | --- |
| `vm-full-diagnosis` | 一次性体检 CPU/内存/磁盘/网络/运行状况五维 |
| `vm-cpu-check` | CPU 利用率诊断 |
| `vm-memory-check` | 内存使用率诊断 |
| `vm-disk-check` | 逐盘 IOPS / 吞吐 / 延迟与 SKU 上限对比 |
| `vm-network-check` | 带宽 / 连接数 / 加速网卡诊断 |
| `vm-resource-health-check` | 某台 VM 的 Resource Health 事件 |
| `service-health-check` | 订阅级 Azure 平台故障 / 服务运行状况 |
| `azure-qa` | Azure 通识问答与能力边界兜底 |

## 环境要求

- Python 3.10+
- Azure CLI（已 `az login`，用于只读查询）
- 一个 Azure OpenAI 部署
- 一个飞书自建应用（开通机器人能力）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 配置

在项目根目录创建 `.env`：

```ini
# Azure
AZURE_SUBSCRIPTION_ID=<订阅 ID>
AZURE_AUTH_MODE=cli                 # cli | sp（服务主体时需补 tenant/client 三项）
AZURE_DEFAULT_RESOURCE_GROUP=xiaomi-azure

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://<your>.openai.azure.com
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_DEPLOYMENT=<部署名>
AZURE_OPENAI_API_VERSION=preview

# 飞书
FEISHU_APP_ID=<app id>
FEISHU_APP_SECRET=<app secret>

# 可选
APP_LOG_LEVEL=INFO
SESSION_IDLE_TTL_SEC=1200           # 会话闲置过期秒数
SESSION_MAX_TURNS=6                 # 单会话保留的最大轮次
```

## 运行

```bash
# 生产：飞书长连接
python -m app.main

# 仅健康检查 HTTP（可选，用于监控）
uvicorn app.main:app --host 0.0.0.0 --port 8080
# GET /health
```

## 使用示例（飞书内对话）

- 「诊断下主机 vm-xxx」 → 整体体检
- 「vm-xxx 的 CPU 高不高」 → CPU 诊断
- 「看下内存」「磁盘 IO 有没有问题」 → 延续上一台主机
- 「今天有没有 AOAI 相关故障」 → 订阅级服务故障查询

## 测试

```bash
pytest
```

## 目录结构

```
app/
  main.py              # 入口（飞书长连接 / 健康检查）
  config.py            # 配置（.env）
  feishu_longconn.py   # 飞书长连接客户端
  agent/vm_agent.py    # 主 Agent：路由 + 会话管理
  skills/              # 各诊断技能（SKILL.md + 注册）
  tools/               # run_az 等只读工具
  services/            # Azure / 飞书客户端
tests/                 # 单元测试
```

> 本服务仅执行只读 az 命令，不会对 Azure 资源做任何变更。
