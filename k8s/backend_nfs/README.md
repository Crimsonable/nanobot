# OpenCode NFS 双节点部署（opencode-sv-formal）

本目录是 **opencode-sv-formal** 命名空间的实际部署清单：一台 CU 同时承载 control（8001）与 channel gateway（8000），存储放在 166 本地盘并同时导出给 168，因此 Pod 可以调度到任意一台节点。

与 [k8s/opencode](../opencode/README.md) 的区别：那边用 local/NFS 两套 PV 二选一，state 固定在单节点本地；这边只有一套 **无 nodeAffinity 的 hostPath PV**，靠两台机器挂载同一个 NFS 导出实现多节点可调度。

## 拓扑

| 项 | 值 |
| --- | --- |
| NFS server | 192.168.199.166（`zcaj-166`），导出 `/data/mnt/opencode-sv-formal` |
| NFS client | 192.168.199.168（`anquanzhouqi-168`），挂到同名路径 |
| 命名空间 | `opencode-sv-formal` |
| 节点标签 | `opencode-sv-formal=true`（166、168 都打） |
| Gateway NodePort | **30080**（承接原 nanobot 入口） |
| control Service | ClusterIP 8000 → `control-http`（仅集群内） |
| 镜像 | `agent-container-up:opencode-1.18.31`、`agent-bucket-runtime:opencode-1.18.31-bwrap011` |

## 本目录清单

| 文件 | 用途 |
| --- | --- |
| storage.yaml | namespace、4 个 hostPath PV、4 个 PVC（volumeName 静态绑定） |
| backend.yaml | ServiceAccount/Role/RoleBinding、CU 单副本 Deployment、两个 Service |
| frontends.json.sample | frontends.json 结构样板，**凭证字段已替换为 REPLACE_WITH_*** |

bucket 的 `opencode-bucket-<hash>` Deployment 由 CU 运行时按需创建，不在此清单内。

## 1. 前置条件

两台节点都需要 `nfs-utils`，并且 166 兼作 server：

```bash
# 166
apt-get install -y nfs-kernel-server
mkdir -p /data/mnt/opencode-sv-formal/{source,common,data,state}
# /etc/exports
/data/mnt/opencode-sv-formal 192.168.199.168(rw,sync,no_subtree_check,no_root_squash)
exportfs -rav && systemctl enable --now nfs-server

# 168
apt-get install -y nfs-common
mkdir -p /data/mnt/opencode-sv-formal
# /etc/fstab
192.168.199.166:/data/mnt/opencode-sv-formal /data/mnt/opencode-sv-formal nfs4 defaults,_netdev,hard,timeo=600,retrans=2,actimeo=1 0 0
mount -a && findmnt /data/mnt/opencode-sv-formal
```

`no_root_squash` 是必须的：CU 与 bucket 以 root 运行，hostPath PV 不改变文件属主。`source/`、`common/` 属于 UID/GID 10001，`data/`、`state/` 属于 root。

镜像必须在 **两台节点** 上都存在，否则 Pod 一旦被调度到没导入镜像的节点就会 `ErrImageNeverPull`：

```bash
ctr -n k8s.io images import cu.tar
ctr -n k8s.io images import bucket-bwrap011.tar
```

节点标签：

```bash
kubectl label node zcaj-166 anquanzhouqi-168 opencode-sv-formal=true
```

## 2. 目录结构

```
/data/mnt/opencode-sv-formal/
├── source/   源码与插件（RO，10001:10001）        → /mnt/source
├── common/   frontends.json + <fe>/{app,config}（RO，10001:10001） → /mnt/common
├── opencode/ opencode 1.18.31 程序目录（10001:10001），bucket 以 hostPath 方式引用
├── data/     workspaces/<fe>/（RWX，root）        → /mnt/env
└── state/    control/gateway SQLite 与 bucket runtime（RWX，root） → /data
```

`common/<fe>/` 下固定三件套：`app/skills`、`app/templates/AGENTS.md`、`config/{opencode-server.json,user-permissions.yaml,session-logging.json}`；frontend 名必须与 `common/frontends.json` 的 `frontend_id` 一致。

## 3. 部署

```bash
kubectl apply -f k8s/backend_nfs/storage.yaml
kubectl apply -f k8s/backend_nfs/backend.yaml
kubectl -n opencode-sv-formal rollout status deploy/container-up
```

`frontends.json` 不通过 ConfigMap 下发，直接放在 NFS 上：把 `frontends.json.sample` 复制成 `/data/mnt/opencode-sv-formal/common/frontends.json` 并填回真实凭证，然后 `kubectl -n opencode-sv-formal rollout restart deploy/container-up`。

## 4. 验证

```bash
kubectl -n opencode-sv-formal get pod -o wide           # 看落在 166 还是 168
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.199.166:30080/health/ready
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.199.168:30080/health/ready
curl -s -X POST http://192.168.199.166:30080/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"frontend_id":"web-main","user_id":"deploy-check","content":"reply ok"}'
```

流式入口是 `web-stream` frontend 的 SSE 端点，事件类型见 [前端同步接口](../../docs/WEB_FRONTEND_SYNC_API.md)。

## 5. 注意事项

- **state 在 NFS 上**：`state/` 里是 SQLite（WAL）。同机访问等价本地盘，跨机访问走 NFS；一旦 168 上的 bucket 报 `disk I/O error` 或 ReadTimeout，退路是把 state 换成节点本地 PV 并把 bucket 通过 nodeSelector 固定到该节点。
- **PV 无 nodeAffinity 是刻意设计**，不要为了让 Pod 落在某台机器而加亲和性；需要固定时改 CU 的 `nodeSelector`。
- **凭证不要进仓库**：`common/frontends.json` 含 QXT `app_secret` 和 MinIO 密钥，只放 NFS，本目录只保留 `frontends.json.sample`。
- 更新源码走 [部署与运维手册](../../docs/DEPLOYMENT_MANUAL.md) 第 8 章；改完 `source/` 需要 `rollout restart`，并注意重建已有 bucket。
- 删除命名空间不会删除 PV 数据（`Retain`），但会留下 `Released` 状态的 PV，重装前需手工清理。