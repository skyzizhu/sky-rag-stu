# 部署指南（服务器版）

把 Sky Personal RAG 从本地部署到服务器（Linux）的完整清单。照着做即可。

---

## 一、架构与数据位置

| 服务 | 端口 | 说明 |
|---|---|---|
| Streamlit 前台 | 8501 | 团队成员日常使用 |
| FastAPI 管理后台 | 8700 | 管理员使用 |
| MCP HTTP（可选） | 8760 | 团队 Agent 接入，PAT 鉴权 |
| Qdrant 向量库 | 6333 | **只绑本机**，不对网络开放 |
| Ollama | 11434 | 本地向量化模型，**只绑本机** |

**所有数据都在服务器的项目目录里**（用户浏览器上没有数据）：

| 数据 | 位置 | 说明 |
|---|---|---|
| 账号 / 权限 / 令牌 / 登录与问答日志 / 个人偏好 | `storage/auth.db`（SQLite） | |
| 向量知识卡片 | `storage/qdrant_data/` | |
| 知识源文件 | `knowledge/` | **隐私文件，不进 git，需单独拷贝** |
| 入库台账 / 签名密钥 / 领域注册表 | `storage/` 下若干文件 | |
| LLM API Key 等配置 | `.env` | 不进 git，需单独拷贝 |

不需要另装 MySQL / Postgres——SQLite（WAL 模式）+ 文件对这个团队规模足够，且零部署成本。

---

## 二、服务器要求

- Linux（x86_64 或 arm64），Python **3.12+**
- 磁盘：≥ 20GB（Ollama 模型约 2.5GB + 知识库与向量数据）
- 内存：≥ 8GB（本地跑 4B 向量化模型；有 GPU 更快）
- 网络：能出网访问 DeepSeek API（llm_base_url）与下载模型 / 依赖
- **不要求 GPU**：无 GPU 时检索可用，批量入库较慢；介意可换支持云端 embedding 的方案

---

## 三、迁移清单（从本地搬到服务器）

| 内容 | 方式 | 说明 |
|---|---|---|
| 代码 | `git clone`（或 rsync 项目目录） | 用 main 或 dev 分支均可 |
| `.env` | **手动拷贝**（在 git 里被忽略） | 含 LLM_API_KEY，必拷 |
| `knowledge/` | **手动拷贝**（已整体退出 git） | 知识源文件；不拷则是空知识库 |
| `storage/auth.db` | 可选 | 拷贝 = 现有账号 / 权限 / 日志原样保留；不拷 = 服务器上重新初始化管理员（本地测试账号 123 / lisi 等不会带过去） |
| `storage/qdrant_data/` | 可选 | 拷贝 = 免重新向量化；不拷 = 在服务器上重新全量入库 |
| `storage/admin_secret.key` | 建议不拷 | 只影响 Cookie 签名；不拷则首次启动生成新密钥，旧登录态失效，账号密码不受影响 |
| `tools/qdrant/qdrant` | **必须换 Linux 版** | 本地的是 macOS 二进制，服务器跑不了；从 Qdrant GitHub Releases 下载对应架构版本，放到 `tools/qdrant/qdrant` |

---

## 四、安装步骤

```bash
# 1. 代码 + 依赖
git clone <仓库地址> sky-rag && cd sky-rag
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Qdrant 二进制（Linux 版，替换 macOS 的）
mkdir -p tools/qdrant
curl -L https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-unknown-linux-gnu.tar.gz | tar xz -C tools/qdrant --strip-components=0
chmod +x tools/qdrant/qdrant

# 3. Ollama（官方脚本）+ 向量化模型
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3-embedding:4b

# 4. 配置
cp /path/to/本地/.env .env          # LLM_API_KEY 等
# 按需拷贝 knowledge/ 与 storage/auth.db（见第三节）
```

首次启动 `bash start.sh` 后：打开 `http://<服务器IP>:8501`：
- 拷了 `auth.db` → 直接用现有账号登录
- 没拷 → 前台会先引导创建管理员（创建后到 `:8700/admin` 登录建部门，员工即可自助注册）

---

## 五、安全配置（服务器必做）

### 1. Qdrant 只绑本机

`start.sh` 已配置 `QDRANT__SERVICE__HOST=127.0.0.1`：向量库只接受本机连接，
局域网 / 公网上的其他机器无法直接访问 6333。若 Qdrant 需要独立机器部署，
则必须改用带 API Key + TLS 的正式部署，并同步修改 `.env` 里的 `QDRANT_URL`。

### 2. systemd 守护（替代 nohup）

