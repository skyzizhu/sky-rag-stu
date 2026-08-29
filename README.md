# 🧠 Sky Personal RAG —— 自己动手搭的个人知识库

> 在自己电脑上从 0 到 1 搭一个 RAG 个人知识库：把你的文档喂进去，然后用大白话提问，
> 系统会帮你查资料、给答案、标出处。
>
> 这既是一个能天天用的知识库工具，也是一份「边做边学 RAG」的学习笔记。

## 📖 文档导航

| 想了解什么 | 去哪里 |
|---|---|
| **系统学习 RAG**（每个节点做什么、注意什么、Metadata 格式、提示词规则） | 📄 [**prd.md**](prd.md) —— 产品需求文档，本项目的学习主文档 |
| 完整开发过程（V1/V2/V3 逐项清单） | [personal_rag_development_plan.md](personal_rag_development_plan.md) |
| 知识库管理机制的设计 | [Personal RAG — 知识库管理开发计划.md](Personal%20RAG%20—%20知识库管理开发计划.md) |
| 怎么安装、怎么用 | 就在本 README，往下看 ↓ |

---

## 一、这个项目是什么

**一句话**：一个跑在自己 Mac 上的私人问答系统。

- 把你的笔记、需求文档、会议纪要、网页剪藏（支持 6 种格式）放进 `knowledge/` 文件夹；
- 用自然语言提问，比如「chunk_size 初始设多少？」；
- 系统自动找出最相关的知识片段，让云端大模型基于这些片段回答，并**标明每条答案出自哪个文件、哪一页**；
- 资料库里没有的内容，它会老实说不知道，不编造。

**两种打开方式**：
- **当工具用**：日常知识问答，文档一入库，随问随查；
- **当教材学**：打开调试模式（默认开启），每次回答都附带完整的「RAG 节点时间线」——
  14 个节点逐个展示发生时间、做了什么、输入与输出，对照 [prd.md](prd.md) 的节点讲解，边用边学。

**技术组合**：

| 角色 | 用的什么 | 在哪里运行 |
|---|---|---|
| 开发语言 | Python | 本机 |
| 文档理解 | 自研解析模块 | 本机 |
| 知识向量化 | qwen3-embedding:4b（Ollama） | 本机，不联网 |
| 知识存放 | Qdrant 向量数据库 | 本机 |
| 回答生成 | 云端大模型 API（DeepSeek / Kimi / 智谱 / 通义 / OpenAI 均可） | 云端 |
| 关键词检索 | BM25（rank-bm25 + jieba 分词） | 本机 |
| 回答精排 | LLM 重排（可替换为 bge-reranker 等专用模型） | 云端/本机 |
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

### 流水线 B：把答案「问」出来（V2 增强后）

