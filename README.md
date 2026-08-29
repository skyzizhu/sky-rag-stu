# 🧠 Sky Personal RAG —— 自己动手搭的个人知识库

> 在自己电脑上从 0 到 1 搭一个 RAG 个人知识库：把你的文档喂进去，然后用大白话提问，
> 系统会帮你查资料、给答案、标出处。
>
> 这既是一个能天天用的知识库工具，也是一份「边做边学 RAG」的学习笔记。

---

## 一、这个项目是什么

**一句话**：一个跑在自己 Mac 上的私人问答系统。

- 把你的笔记、需求文档、会议纪要、网页剪藏（支持 6 种格式）放进 `knowledge/` 文件夹；
- 用自然语言提问，比如「chunk_size 初始设多少？」；
- 系统自动找出最相关的知识片段，让云端大模型基于这些片段回答，并**标明每条答案出自哪个文件、哪一页**；
- 资料库里没有的内容，它会老实说不知道，不编造。

**技术组合**：

| 角色 | 用的什么 | 在哪里运行 |
|---|---|---|
| 开发语言 | Python | 本机 |
| 文档理解 | 自研解析模块 | 本机 |
| 知识向量化 | qwen3-embedding:4b（Ollama） | 本机，不联网 |
| 知识存放 | Qdrant 向量数据库 | 本机 |
| 回答生成 | 云端大模型 API（DeepSeek / Kimi / 智谱 / 通义 / OpenAI 均可） | 云端 |
| 操作界面 | Streamlit 网页 | 本机 |

---

## 二、RAG 是怎么工作的（先看懂原理）

RAG = Retrieval-Augmented Generation，检索增强生成。整个系统就是两条流水线：

### 流水线 A：把知识「存」进去

```text
📁 你的文件
   ↓ ① 文档解析      —— 不管什么格式，统一读成「正文 + 档案卡」
   ↓ ② 清洗          —— 去掉乱码、多余空行、PDF 页眉页脚
   ↓ ③ 知识切片      —— 把长文档切成一张张「知识卡片」
   ↓ ④ 向量化        —— 给每张卡片算一组「特征指纹」
   ↓ ⑤ 存入向量库    —— 卡片连指纹一起存进 Qdrant 档案馆
```

### 流水线 B：把答案「问」出来

```text
❓ 你的问题
   ↓ ⑥ 问题向量化    —— 问题也算成指纹（和文档用同一个模型）
   ↓ ⑦ 元数据过滤    —— 默认只搜「启用中」的知识；可手动限定领域/分类
   ↓ ⑧ 向量检索      —— 拿问题指纹去档案馆比对，找出最像的 Top K 张卡片
   ↓ ⑨ 资料组装      —— 按相关度排好、去重、标上出处编号
   ↓ ⑩ 提示词        —— 写一份「任务说明书」给大模型
   ↓ ⑪ 生成回答      —— 云端大模型基于资料回答
   ✅ 答案 + 参考来源（文件 / 领域 / 章节 / 页码）
```

### 每个节点在解决什么问题（白话版）

| 节点 | 干什么 | 为什么必须有 |
|---|---|---|
| ① 文档解析 | 6 种文件格式统一读出 | Word 和 PDF 长得完全不一样，得先统一 |
| ② 清洗 | 去杂质、保结构 | 脏文本会污染后面每一步 |
| ③ 切片 | 长文档 → 知识卡片 | 卡片越聚焦，搜得越准；整篇文档没法比对 |
| ④ 向量化 | 文字 → 特征指纹 | 让「意思相近」可以计算 |
| ⑤ 向量库 | 指纹 + 原文 + 档案一起存 | 千万张卡片也能毫秒级找相似 |
| ⑥ 问题向量化 | 问题也算指纹 | 才能和卡片指纹比对 |
| ⑦ 检索 | 找出最相关的 Top K | RAG 的「R」：先检索 |
| ⑧ 资料组装 | 排序、去重、标出处 | 给大模型一份干净的考卷附页 |
| ⑨ 提示词 | 立规矩 | 只准按资料答、不知就说不知、必须标出处 |
| ⑩ 生成回答 | 大模型组织语言 | RAG 的「G」：基于资料生成 |
| 引用展示 | 答案后面附来源 | 你随时能核对它说的是不是真的 |