`/etc/systemd/system/` 下建四个单元（`User` 换成实际运行用户，`WorkingDirectory` 换成项目路径）：

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
Restart=always
[Install]
WantedBy=multi-user.target
```

```ini
# sky-rag-admin.service
[Unit]
Description=Sky RAG - Admin Console (8700)
After=network.target sky-rag-qdrant.service
[Service]
User=rag
WorkingDirectory=/opt/sky-rag
ExecStart=/opt/sky-rag/.venv/bin/python -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700 --proxy-headers --forwarded-allow-ips="127.0.0.1"
Restart=always
[Install]
WantedBy=multi-user.target
```

```ini
# sky-rag-web.service
[Unit]
Description=Sky RAG - Streamlit (8501)
After=network.target sky-rag-qdrant.service
[Service]
User=rag
WorkingDirectory=/opt/sky-rag
Environment=SERVER_HEADLESS=true
ExecStart=/opt/sky-rag/.venv/bin/streamlit run app.py --server.headless true --server.port 8501
Restart=always
[Install]
WantedBy=multi-user.target
```

```ini
# sky-rag-mcp.service（可选：团队 Agent 接入）
[Unit]
Description=Sky RAG - MCP HTTP (8760)
After=network.target sky-rag-qdrant.service
[Service]
User=rag
WorkingDirectory=/opt/sky-rag
Environment=MCP_TRANSPORT=streamable-http
ExecStart=/opt/sky-rag/.venv/bin/python -m src.mcp_server
Restart=always
[Install]
WantedBy=multi-user.target
```

启用：`systemctl daemon-reload && systemctl enable --now sky-rag-qdrant sky-rag-admin sky-rag-web sky-rag-mcp`

> Ollama 用官方安装脚本时自带 systemd 单元（`ollama.service`），无需另配。

**注意：不要给 uvicorn / streamlit 开多个 worker 或多实例**——缓存与台账按单进程设计。

### 3. 反向代理 + HTTPS（公网 / 需要 HTTPS 时）

nginx 站点示例（证书用 certbot 签发）：

```nginx
server {
    listen 443 ssl;
    server_name rag.example.com;
    # ssl_certificate / ssl_certificate_key 由 certbot 配置

    # 前台（Streamlit 需要 WebSocket 支持）
    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;      # WebSocket 升级
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600;                     # 流式回答长连接
    }

    # 管理后台（建议只允许办公网段访问）
    location /admin {
        allow 192.168.31.0/24;                       # 换成办公网段
        deny all;
        proxy_pass http://127.0.0.1:8700;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # MCP（团队 Agent 用；有 PAT 鉴权，也可只放行办公出口 IP）
    location /mcp {
        proxy_pass http://127.0.0.1:8760;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600;
    }
}
```

要点：
- `X-Forwarded-Host` / `X-Forwarded-Proto` 必须传：管理接口的同源校验与 Cookie 依赖它们；
- uvicorn 已带 `--proxy-headers --forwarded-allow-ips="127.0.0.1"`：登录日志里的访客 IP 才是真实 IP（否则记录的是反代地址）；
- HTTPS 部署后建议给 Cookie 追加 `Secure` 标记（`src/admin_app.py` 中 `samesite="lax"` 处，当前未加是为了兼容本地 HTTP 直连）。

### 4. 防火墙端口

| 端口 | 开放范围 |
|---|---|
| 80 / 443（反代）或 8501（直连） | 使用者 |
| 8700 管理后台 | 内网 / 办公网段（或仅经反代 /admin 路径访问） |
| 8760 MCP | 团队成员可达即可（有 PAT 鉴权） |
| 6333 / 11434 | **不开放**（只绑 127.0.0.1） |

---

## 六、注册策略

前台注册页默认对能访问到的人开放（注册后默认无任何权限，看不到知识内容）。
公网部署且不想让人随便注册时，两个办法：

1. **网络层**：反代上限制 `/`（或注册接口）的来源 IP；
2. **流程上**：告知团队"账号由管理员在后台统一创建"，并限制站点可达范围。

---

## 七、备份（建议每日）

```bash
#!/usr/bin/env bash
# /opt/sky-rag/backup.sh —— auth.db 用 SQLite 在线备份，其余打包
set -e
cd /opt/sky-rag
DEST=/opt/backups/$(date +%F)
mkdir -p "$DEST"
.venv/bin/python -c "import sqlite3; sqlite3.connect('storage/auth.db').backup(sqlite3.connect('$DEST/auth.db'))"
tar czf "$DEST/knowledge.tgz" knowledge
tar czf "$DEST/storage_meta.tgz" storage/index_state.json storage/domains.json .env
# Qdrant 快照：curl -X POST http://127.0.0.1:6333/snapshots （或直接停服拷 qdrant_data）
find /opt/backups -mtime +14 -delete
```

crontab：`0 2 * * * /opt/sky-rag/backup.sh`

**恢复**：停服务 → 用备份的 `auth.db` / `knowledge` / `storage_meta` 覆盖 → 重启。
向量库可由 `knowledge/` 全量重建（重建前先清库），所以最关键的是 `auth.db` 与 `knowledge/`。

---

## 八、升级流程

```bash
cd /opt/sky-rag
git pull                      # knowledge/ 与 storage/ 不在 git 里，不受影响
.venv/bin/pip install -r requirements.txt
systemctl restart sky-rag-web sky-rag-admin
# 测试（全部应通过）
.venv/bin/python -m unittest discover -s tests -t .
```

---

## 九、常见问题

| 现象 | 处理 |
|---|---|
| Qdrant 启动超时 | 看 `storage/qdrant.log`：多为二进制平台不对（macOS 版跑在 Linux）或端口被占 |
| 登录日志里 IP 全是同一个 | 反代未透传头：uvicorn 加 `--proxy-headers`，nginx 加 `X-Forwarded-For` |
| 页面频繁断线重连 | 反代未支持 WebSocket / 读超时太小 |
| 检索报连不上 Qdrant | `.env` 的 `QDRANT_URL` 与实际部署不符；或 Qdrant 绑定后用了非本机地址访问 |
| 入库很慢 | 无 GPU 跑 4B 向量化属正常；批量入库放夜间，或换云端 embedding 方案 |
| 想关闭前台开放注册 | 目前代码未提供开关——用网络层限制站点可达范围，或联系开发者加开关 |
