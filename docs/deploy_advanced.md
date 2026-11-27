# 生产运维说明

首次安装见 [部署手册](../DEPLOY.md)。本文补充长期运行、网络边界、备份恢复和升级。

## 一、Windows 常驻

`start.bat` 和 `restart_services.bat` 适合手动启停，不是 Windows 服务。无人值守时，使用企业批准的服务管理工具（例如由 IT 配置 WinSW）分别托管实际进程，不要托管会启动子进程后退出的批处理。使用固定、非管理员服务账号，给它项目读取权限、`knowledge` 和实际数据目录所需的写权限，并配置每个服务各自的日志、开机启动和失败重启。模型应安装在运行 Ollama 的账号可访问的位置。

| 服务 | 托管的进程与参数 |
|---|---|
| Qdrant | `qdrant.exe`；工作目录保持为 exe 所在目录，设置 `QDRANT__SERVICE__HOST=127.0.0.1` 与绝对路径 `QDRANT__STORAGE__STORAGE_PATH`；沿用已有数据目录，不隐式迁移 |
| Ollama | `ollama.exe serve`；设置 `OLLAMA_HOST=127.0.0.1:11434`，不要依赖桌面应用登录后启动 |
| 管理后台 | 项目根目录下 `.venv\Scripts\python.exe -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700` |
| 前台 | 项目根目录下 `.venv\Scripts\python.exe -m streamlit run app.py --server.headless true --server.port 8501` |
| MCP（可选） | 项目根目录下 `.venv\Scripts\python.exe -m src.mcp_server`，设置 `MCP_TRANSPORT=streamable-http` |

通过服务管理器统一启停，不混用后台的启停按钮或 `.bat` 脚本。前台和后台各保留一个实例。Qdrant、Ollama 就绪后再验收应用，不把「进程已启动」当作就绪。

## 二、Linux 常驻

以下为 systemd 单元的关键配置。`User` 换成实际运行用户；`WorkingDirectory` 和绝对路径都改为实际安装位置。各服务只启动一个实例。Ollama 如已由官方安装器托管，则沿用其单元并检查只绑定回环。

```ini
# sky-rag-qdrant.service
[Unit]
Description=Sky RAG - Qdrant
After=network.target
[Service]
User=rag
WorkingDirectory=/opt/sky-rag
Environment=QDRANT__STORAGE__STORAGE_PATH=/opt/sky-rag/storage/qdrant_data
Environment=QDRANT__SERVICE__HOST=127.0.0.1
ExecStart=/opt/sky-rag/tools/qdrant/qdrant
Restart=on-failure
[Install]
WantedBy=multi-user.target
```

其他应用单元沿用 `User`、`WorkingDirectory`、`Restart` 和 `[Install]`，`After` 加上实际运行的 Qdrant/Ollama 单元。根据服务设置 `ExecStart`：

- Admin：`/opt/sky-rag/.venv/bin/python -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700`
- Web：`/opt/sky-rag/.venv/bin/python -m streamlit run app.py --server.headless true --server.port 8501`
- MCP（可选）：`/opt/sky-rag/.venv/bin/python -m src.mcp_server`，另加 `Environment=MCP_TRANSPORT=streamable-http`

执行 `systemctl daemon-reload` 后，用 `systemctl enable --now` 启用实际需要的单元。使用 systemd 时不要再混用后台脚本启停。

## 三、网络与 HTTPS

默认使用公司内网或 VPN。6333、6334 和 11434 不向客户端开放；8501、8700、8760 按 [部署手册](../DEPLOY.md) 的范围限制。对外不需要 MCP 时不要启用或放行。PAT 不能替代传输加密，跨信任网络接入时使用经审核的 HTTPS 或受控隧道。

HTTPS 反代不是只转发 `/admin` 就能用：

- 前台的 Streamlit 流式响应依赖 WebSocket，反代需支持 Upgrade 和合理的读超时。
- 管理后台同时依赖 `/admin`、管理 API 与静态资源。员工浏览器的认证请求还会按当前 `AUTH_ADMIN_PORT` 构造地址；代理拓扑必须实测登录后刷新、退出、同源限制、Cookie 和客户端 IP。不能靠挡住 `/admin` 就认为管理接口和认证链路都已正确隔离。
- 只信任实际代理地址传入的 `X-Forwarded-*`；后端端口不要绕过代理直接开放。公网开放前应由 IT 验证路由、证书和 Cookie 安全属性，不复用未验收的单路由配置。
- 正式环境按公司要求限制站点可达范围并管理账号。限制登录页不等于关闭注册接口；部署后分别验证注册策略和普通员工权限。

## 四、备份与恢复

| 内容 | 默认位置或取法 |
|---|---|
| 账号、权限、日志、令牌数据 | `storage/auth/auth.db`；后台「数据管理 → 导出整库」可生成在线 SQLite 快照 |
| 签名密钥 | `storage/auth/admin_secret.key`（如已生成）；与 `.env` 分开加密保管，限制操作权限 |
| 知识文件与索引状态 | `knowledge/`、`storage/index_state.json`、`storage/domains.json`（如有）；保留原相对路径及文件名 |
| 向量库 | Linux 通常在 `storage/qdrant_data/`；Windows 看 `storage/svc_qdrant_control.log` 中的实际路径。使用匹配版本的 Qdrant 快照，或停服后复制整个实际数据目录，不热拷数据目录 |
| 站点设置 / 历史会话 | 根目录 `settings.json`（如有）；需保留历史时另备 `storage/sessions/`（如有） |

**整套备份**：在同一维护窗口暂停上传、重建、文档移动和权限修改。停止应用写入后，备份知识文件、台账、配置和向量库；Windows 无人值守可由公司备份系统或计划任务执行这套流程。若应用仍运行，不要直接复制可能处于 WAL 模式的 SQLite `.db`，用后台导出或 SQLite 在线备份；在线 SQLite 快照只覆盖数据库本身。备份目标应不在项目目录，核对文件与向量快照都已生成，定期用独立目录和临时实例练习恢复。

**恢复**：先保留当前数据，再在停服状态下把同批备份恢复到隔离位置。按原配置启动匹配版本的 Qdrant 和相同的 embedding 模型，核对集合、向量维度、文件与卡片数，并抽查受限资料。需保持原会话签名时从受控备份恢复原密钥；否则按公司策略轮换，安排用户重新登录。不要直接覆盖唯一的库，也不要把清库或全量重建当成恢复校验。跨系统迁移注意文件名大小写、Windows 保留名及路径长度。

## 五、升级与回滚

1. 记录当前部署提交、依赖与 Qdrant 版本，完成一次成套备份。选择明确的发布提交，在隔离环境先安装依赖并验证，再安排维护窗口。
2. 切换到验收提交，保留原 `.env` 和数据路径。只用一种进程管理方式重启实际启用的服务；若 Windows 刚升级到回环绑定规则，应确认新的 Qdrant 进程实际读取原库，并检查 HTTP 与 gRPC 的监听。
3. 验收员工登录和问答、最小权限、管理员功能和可选 MCP。从另一台内网机器确认向量库及 Ollama 端口不可达。异常时回到原应用版本；若升级期间写入了不兼容的数据，在停服后回到同批备份，不混用不同时间点的数据。
