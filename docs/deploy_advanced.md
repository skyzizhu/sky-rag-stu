# 生产化运维参考（可选）

> 主部署流程见根目录 [DEPLOY.md](../DEPLOY.md)。本文是上生产时的深化选项：
> 常驻服务、HTTPS 反代、每日备份与升级流程。日常试用/小团队部署不需要本文。

## 一、systemd 常驻（替代 nohup / 手动启动）

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

## 二、反向代理 + HTTPS（公网 / 需要 HTTPS 时）

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

### 防火墙端口

| 端口 | 开放范围 |
|---|---|
| 80 / 443（反代）或 8501（直连） | 使用者 |
| 8700 管理后台 | 内网 / 办公网段（或仅经反代 /admin 路径访问） |
| 8760 MCP | 团队成员可达即可（有 PAT 鉴权） |
| 6333 / 11434 | **不开放**（只绑 127.0.0.1） |

## 三、注册策略

前台注册页默认对能访问到的人开放（注册后默认无任何权限，看不到知识内容）。
不想让人随便注册时，两个办法：

1. **网络层**：反代上限制 `/`（或注册接口）的来源 IP；
2. **流程上**：告知团队"账号由管理员在后台统一创建"，并限制站点可达范围。

## 四、备份（建议每日）

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

## 五、升级流程

```bash
cd /opt/sky-rag
git pull                      # knowledge/ 与 storage/ 不在 git 里，不受影响
.venv/bin/pip install -r requirements.txt
systemctl restart sky-rag-web sky-rag-admin
# 测试（全部应通过）
.venv/bin/python -m unittest discover -s tests -t .
```

## 六、扩展 FAQ

| 现象 | 处理 |
|---|---|
| 登录日志里 IP 全是同一个 | 反代未透传头：uvicorn 加 `--proxy-headers`，nginx 加 `X-Forwarded-For` |
| 页面频繁断线重连 | 反代未支持 WebSocket / 读超时太小 |
| 入库很慢 | 无 GPU 跑 4B 向量化属正常；批量入库放夜间，或换云端 embedding 方案 |
| 想关闭前台开放注册 | 目前代码未提供开关——用网络层限制站点可达范围，或联系开发者加开关 |