```text
❓ 你的问题
   ↓ ⑥ Query 理解/改写（V2）—— LLM 把口语化提问改写成检索友好查询，并推断过滤条件
   ↓ ⑦ 问题向量化    —— 问题也算成指纹（和文档用同一个模型）
   ↓ ⑧ 元数据过滤    —— 默认只搜「启用中」的知识；可手动限定领域/分类
   ↓ ⑨ 混合检索（V2）—— 向量通道 + BM25 关键词通道两路召回，RRF 融合排名
   ↓ ⑩ Rerank 精排（V2）—— LLM 逐条阅读候选打相关度分，精选 Top K
   ↓ ⑪ 资料组装      —— 排序、去重、相邻卡片拼接、标上出处编号
   ↓ ⑫ 提示词        —— 写一份「任务说明书」给大模型
   ↓ ⑬ 生成回答      —— 云端大模型基于资料回答
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
| ⑥ Query 理解/改写（V2） | 口语化提问 → 检索友好查询 + 推断过滤条件 | 「旧版笔记里写的多少」这种话，机器听不懂就搜不准 |
| ⑦ 问题向量化 | 问题也算指纹 | 才能和卡片指纹比对 |
| ⑧ 检索（混合，V2） | 向量通道 + BM25 关键词通道，RRF 融合 | 向量懂「意思相近」，关键词懂「一字不差」，互补 |
| ⑨ Rerank 精排（V2） | LLM 逐条阅读候选打相关度分 | 粗排只看文字指纹，精排才理解内容 |
| ⑩ 资料组装 | 排序、去重、相邻拼接、标出处 | 给大模型一份干净完整的考卷附页 |
| ⑪ 提示词 | 立规矩 | 只准按资料答、不知就说不知、必须标出处 |
| ⑫ 生成回答 | 大模型组织语言 | RAG 的「G」：基于资料生成 |
| 引用展示 | 答案后面附来源 | 你随时能核对它说的是不是真的 |

> 💡 学习心法：每完成一个节点，标准不是「程序没报错」，
> 而是「我能看到这一步的输入和输出，并说出它解决了什么问题」。
> 项目里每个模块都可以单独运行打印结果，方法见「五、怎么用」。

---

## 三、版本规划

| 版本 | 目标 | 状态 |
|---|---|---|
| **V1** | 跑通基础 RAG 全链路：能入库、能检索、能回答、能看来源 | ✅ 已完成 |
| **V1.x** | 知识库管理：目录分类、统一元数据、手动过滤、归档机制 | ✅ 已完成 |
| **V2** | 增强检索质量：Query 理解/改写、混合检索（向量+BM25）、Rerank 精排、上下文优化 | ✅ 当前版本 |
| V3 | 可长期使用的知识系统：增量更新、版本管理、自动打标签 | 📋 规划中 |

三份文档：[产品需求文档 prd.md](prd.md)（学习主文档）、[基础开发计划](personal_rag_development_plan.md)、[知识库管理开发计划](Personal%20RAG%20—%20知识库管理开发计划.md)。

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
- 检索范围可切换「仅 active / 包含归档 / 仅归档」，可限定某个领域；
- 命令行同样支持：`python -m src.retriever "RAG" --domain learning --status archive`。

### 知识管理

除了入库和检索，系统还内置了知识的管理能力：查看每个文件的档案与卡片原文、按关键词/领域/状态筛选、单文件重新入库、归档（退出日常检索）、恢复、移除（仅删知识，磁盘文件保留）、一键清空重建。这些操作都能在网页里完成，具体入口打开界面就能看到。

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
| 添加新知识 | 把文件丢进 `knowledge/` 对应领域目录后运行 `python ingest.py`；或直接在网页里上传入库 |
| 提问 | 网页聊天框直接问，也可以点首页的「建议问题」直接体验 |
| 看答案出处 | 每个回答下方展开「📚 参考来源」，含领域、分类、版本、状态标签和原文摘录 |
| 管理知识 | 网页里可完成：筛选、查看档案、重新入库、归档、恢复、移除 |
| 学习 RAG 节点流转 | 调试模式（默认开启）下，每次回答附带 14 节点时间线；对照 [prd.md](prd.md) 第 2、3 节食用 |
| 检查系统状态 / 清空重建 | 网页「设置」菜单里统一操作 |

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
| `CONTEXT_MAX_PER_DOC` | 同一文档最多进入回答的卡片数（保持来源多样） | 3 |
| `QUERY_UNDERSTANDING` | Query 理解/改写开关（V2，多用一次 LLM 调用） | true |
| `HYBRID_SEARCH` | 混合检索开关（向量 + BM25 关键词，V2） | true |
| `RERANK_ENABLED` | Rerank 精排开关（V2，多用一次 LLM 调用） | true |
| `RERANK_RECALL_K` | 精排前的宽召回候选数 | 10 |
| `LLM_TEMPERATURE` | 回答的稳定度（越小越严谨） | 0.2 |

> 💡 V2 三个开关的经验法则：日常问答开「混合检索」就够了（免费提准）；要处理口语化提问和归档意图时开「Query 理解」；对回答质量要求高、不在乎多几秒延迟时开「Rerank」。三个开关都在网页「设置 → 检索设置」里可随时切换。

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
│   ├── query_understanding.py 🧠 V2 Query 理解/改写：口语化提问 → 检索查询 + 过滤条件
│   ├── keyword_search.py      🔑 V2 BM25 关键词检索通道（jieba 分词）
│   ├── reranker.py            🏆 V2 Rerank 精排：LLM 逐条相关度打分
│   ├── context.py             ⑧  资料组装：排序、去重、标出处、控长度
│   ├── prompt.py              ⑨  提示词：给大模型的任务说明书
│   ├── llm.py                 ⑩  大模型调用：OpenAI 兼容接口 + 友好报错
│   └── pipeline.py            🔗 总控：入库/问答两条流水线串起来
│
├── ingest.py                  📥 命令行入库入口
├── app.py                     🖥 Streamlit 网页界面（问答 / 上传 / 知识库管理）
├── eval.py                    📊 评测脚本
├── eval_compare.py            📊 V2 对比评测：4 种检索配置矩阵跑分
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

**Q：连不上 Qdrant / 系统状态里 Qdrant 是红灯？**
先运行 `bash scripts/start_qdrant.sh`，确认 6333 端口没被占用。每次重启电脑后需要重新启动它。

**Q：换了 LLM 服务商要改哪里？**
只改 `.env` 里 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 三行，代码不用动。

**Q：某个文件入库失败了？**
入库日志会写明原因。最常见的是加密 PDF 和扫描版 PDF（图片型，没有文字层），后者需要 OCR 能力，属于后续版本规划。

**Q：回答不准确 / 检索不到，先调什么？**
按顺序试：调大 `TOP_K` → 调 `CHUNK_SIZE`（300~800 之间试）→ 调 `CHUNK_OVERLAP`。每改一次跑一遍 `python eval.py --no-llm` 对比命中率。

**Q：我明明有相关文档，为什么检索不到？**
先看两件事：① 文件是否已经重新入库（`python ingest.py`）；② 文件是否放在 `knowledge/archive/` 里——归档内容默认不参与回答，把检索范围切到「包含归档」即可。

**Q：数据安全吗？**
全部知识、向量、运行数据都在你本机；唯一出网的是「提问时把检索到的相关片段发给云端大模型」。如果内容敏感，选一个可信的大模型服务商，或未来接入本地大模型。

---

## 九、开源与致谢

- License：[MIT](LICENSE)
- 本项目是「通过做项目学习 RAG」的产物，架构与开发计划文档完整保留在仓库里，欢迎对照学习
- 感谢开源生态：[Ollama](https://ollama.com)、[Qdrant](https://qdrant.tech)、[Streamlit](https://streamlit.io)、[pypdf](https://github.com/py-pdf/pypdf)、[python-docx](https://github.com/python-openxml/python-docx)、[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)、[striprtf](https://github.com/joshy/striprtf)

欢迎 Issue 和 PR 🎉
