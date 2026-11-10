import os, yaml, json, io

src = os.path.join(os.environ['TEMP'], 'ocdeploy', 'formal_deploy')
out = os.path.join(os.getcwd(), 'k8s', 'backend_nfs')
os.makedirs(out, exist_ok=True)

def dump(docs, header, path):
    chunks = []
    for d in docs:
        chunks.append(yaml.safe_dump(d, sort_keys=False, default_flow_style=False,
                                     allow_unicode=True, width=200))
    text = header.rstrip('\n') + '\n' + '---\n'.join(chunks)
    if not text.endswith('\n'):
        text += '\n'
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    print(path, len(text))

storage_header = '''# opencode-sv-formal：NFS 共享目录版存储清单。
#
# 设计要点：四个 PV 均为 hostPath，指向 166 本地盘上的
# /data/mnt/opencode-sv-formal/<子目录>；168 通过 /etc/fstab 把同一导出挂到同名路径，
# 因此 PV 不带 nodeAffinity —— Pod 调度到 166 是本地读写，调度到 168 则经 NFS 读写同一份数据。
#
# 前提：两台节点都必须已挂载 /data/mnt/opencode-sv-formal，且四个子目录真实存在。
# PVC 用 volumeName 静态绑定，storageClassName 必须为空，否则会被默认 StorageClass 抢绑。
'''

storage_docs = list(yaml.safe_load_all(io.open(os.path.join(src, 'storage.yaml'), encoding='utf-8')))
ns = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'opencode-sv-formal'}}
order = {('PersistentVolume', d['metadata']['name']): i for i, d in enumerate(storage_docs)}
dump([ns] + storage_docs, storage_header, os.path.join(out, 'storage.yaml'))

backend_header = '''# opencode-sv-formal：CU（control + gateway）及其 Service。
#
# control 容器持有 kubeconfig 权限，负责按需创建 opencode-bucket-<hash> Deployment；
# 这些 bucket Deployment 由 CU 运行时生成，不在此清单内。
# Gateway 对外暴露 NodePort 30080（承接原 nanobot 的入口端口）。
'''

backend_docs = list(yaml.safe_load_all(io.open(os.path.join(src, 'workload.yaml'), encoding='utf-8')))
for d in backend_docs:
    if d.get('kind') == 'Service' and d['metadata']['name'] == 'channel-gateway':
        d['spec']['ports'][0]['nodePort'] = 30080
    if d.get('kind') == 'Deployment':
        ann = d['spec']['template']['metadata'].get('annotations')
        if ann:
            ann.pop('kubectl.kubernetes.io/restartedAt', None)
            if not ann:
                d['spec']['template']['metadata'].pop('annotations')
dump(backend_docs, backend_header, os.path.join(out, 'backend.yaml'))

fe = json.load(io.open(os.path.join(src, 'frontends.json.sample'), encoding='utf-8'))
for f in fe['frontends']:
    ch = f.get('channel') or {}
    if ch.get('app_secret'):
        ch['app_secret'] = 'REPLACE_WITH_QXT_APP_SECRET'
    att = f.get('attachment_storage') or {}
    if att.get('access_key'):
        att['access_key'] = 'REPLACE_WITH_MINIO_ACCESS_KEY'
    if att.get('secret_key'):
        att['secret_key'] = 'REPLACE_WITH_MINIO_SECRET_KEY'
with io.open(os.path.join(out, 'frontends.json.sample'), 'w', encoding='utf-8', newline='\n') as f:
    json.dump(fe, f, ensure_ascii=False, indent=2)
    f.write('\n')
print('frontends.json.sample done')