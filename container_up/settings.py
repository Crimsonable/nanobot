from __future__ import annotations

import os
from pathlib import Path


# container_up 服务自身监听地址
APP_HOST = os.getenv("CONTAINER_UP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("CONTAINER_UP_PORT", "8080"))

# SQLite 数据库文件路径
DB_PATH = Path(
    os.getenv("CONTAINER_UP_DB_PATH", "/var/lib/container_up/container_up.db")
)

# 宿主机上的 workspace 根目录
HOST_WORKSPACE_ROOT = Path(os.getenv("HOST_WORKSPACE_ROOT", "/opt/nanobot/workspaces"))
# 宿主机上的共享配置文件路径
HOST_SHARED_CONFIG = Path(
    os.getenv("HOST_SHARED_CONFIG", "/opt/nanobot/shared/config.json")
)
# 宿主机上的共享 skills 目录
HOST_SHARED_SKILLS = Path(os.getenv("HOST_SHARED_SKILLS", "/opt/nanobot/shared/skills"))

# 子容器使用的镜像名
CHILD_IMAGE = os.getenv("CHILD_IMAGE", "nanobot-bridge:latest")
# 子容器加入的 Docker 网络名
CHILD_NETWORK = os.getenv("CHILD_NETWORK", "nanobot-stack")
# 子容器的网络模式；为空时使用普通 network 连接
CHILD_NETWORK_MODE = os.getenv("CHILD_NETWORK_MODE", "").strip()
# 宿主机 workspace 挂载到子容器内的目标目录
CHILD_WORKSPACE_TARGET = os.getenv("CHILD_WORKSPACE_TARGET", "/app/nanobot_workspaces")
# 共享配置文件挂载到子容器内的目标路径
CHILD_SHARED_CONFIG_TARGET = os.getenv(
    "CHILD_SHARED_CONFIG_TARGET", "/app/nanobot_workspaces/config.json"
)
# 内置 skills 挂载到子容器内的目标目录
CHILD_BUILTIN_SKILLS_TARGET = os.getenv(
    "CHILD_BUILTIN_SKILLS_TARGET", "/app/nanobot/skills"
)
# parent 与 child 之间 bridge 连接使用的 token
CHILD_BRIDGE_TOKEN = os.getenv("CHILD_BRIDGE_TOKEN", "")
# 等待子容器 bridge ready 的超时时间，单位秒
CHILD_READY_TIMEOUT = int(os.getenv("CHILD_READY_TIMEOUT", "90"))
# 组织容器名称前缀
CHILD_CONTAINER_PREFIX = os.getenv("CHILD_CONTAINER_PREFIX", "nanobot-org")
# child 容器回连 parent bridge 的 websocket 地址
PARENT_BRIDGE_URL = os.getenv(
    "PARENT_BRIDGE_URL", f"ws://container-up:{APP_PORT}/ws/bridge"
)

# parent 转发请求到 child 的超时时间，单位秒
FORWARD_TIMEOUT = float(os.getenv("FORWARD_TIMEOUT", "300"))
# org 级别空闲回收阈值，单位秒
IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "3600"))
# 后台扫描空闲 org 容器的周期，单位秒
CLEANUP_SCAN_INTERVAL = int(os.getenv("CLEANUP_SCAN_INTERVAL", "300"))
# child 内用户实例空闲超时，单位秒
INSTANCE_IDLE_TIMEOUT_SECONDS = int(os.getenv("INSTANCE_IDLE_TIMEOUT_SECONDS", "1800"))
# 是否打印 LLM 请求日志
LOG_LLM_REQUESTS = os.getenv("NANOBOT_LOG_LLM_REQUESTS", "").strip()

# 订阅解密/鉴权使用的应用 ID
APP_ID = os.getenv("APP_ID", os.getenv("APPID", "")).strip()
# 订阅解密/鉴权使用的应用密钥
APP_SECRET = os.getenv(
    "APP_SECRET",
    os.getenv("APPSECRET", os.getenv("APPSECRECT", os.getenv("APPSCRECT", ""))),
).strip()
# 企业或组织 ID
CORP_ID = os.getenv("CORP_ID", os.getenv("CORPID", "")).strip()
# 订阅回调验签 token
CALLBACK_TOKEN = os.getenv("CALLBACK_TOKEN", os.getenv("TOKEN", "")).strip()
# 获取 access token 的接口地址
ACCESS_URL = os.getenv("ACCESS_URL", "").strip()
# 发送消息回调到远端的接口地址
SEND_MSG_URL = os.getenv("SEND_MSG_URL", "").strip()
# 发送消息回调请求超时时间，单位秒
SEND_MSG_TIMEOUT = float(os.getenv("SEND_MSG_TIMEOUT", "10"))
# 发送消息回调失败后的最大重试次数
SEND_MSG_RETRY_COUNT = int(os.getenv("SEND_MSG_RETRY_COUNT", "3"))
# 发送消息回调重试退避基数，单位秒
SEND_MSG_RETRY_BACKOFF = float(os.getenv("SEND_MSG_RETRY_BACKOFF", "1"))
