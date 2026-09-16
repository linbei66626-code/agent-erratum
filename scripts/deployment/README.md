# Letta 本地部署步骤（不安装包、不运行实验）

目标是已授权的 Ubuntu 22 服务器，不是 Mac；默认项目路径 `/root/rivermind-data/agent-erratum`。本助手只准备专用 DB、迁移、启动和停止 Letta，**不执行 GPU 推理、不安装依赖、不清理旧实验数据**。

先由部署负责人准备：

- `vendor/letta-v1`：固定 `56ba9c25552605eec89de8ed3dc6394b625c1993`，优先连 `.git` 一起保留。脚本检查四份关键文件 SHA；有 `.git` 时也核 HEAD 和 tracked 工作树。无 `.git` 的选择性 SHA 检查不代表全树证明。
- `.venv-letta`：以对应 `uv.lock` 安装 server/postgres（可带 redis client）extras；不与 MU/vLLM/Vita 共用环境。
- `pg_config` 指向目标 PostgreSQL，且对应版本的 `postgres/initdb/pg_ctl/psql/pg_isready`、pgvector extension 已安装，`postgres` 系统用户存在。包安装不在本脚本内。
- 当前登录用户为服务器 root；旧数据库、端口和磁盘由主执行者先盘点。不运行上游 `init.sql` 或 `startup.sh`。
- 源码目录没有 `.env`。脚本使用环境白名单，不继承 cloud keys、代理、默认 embedding、OTEL/ClickHouse、Redis host 等。

## 首次部署

```bash
cd /root/rivermind-data/agent-erratum
python scripts/deployment/letta_local.py prepare-db
python scripts/deployment/letta_local.py migrate
python scripts/deployment/letta_local.py start --model-url http://127.0.0.1:8000/v1
python scripts/deployment/letta_local.py status
```

每步看到 `success: true` 才执行下一步。`start` 仅验证版本为 0.16.8 的 Letta liveness；模型列表、创建 Agent 和工具回路仍由另一个探针验证，不声称服务全链条已成功。

`prepare-db` **始终创建专用 cluster** `/var/lib/postgresql/ae01_letta` 和 socket 目录 `/run/postgresql/ae01_letta`。检测到任意既有 cluster/Postgres 进程或 5432 占用时选独立 5543；否则选 5432。目标端口占用、目标目录/凭据存在即拒绝，不接管旧库、不删除文件。新角色与数据库均为 `ae01_letta`；角色没有 superuser/createdb/createrole 权限；管理员只为新库创建 vector extension。

随机 DB 密码仅保存于 `deployment/private/letta-db.json`（0600，父目录0700），不进入命令行/公开日志。迁移 stdout/stderr 在内存脱敏后再写记录。后台进程原始日志可能含敏感错误，因此全部为 `.private.log`、0600；**只用 status 提供的脱敏尾部对外分享，不打包 private 目录或原始 private 日志。**

每次调用新建 `deployment/runs/<UTC时间-随机后缀>/`，保存脚本/源 SHA、执行步骤、结果与日志；不覆盖旧记录。DB 数据在系统 Postgres 专用目录，所有本脚本捕获的进程日志在上述 deployment 记录目录。

## 停止和再次启动

```bash
python scripts/deployment/letta_local.py stop
```

只停止本脚本记录、经 PID/命令核验的 Letta 进程和本实验专用 cluster；保留 DB/角色/凭据、源码、venv 和记录，不使用强制 kill。停止不等于平台关机，计费实例仍须在平台结束。

重新开机或主动停止后，不再执行 `prepare-db`，而执行：

```bash
python scripts/deployment/letta_local.py start-db
python scripts/deployment/letta_local.py start --model-url http://127.0.0.1:8000/v1
```

如果上次异常退出留下 `letta-process.json`，先运行 `status`、再 `stop` 做经核验的停机/记录归档，然后重启；不要手工覆盖 PID 或凭据。DB 初始化部分失败时同样停下检查：凭据和现场特意保留，脚本不会猜测怎样重建，更不会自动删除部分数据。

## 边界与源码依据

- 不配置 Redis host 时 Letta 使用 Noop Redis，适用于这一次串行 smoke，不支持声称跨进程锁/并发安全：`letta/data_sources/redis_client.py:663–680`。
- PostgreSQL + vector + Alembic + server 顺序：固定源码 `CONTRIBUTING.md:28–85`。
- Alembic 会打印数据库 URI：`alembic/env.py:23–24`，所以不直接 tee 原始输出。
- 本地 provider：`letta/server/server.py:280–295`、`letta/schemas/providers/vllm.py:18–59`。
- 禁止从 `enable_reasoner` 猜 Qwen thinking 状态；服务侧设置及实际模型请求另核。
- 命令的本地语法/单元检查不是 Ubuntu 实际部署成功证据。
