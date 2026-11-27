# 长城互联网 · 企业知识库部署手册

推荐在公司内网或 VPN 部署。开机常驻、HTTPS 接入、备份恢复和升级见 [生产运维说明](docs/deploy_advanced.md)。

## 一、端口与环境

| 服务 | 端口 | 放行范围 |
|---|---|---|
| 知识库前台 | **8501** | 使用知识库的员工网段 |
| 认证与管理后台 | **8700** | 直连部署时，员工浏览器需要访问 `/auth/*`；其他管理操作仍需管理员鉴权。请仅向所需内网网段放行 |
| MCP（可选） | 8760 | 指定的 Agent 客户端网段；需要访问令牌 |
| Qdrant | 6333（HTTP）、6334（gRPC，如启用） | **仅本机，不放行防火墙** |
| Ollama | 11434 | **仅本机，不放行防火墙** |

安装 **Python 3.12**、Git、Qdrant 和 Ollama。建议至少 8 GB 内存、20 GB 磁盘，另预留知识数据及备份空间。需要能访问所选 LLM 服务；无 GPU 可运行，但批量向量化较慢。企业应用控制策略如果阻止可执行文件或原生 DLL，请由 IT 核对允许清单，不要关闭策略绕过。

## 二、Windows Server（推荐流程）

在 PowerShell 中执行，把示例仓库地址替换为实际地址：

```powershell
git clone -b dev/v3.9 "<仓库地址>" sky-rag
Set-Location sky-rag
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

1. 将适配服务器架构的 Qdrant Windows 版解压至 `tools\qdrant\`，确保 `qdrant.exe` 在该目录。安装 Ollama，在部署所用的 Windows 账号下运行 `ollama pull qwen3-embedding:4b`。处理旧版 Office/WPS 文件时才需要 LibreOffice；安装在其他位置可设置 `LIBREOFFICE_DIR` 指向含 `soffice.exe` 的目录。
2. 在 `.env` 配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；其余参考示例。企业文档放进 `knowledge\`，不要提交到 Git。如选用 `local_onnx` 精排，先运行 `.\.venv\Scripts\python.exe scripts\download_rerank_model.py`。
3. 运行 `cmd /d /c start.bat`；使用 MCP 时运行 `cmd /d /c start.bat --with-mcp`。`start.bat` 按顺序探活，日志在 `storage\svc_*.log`，失败时检查对应的服务和控制日志。单独启停可用 `service_ctl.bat <服务> <start|stop>`，服务名为 `qdrant`、`ollama`、`admin`、`front` 或 `mcp`。需要全组重启时使用 `restart_services.bat`，不要把普通启动当成重启。
4. 新装默认向量数据路径为项目的 `storage\qdrant_data\`；如果所用 Qdrant 已有同目录的 `storage\`，默认保留该库。升级前以 `storage\svc_qdrant_control.log` 为准核对路径。自定义路径时在启动账号环境中设置 `QDRANT_EXE`、`QDRANT_STORAGE_PATH` 或 `OLLAMA_EXE`（使用绝对路径；这些是 Windows 启动脚本读取的环境变量，不写进 `.env`）。例如：

```powershell
$env:QDRANT_EXE = 'D:\RagTools\qdrant\qdrant.exe'
$env:QDRANT_STORAGE_PATH = 'D:\RagData\qdrant'
cmd /d /c start.bat
```

长期运行时，将路径配置到实际运行账号的环境，见 [生产运维说明](docs/deploy_advanced.md)。切换数据目录不会搬迁原库。

## 三、Linux

```bash
git clone -b dev/v3.9 "<仓库地址>" sky-rag && cd sky-rag
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# 按服务器 CPU 架构从官方发行页下载 Qdrant，解压到 tools/qdrant/qdrant
chmod +x tools/qdrant/qdrant
# 安装 Ollama 后拉取与现有向量库匹配的模型
ollama pull qwen3-embedding:4b
# 按需安装 LibreOffice 或下载本地精排模型，填写 .env 后启动
bash start.sh
```

## 四、首次初始化与验收

1. 打开 `http://<服务器IP>:8700/admin/login`，在首次初始化页面设置管理员，然后创建部门和员工账号。
2. 用员工账号登录 `http://<服务器IP>:8501`。上传一份无敏感内容的测试资料，确认问答有引用，再用非授权员工核对受限资料无法检索。登录后刷新并退出一次，确认认证链路可用。
3. 如启用 MCP，用后台签发的令牌在实际客户端查一次；无令牌的 `401` 只证明鉴权端点在线。
4. 在服务器上查 6333、6334、11434 的监听地址；再从另一台内网电脑确认这些端口不可达。Windows 可用 `Get-NetTCPConnection -State Listen -LocalPort 6333,6334,11434` 在服务器上核查。

## 五、数据与备份

- 知识源文件默认在 `knowledge/`。认证库是 `storage/auth/auth.db`，同目录还有会话签名密钥。后台「数据管理 → 导出整库」只备份 SQLite，不包含密钥或知识文件。
- 同时备份 `storage/index_state.json`、`storage/domains.json`（如有）、根目录 `settings.json`（如有）和实际的 Qdrant 数据目录。`.env` 和签名密钥单独加密保管。详见 [备份与恢复](docs/deploy_advanced.md#四备份与恢复)。
