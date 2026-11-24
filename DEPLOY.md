# 长城互联网 · 企业知识库 部署手册

从零部署到公司服务器，照着做即可，**全程约 30 分钟**。
生产化选项（开机常驻 / HTTPS 反代 / 每日备份 / 升级流程）见 [docs/deploy_advanced.md](docs/deploy_advanced.md)。

---

## 一、系统组成

| 服务 | 端口 | 用途 | 对谁开放 |
|---|---|---|---|
| 知识库前台 | **8501** | 员工日常问答 | 全体员工 |
| RAG管理后台 | **8700** | 部门/员工/领域/数据管理 | 管理员 |
| MCP 知识服务 | 8760 | Agent 接入（可选） | 持令牌者 |
| Qdrant 向量库 | 6333 | 向量存储 | **仅本机** |
| Ollama | 11434 | 本地向量化模型 | **仅本机** |

防火墙只需放行 **8501**（8700 按需）；6333/11434 启动脚本默认只绑本机，**不要对网开放**。

## 二、环境要求

- Linux（x86_64/arm64）或 Windows Server；**Python 3.12+**
- 内存 ≥ 8GB，磁盘 ≥ 20GB（模型约 2.5GB + 知识数据）
- 能出网：访问 LLM API（如 DeepSeek）、下载依赖与模型
- 无 GPU 可运行（检索正常，批量入库较慢）

## 三、部署步骤（Linux）

```bash
# 1. 代码 + 依赖（使用已验收的 dev/v3.9 分支）
git clone -b dev/v3.9 <仓库地址> sky-rag && cd sky-rag
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Qdrant 二进制（Linux 版，放脚本约定位置 tools/qdrant/）
mkdir -p tools/qdrant
curl -L https://github.com/qdrant/qdrant/releases/latest/download/qdrant-x86_64-unknown-linux-gnu.tar.gz \
  | tar xz -C tools/qdrant && chmod +x tools/qdrant/qdrant

# 3. Ollama + 向量化模型
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3-embedding:4b

# 4. LibreOffice（仅老格式 doc/xls/ppt/wps 需要；不装不影响新格式与图片 OCR）
sudo apt install -y libreoffice

# 5. 本地精排模型（可选，约 600MB）
#    .env 设 RERANK_BACKEND=local_onnx 时必须；不下载则用默认 RERANK_BACKEND=llm 云端打分
.venv/bin/python scripts/download_rerank_model.py

# 6. 配置 .env（必填三项：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL，
#    各服务商示例见 .env.example 内注释；企业知识文件单独拷入 knowledge/ 目录，不进 git）
cp .env.example .env && vim .env

# 7. 启动全部服务（自动清残留、Qdrant 只绑 127.0.0.1、带就绪检查）
bash start.sh
```

## 四、初始化（首次启动后一次即可）

1. 打开 `http://<服务器IP>:8501` → 按引导**创建管理员**。
2. 打开 `http://<服务器IP>:8700/admin` → 管理员登录 → 建部门、业务方向、员工账号。
3. （可选）后台「MCP访问令牌」页签发令牌，供公司 Agent 通过 8760 接入。
4. **验证**：前台登录并提问一次（答案带来源引用即通）；后台「服务状态」页五个服务全绿。

## 五、Windows 服务器差异

- 依赖同上（Qdrant / Ollama 用 Windows 版可执行文件；LibreOffice 装官网安装包）。
- 双击 `start.bat` 一键启动；先改两处本机路径：`start.bat` 的 `QDRANT_DIR`、
  `service_ctl.bat` 的 `QDRANT_EXE`，换成公司服务器上 Qdrant 的实际目录。
- 单服务启停：`service_ctl.bat <front|admin|qdrant|ollama|mcp> <start|stop>`。

## 六、数据与备份

| 数据 | 位置 | 说明 |
|---|---|---|
| 知识源文件 | `knowledge/` | **核心资产，定期备份**（不进 git） |
| 账号/权限/日志/令牌 | `storage/auth.db` | 后台「数据管理 → 导出整库」可一键备份 |
| 向量数据 | `storage/qdrant_data/` | 拷走可免重新向量化；不拷则重新全量入库 |

每日自动备份与恢复方法见 [docs/deploy_advanced.md](docs/deploy_advanced.md) 第四节。

## 七、常见问题

| 现象 | 处理 |
|---|---|
| 员工打不开 8501 | 防火墙放行 8501；确认服务已启动 |
| 后台服务页 Ollama 显示「已启动，未就绪」 | 向量化模型未安装：`ollama pull qwen3-embedding:4b` |
| 老格式文档（doc/xls/wps）解析失败 | 未装 LibreOffice，见部署步骤 4 |
| 端口被占用 | `service_ctl.bat` / `start.sh` 会按端口清理残留；反复失败查 `storage/svc_*.log`、`storage/qdrant.log` |
| 管理后台打不开但前台正常 | 8700 未放行或服务未启动：`service_ctl.bat admin start` |