> 💡 学习心法：每完成一个节点，标准不是「程序没报错」，
> 而是「我能看到这一步的输入和输出，并说出它解决了什么问题」。
> 项目里每个模块都可以单独运行打印结果，方法见「五、怎么用」。

---

## 三、版本规划

| 版本 | 目标 | 状态 |
|---|---|---|
| **V1** | 跑通基础 RAG 全链路：能入库、能检索、能回答、能看来源 | ✅ 已完成 |
| **V1.x** | 知识库管理：目录分类、统一元数据、手动过滤、归档机制 | ✅ 当前版本 |
| V2 | 增强检索质量：混合检索（向量 + 关键词）、重排序、自动理解过滤条件 | 🚧 规划中 |
| V3 | 可长期使用的知识系统：增量更新、版本管理、自动打标签 | 📋 规划中 |

两份计划文档：[基础开发计划](personal_rag_development_plan.md)、[知识库管理开发计划](Personal%20RAG%20—%20知识库管理开发计划.md)。

---

## 三点五、知识库管理规范（V1.x 新增）

### 目录就是分类

所有知识统一放在 `knowledge/` 主目录，**放对文件夹 = 自动打好分类**：

```text
knowledge/
├── work/          💼 工作       → projects / product / management / interview / experience
├── learning/      📚 学习       → ai（下分 rag / agent / llm / prompt） / product / technology / reading
├── life/          🌱 生活       → thinking / experience / travel / notes
├── reference/     📎 参考资料   → books / articles / documents
└── archive/       🗄 归档       —— 归档目录里的内容默认不参与回答
```

例如把文件放到 `knowledge/learning/ai/rag/` 下，系统自动得到：
`domain=learning`（领域）、`category=ai`（分类）、`topic=RAG`（主题）。
二级目录随意取名（自动转小写），三级目录名会提示主题（`rag` 会规范成 `RAG`）。

### 每张知识卡片都有一张完整「档案」

统一元数据包括：document_id（文档身份证，按路径生成，永不改变）、chunk_id（卡片编号，如 `doc_a913bc12_0001`）、来源、路径、领域、分类、主题、标签、章节路径、页码、版本、状态、各时间戳——共 18 个字段，一个不少。

### Markdown 可以自定义分类（Front Matter）

在 md 文件开头写一段声明，优先级高于目录推断：

```markdown
---
title: 我的标题
topic:
  - RAG
  - 向量数据库
tags:
  - Chunk
  - Embedding
version: "1.0"
---
```

### 检索范围由你控制

- **默认只搜「启用中（active）」的知识**：丢进 `archive/` 的内容自动退居二线，不再参与回答；
- 界面左侧可切换「仅 active / 包含归档 / 仅归档」，可限定某个领域；
- 命令行同样支持：`python -m src.retriever "RAG" --domain learning --status archive`。

### 知识管理台（左侧菜单「🗂 知识管理台」）

「🗂 知识管理台」是整个知识库的操作中枢：

- **统计卡片**：知识文件数、知识卡片数、覆盖领域数、归档文件数一目了然；
- **筛选搜索**：按关键词 / 领域 / 状态快速定位文件；
- **选中即管理**：点击表格任意一行，可查看该文件的完整档案与知识卡片原文，并直接执行——
  📥 **重新入库**（文件改完后刷新）、🗄 **归档**（退出日常检索）、🔄 **恢复**（回到原目录）、🗑 **移除**（仅从向量库删除，磁盘文件保留）。

---

