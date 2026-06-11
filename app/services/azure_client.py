"""Azure 管理面客户端工厂。

本项目只使用 **只读** API：
  - ComputeManagementClient ：VM / Disk 描述
  - MonitorManagementClient ：metrics + metric_definitions
  - ResourceHealthClient    ：availability_statuses
  - NetworkManagementClient ：NIC 上 `enable_accelerated_networking` 判定

生产环境推荐 SPN 模式 + 只读角色（Reader + Monitoring Reader + Resource Health Reader）。
开发/本地调试可用 cli 模式。
"""
from __future__ import annotations

import logging
from functools import lru_cache

from azure.identity import AzureCliCredential, ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.network import NetworkManagementClient

logger = logging.getLogger(__name__)

try:
    from azure.mgmt.resourcehealth import MicrosoftResourceHealth as ResourceHealthClient  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from azure.mgmt.resourcehealth import ResourceHealthMgmtClient as ResourceHealthClient  # type: ignore
    except ImportError:
        from azure.mgmt.resourcehealth import ResourceHealthManagementClient as ResourceHealthClient  # type: ignore

from app.config import get_settings


@lru_cache(maxsize=1)
def get_credential():
    """只支持两种鉴权：spn（生产）/ cli（本地调试）。"""
    s = get_settings()
    mode = (s.azure_auth_mode or "").strip().lower()

    if mode in {"spn", "client_secret", "service_principal"}:
        if not (s.azure_tenant_id and s.azure_client_id and s.azure_client_secret):
            raise RuntimeError(
                "AZURE_AUTH_MODE=spn 时必须配置 AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET"
            )
        logger.info("Azure 凭证模式: spn")
        return ClientSecretCredential(
            tenant_id=s.azure_tenant_id,
            client_id=s.azure_client_id,
            client_secret=s.azure_client_secret,
        )

    if mode in {"cli", "azure_cli"}:
        logger.info("Azure 凭证模式: cli")
        return AzureCliCredential()

    raise RuntimeError(
        f"不支持的 AZURE_AUTH_MODE={mode!r}，仅允许 'spn' 或 'cli'"
    )


@lru_cache(maxsize=1)
def get_compute_client() -> ComputeManagementClient:
    s = get_settings()
    return ComputeManagementClient(get_credential(), s.azure_subscription_id)


@lru_cache(maxsize=1)
def get_monitor_client() -> MonitorManagementClient:
    s = get_settings()
    return MonitorManagementClient(get_credential(), s.azure_subscription_id)


@lru_cache(maxsize=1)
def get_resource_health_client():
    s = get_settings()
    return ResourceHealthClient(get_credential(), s.azure_subscription_id)


@lru_cache(maxsize=1)
def get_network_client() -> NetworkManagementClient:
    s = get_settings()
    return NetworkManagementClient(get_credential(), s.azure_subscription_id)