## 四、从零到跑通（安装步骤）

### 你需要准备

- 一台 Mac（Apple Silicon 或 Intel 均可）
- Python 3.10 或更新版本
- [Ollama](https://ollama.com)（已安装并可运行）

> Windows / Linux 用户：只有 Qdrant 安装一步不同（推荐改用 Docker），其余步骤一致。

### 第 1 步：下载项目 + 创建虚拟环境

```bash
cd sky-rag-stu
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 第 2 步：填配置文件

```bash
cp .env.example .env
```

打开 `.env`，填这 3 行（以 DeepSeek 为例）：

```text
LLM_API_KEY=sk-你的key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

其他服务商只需换后两行，对照表见 `.env.example` 里的注释。
**`.env` 已被 git 忽略，不会被开源出去，放心填 Key。**

### 第 3 步：准备本地向量化模型

```bash
ollama pull qwen3-embedding:4b
```

（如果你已经装过，跳过。）

### 第 4 步：安装并启动向量数据库 Qdrant

```bash
bash scripts/install_qdrant.sh    # 只需运行一次，下载约 40MB
bash scripts/start_qdrant.sh      # 每次使用前启动；停止按 Ctrl+C
```

启动后浏览器打开 <http://localhost:6333/dashboard> 能看到控制台，就说明成功了。
数据保存在 `storage/qdrant_data/`，重启电脑不丢。

> 备选方案：如果你更习惯 Docker，也可以
> `docker run -d -p 6333:6333 -v $(pwd)/storage/qdrant_data:/qdrant/storage qdrant/qdrant`，
> 效果完全一样。

### 第 5 步：环境自检

```bash
python check_env.py          # 基础检查，不花 API 的钱
python check_env.py --with-llm   # 顺手真实调用一次大模型，验证 Key
```

每一项都会显示 ✅ / ⚠️ / ❌ 和修复提示，全绿了再往下走。

### 第 6 步：把知识入库

项目自带 6 个「示例_」开头的示例文件（覆盖全部 6 种格式），可以直接体验：

```bash
python ingest.py             # 入库 knowledge/ 里的全部文件
# python ingest.py --rebuild # 想清空重来时用
```

### 第 7 步：打开界面，开始提问

```bash
streamlit run app.py
```

浏览器会自动打开页面：先去「📤 上传文档」确认 6 个示例已入库，
再到「💬 知识库问答」随便问，比如：

- 「知识切片的 chunk_size 初始建议设多少？」
- 「卡片笔记法的核心是什么？」
- 「知了笔记 2.0 卖多少钱？」

---

## 五、怎么用

### 日常使用

| 想做什么 | 怎么做 |
|---|---|
| 添加新知识 | 界面「📤 上传文档」拖文件进去点入库；或把文件丢进 `knowledge/` 对应领域目录后运行 `python ingest.py` |
| 提问 | 界面聊天框直接问（回答逐字流式输出）；也可以点首页的「建议问题」直接体验 |
| 看答案出处 | 每个回答下方展开「📚 参考来源」，含领域、分类、版本、状态标签和原文摘录 |
| 管理知识 | 「🗂 知识管理台」：筛选、查看、重新入库、归档、恢复、移除 |
| 检查系统状态 | 「⚙️ 设置与状态」：状态灯 + 重新检测 |
| 清空重来 | 「⚙️ 设置与状态」底部「🧹 维护操作」→ 清空并重建 |

### 界面布局：左侧菜单 + 右侧页面

打开后左侧是四个菜单入口，右侧是对应页面：

| 菜单 | 页面内容 |
|---|---|
| 💬 知识库问答 | 首页横幅、建议问题、流式回答、卡片式来源、调试面板 |
| 📤 上传文档 | 选择领域目录入库，逐文件入库结果，目录现状 |
| 🗂 知识管理台 | 统计卡片、筛选搜索、查看档案、归档/恢复/移除/重新入库 |
| ⚙️ 设置与状态 | 系统状态灯、检索设置（对问答即时生效）、参数总览、维护操作 |

### 命令行方式（不打开界面也能用）

```bash
python -m src.pipeline "你的问题"              # 提问
python -m src.pipeline --retrieve-only "问题"  # 只看检索到了什么（不花钱）
python -m src.retriever "你的问题"             # 单独体验检索节点
python -m src.retriever "RAG" --domain learning --status archive   # 带过滤检索
python -m src.metadata                         # 看知识目录的自动分类结果
python -m src.parser                           # 单独看解析节点：6 种文件解析成什么样
python -m src.chunker                          # 单独看切片节点：文档被切成了哪些卡片
python -m src.vector_store                     # 看知识库盘点：存了多少卡片
python scripts/test_km_scenarios.py            # 知识库管理机制自动测试（5 场景 + ID 稳定性）
```

> 这就是学习模式：每个节点都能单独运行、单独观察输入输出。

### 调参

所有参数都在 `.env` 里改，改完重新入库即可：

| 参数 | 含义 | 默认 |
|---|---|---|
| `CHUNK_SIZE` | 每张知识卡片的目标长度（字符） | 600 |
| `CHUNK_OVERLAP` | 相邻卡片之间重复多少（防切断语义） | 100 |
| `TOP_K` | 每次提问召回几张卡片 | 5 |
| `CONTEXT_MAX_CHARS` | 给大模型看的资料总字数上限 | 6000 |
| `LLM_TEMPERATURE` | 回答的稳定度（越小越严谨） | 0.2 |

---

## 六、评测：怎么知道系统好不好

`eval_set.json` 是一份考卷：每道题 = 问题 + 标准答案在哪个文件 + 答案该包含的关键词。
其中还有几道「知识库里根本没有答案」的陷阱题，专门考系统会不会老实说不知道。

```bash
python eval.py --no-llm    # 先只考检索（免费，秒出）
python eval.py             # 再考完整问答
python eval.py --save      # 结果存档到 storage/
```

看三个指标：

- **检索命中率**：标准来源有没有进入召回的前几名 —— 检索是地基；
- **回答有据率**：答案里是否真的用上了资料；
- **无答案拒答率**：没资料时会不会胡编 —— 这是 RAG 的底线。

> 你应该用**自己的知识**出 20~50 道题替换示例考卷——用真实问题评出来的结果才有意义。
> 检索不命中多 → 调大 `TOP_K` 或调 `CHUNK_SIZE`；改一个参数、重跑一次评测，对比分数。

---

## 七、目录结构（每个文件是干什么的）

```text
sky-rag-stu/
├── knowledge/                 📁 你的知识文件放这里（按 work/learning/life/reference/archive 分类）
├── storage/                   📁 本地运行数据（向量库数据、评测报告），已 gitignore
│
├── src/                       📁 核心源码：一条流水线上的各个节点
│   ├── config.py              ⚙️  配置中心：读 .env，全项目共用
│   ├── metadata.py            🗂  知识库管理：目录→领域/分类/主题、文档身份证、Front Matter
│   ├── manage.py              🎛  知识管理操作：重新入库 / 归档 / 恢复 / 移除
│   ├── parser.py              ①  文档解析：6 种格式 → 统一「正文+档案卡」
│   ├── cleaner.py             ②  清洗：去杂质、去页眉页脚、保结构
│   ├── chunker.py             ③  切片：长文档 → 带档案的知识卡片
│   ├── embedding.py           ④  向量化：卡片 → 特征指纹（Ollama 本地模型）
│   ├── vector_store.py        ⑤  向量库：Qdrant 存取、元数据索引、按文档删除
│   ├── retriever.py           ⑥⑦ 检索：问题 → 默认过滤+元数据过滤 → Top K 卡片
│   ├── context.py             ⑧  资料组装：排序、去重、标出处、控长度
│   ├── prompt.py              ⑨  提示词：给大模型的任务说明书
│   ├── llm.py                 ⑩  大模型调用：OpenAI 兼容接口 + 友好报错
│   └── pipeline.py            🔗 总控：入库/问答两条流水线串起来
│
├── ingest.py                  📥 命令行入库入口
├── app.py                     🖥 Streamlit 网页界面（问答 / 上传 / 知识库管理）
├── eval.py                    📊 评测脚本
├── eval_set.json              📝 评测考卷（示例 12 题，请换成你自己的）
├── check_env.py               🩺 环境自检：哪里没配好，直接告诉你怎么修
│
├── scripts/                   📁 安装、示例与测试脚本
│   ├── install_qdrant.sh      ⬇️  下载 Qdrant（只需一次）
│   ├── start_qdrant.sh        🚀 启动 Qdrant
│   ├── make_sample_files.py   🎁 重新生成示例文件（docx/pdf/rtf）
│   └── test_km_scenarios.py   ✅ 知识库管理机制自动测试
│
├── .env.example               📄 配置模板（复制成 .env 用）
├── requirements.txt           📄 Python 依赖清单
├── personal_rag_development_plan.md          📄 基础开发计划（V1/V2/V3 细项）
├── Personal RAG — 知识库管理开发计划.md      📄 知识库管理开发计划
└── README.md                  📄 本文件
```

---

## 八、常见问题（FAQ）

**Q：界面显示「连不上本地 Ollama」？**
先在终端跑 `ollama list`，能看到模型列表就说明 Ollama 正常；再检查 `.env` 里的 `OLLAMA_URL`（默认 `http://localhost:11434`）。

**Q：显示「找不到模型 qwen3-embedding:4b」？**
运行 `ollama pull qwen3-embedding:4b`。如果换成其他 embedding 模型，改 `.env` 里的 `EMBEDDING_MODEL` 后必须**重建知识库**（不同模型的指纹规格不同，不能混放）。

**Q：连不上 Qdrant / 界面左侧 Qdrant 是红灯？**
先运行 `bash scripts/start_qdrant.sh`，确认 6333 端口没被占用。每次重启电脑后需要重新启动它。

**Q：换了 LLM 服务商要改哪里？**
只改 `.env` 里 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 三行，代码不用动。

**Q：某个文件入库失败了？**
入库日志会写明原因。最常见的是加密 PDF 和扫描版 PDF（图片型，没有文字层），后者需要 OCR 能力，属于后续版本规划。

**Q：回答不准确 / 检索不到，先调什么？**
按顺序试：调大 `TOP_K` → 调 `CHUNK_SIZE`（300~800 之间试）→ 调 `CHUNK_OVERLAP`。每改一次跑一遍 `python eval.py --no-llm` 对比命中率。

**Q：我明明有相关文档，为什么检索不到？**
先看两件事：① 文件是否已经重新入库（`python ingest.py`）；② 文件是否放在 `knowledge/archive/` 里——归档内容默认不参与回答，在界面左侧把检索范围切到「包含归档」即可。

**Q：数据安全吗？**
全部知识、向量、运行数据都在你本机；唯一出网的是「提问时把检索到的相关片段发给云端大模型」。如果内容敏感，选一个可信的大模型服务商，或未来接入本地大模型。

---

## 九、开源与致谢

- License：[MIT](LICENSE)
- 本项目是「通过做项目学习 RAG」的产物，架构与开发计划文档完整保留在仓库里，欢迎对照学习
- 感谢开源生态：[Ollama](https://ollama.com)、[Qdrant](https://qdrant.tech)、[Streamlit](https://streamlit.io)、[pypdf](https://github.com/py-pdf/pypdf)、[python-docx](https://github.com/python-openxml/python-docx)、[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)、[striprtf](https://github.com/joshy/striprtf)

欢迎 Issue 和 PR 🎉
