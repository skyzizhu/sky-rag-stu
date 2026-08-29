# Personal RAG 开发计划

> 目标：在 Mac 上从 0 到 1 搭建一个个人 RAG 系统，一边学习 RAG 的完整架构，一边沉淀工作与生活知识。

---

## 0. 当前技术方案

- **开发语言**：Python
- **LLM**：云端 API
- **Embedding**：本地模型（建议 Ollama + qwen3-embedding）
- **向量数据库**：Qdrant（本地）
- **UI**：Streamlit
- **支持文件格式**：
  - `.txt`
  - `.md`
  - `.rtf`
  - `.html`
  - `.docx`
  - `.pdf`

### Metadata 设计建议

从 V1 开始就保留以下字段：

```json
{
  "source": "rag学习笔记.md",
  "file_type": "markdown",
  "title": "RAG学习笔记",
  "section": "Metadata",
  "category": "学习",
  "created_at": "2026-08-29",
  "updated_at": "2026-08-29",
  "version": "1.0",
  "status": "active",
  "page": null
}
```

说明：

- `version`：建议 V1 就保留，哪怕第一版暂时固定为 `1.0`
- `status`：建议 V1 就保留，第一版可只使用 `active`
- V1 先“存字段但不做复杂逻辑”
- V3 再正式实现版本切换、历史版本、失效状态、增量更新

---

# 版本总览

| 版本 | 目标 | 当前状态 |
|---|---|---|
| V1 | 跑通基础 RAG 全链路 | **当前开发版本** |
| V2 | 增强检索质量 | 未开始 |
| V3 | 做成可长期使用的个人知识系统 | 未开始 |

---

# V1：基础 RAG

## 目标

跑通最基础、最核心的 RAG 链路：

```text
文件
→ 文档解析
→ 清洗/结构化
→ Chunk
→ Embedding
→ Qdrant
→ 用户提问
→ Query Embedding
→ 向量检索
→ Top K Chunk
→ Context 组装
→ Prompt
→ LLM API
→ 答案 + 来源
```

> V1 的重点不是“效果最强”，而是每一步都能看懂、能打印、能调试。

## V1.1 项目初始化

- [x] 创建项目目录
- [x] 创建 Python 虚拟环境
- [x] 安装依赖
- [x] 配置 `.env`
- [x] 配置 LLM API Key（2026-08-29 配置 DeepSeek 并真实调用通过）
- [x] 安装并启动 Ollama
- [x] 下载本地 Embedding 模型
- [x] 安装并启动 Qdrant
- [x] 确认 Qdrant Dashboard 可访问

建议目录：

```text
personal-rag/
├── knowledge/
├── storage/
├── src/
│   ├── parser.py
│   ├── cleaner.py
│   ├── chunker.py
│   ├── embedding.py
│   ├── vector_store.py
│   ├── retriever.py
│   ├── prompt.py
│   ├── llm.py
│   └── pipeline.py
├── ingest.py
├── app.py
├── eval.py
├── .env
├── requirements.txt / pyproject.toml
└── README.md
```

## V1.2 文档解析 Parser

### 目标

把不同文件统一解析成内部 `Document` 数据结构。

- [x] 支持 `.txt`
- [x] 支持 `.md`
- [x] 支持 `.rtf`
- [x] 支持 `.html`
- [x] 支持 `.docx`
- [x] 支持 `.pdf`
- [x] 统一输出 `text + metadata`
- [x] 打印解析结果进行人工检查
- [x] 对解析失败文件输出明确错误信息

建议统一结构：

```json
{
  "text": "文档正文",
  "metadata": {
    "source": "文件名",
    "file_type": "pdf",
    "title": "标题",
    "section": "章节",
    "page": 1,
    "created_at": null,
    "updated_at": null,
    "version": "1.0",
    "status": "active"
  }
}
```

## V1.3 清洗与结构化

- [x] 去除多余空行
- [x] 去除异常空格
- [x] 清理 PDF 页眉页脚
- [x] 清理重复内容（Context 组装时去重）
- [x] 尽量保留标题层级
- [x] 尽量保留段落结构
- [x] 保留页码到 Metadata
- [x] 保留文件来源到 Metadata
- [x] 检查正文和 Metadata 是否对应

## V1.4 Chunk 切片

### 第一版策略

优先使用：

**标题/段落优先 + 固定长度兜底 + Overlap**

- [x] 定义 Chunk 数据结构
- [x] 支持按段落切分
- [x] 支持最大 Chunk Size
- [x] 支持 Overlap
- [x] Chunk 继承原 Document Metadata
- [x] 每个 Chunk 生成唯一 `chunk_id`
- [x] 打印 Chunk 内容
- [x] 检查是否出现明显语义断裂
- [x] 可配置 `chunk_size`
- [x] 可配置 `chunk_overlap`

建议初始参数：

```text
chunk_size: 500~800 中文字符
chunk_overlap: 80~150 中文字符
```

> 具体参数后续通过评测调整，不要把初始值当成最终答案。

## V1.5 Embedding

- [x] 封装本地 Embedding 客户端
- [x] 使用同一个模型完成文档和 Query Embedding
- [x] 将每个 Chunk 转换成向量
- [x] 打印向量维度
- [x] 检查空文本不进入 Embedding
- [x] 添加 Embedding 异常处理

输入：

```text
Chunk 原始文本
```

输出：

```text
Embedding Vector
```

## V1.6 Qdrant 向量库

每个 Point 至少保存：

```text
id
vector
text
metadata
```

- [x] 创建 Collection
- [x] 确认 Vector Size 与 Embedding 模型一致
- [x] 写入 Chunk Vector
- [x] 写入 Chunk 原文
- [x] 写入 Metadata
- [x] 支持批量写入
- [x] 通过 Dashboard 检查数据
- [x] 支持清空/重建 Collection
- [x] 支持按 `source` 查询数据

## V1.7 用户 Query 向量化

- [x] 接收用户自然语言问题
- [x] 将 Query 传给同一个 Embedding 模型
- [x] 得到 Query Vector
- [x] 打印 Query Vector 基本信息
- [x] 验证 Query 与文档使用同一 Embedding 模型

## V1.8 向量检索

- [x] 使用 Query Vector 搜索 Qdrant
- [x] 支持配置 Top K
- [x] 默认先使用 Top 5
- [x] 返回相似度分数
- [x] 返回 Chunk 原文
- [x] 返回 Metadata
- [x] 在终端打印召回结果
- [x] 人工判断召回是否合理

输出示例：

```text
Top 1
score: 0.83
source: rag学习笔记.md
section: Metadata
text: Metadata 是附着在 Chunk 上的结构化信息……
```

## V1.9 Context 组装

- [x] 从 Top K 中提取 Chunk 原文
- [x] 按相关度排序
- [x] 去除明显重复 Chunk
- [x] 拼接来源信息
- [x] 控制 Context 最大长度
- [x] 输出最终 Context 用于调试

## V1.10 Prompt

系统提示词至少包括：

- [x] 明确模型角色
- [x] 要求优先基于知识库资料回答
- [x] 资料不足时明确说明不知道
- [x] 禁止无依据编造
- [x] 要求输出引用来源
- [x] 区分“知识库资料”和“系统指令”

用户输入包括：

```text
检索到的 Context
+
用户原始问题
```

- [x] 能打印最终发送给 LLM 的 Prompt

## V1.11 LLM API

- [x] 封装 LLM API Client
- [x] 支持配置模型名
- [x] 支持配置 temperature
- [x] 支持配置 max_tokens
- [x] 发送 System Prompt
- [x] 发送 Context + User Query
- [x] 返回模型答案
- [x] API 异常处理
- [x] 请求耗时日志

## V1.12 引用来源

- [x] 答案中展示来源文件
- [x] 如果有 page，展示页码
- [x] 如果有 section，展示章节
- [x] 不允许引用未召回的文档
- [x] 前端支持展示“参考来源”

## V1.13 Streamlit 页面

- [x] 文件上传
- [x] 批量入库按钮
- [x] 输入问题
- [x] 展示回答
- [x] 展示来源
- [x] 展示召回 Chunk（调试模式）
- [x] 展示相似度分数（调试模式）
- [x] 支持清空对话

## V1.14 基础评测

先准备 20~50 个自己的问题。

- [x] 创建 `eval.json`
- [x] 每个问题记录标准来源
- [x] 测试正确 Chunk 是否进入 Top K
- [x] 测试最终答案是否有依据（2026-08-29 首轮：10/10 有据）
- [x] 测试资料不存在时是否拒绝编造（2026-08-29 首轮：2/2 正确拒答）
- [x] 记录错误案例（首轮 12 题无错误案例）
- [x] 调整 Chunk Size（首轮指标全绿，维持 600）
- [x] 调整 Overlap（首轮指标全绿，维持 100）
- [x] 调整 Top K（首轮指标全绿，维持 5）

## V1 完成标准

满足以下条件即可进入 V2：

- [x] 6 种文件均能正常入库
- [x] Qdrant 中可看到 Vector + Text + Metadata
- [x] 用户问题可完成语义检索
- [x] 能看到召回 Chunk
- [x] 能完成 Context 组装
- [x] 能调用 LLM API 回答（2026-08-29 端到端验证通过）
- [x] 能展示来源（界面与命令行均已验证）
- [ ] 至少完成一轮 20+ 问题的人工评测（示例集 12 题全绿；待用自己的真实知识出 20+ 题再测一轮）

---

# V2：增强检索

## 目标

从“能搜到”升级为：

**更容易搜准、搜全、排对。**

整体链路：

```text
用户问题
→ Query Understanding / Rewrite
→ Metadata Filter
→ Vector Search
→ BM25 / Full-text Search
→ Hybrid Search
→ Recall Merge
→ Rerank
→ Context
→ LLM
```

## V2.1 Query Understanding / Rewrite

- [ ] 设计 Query Understanding Prompt
- [ ] 使用一次 LLM 调用同时完成理解和改写
- [ ] 输出结构化 JSON
- [ ] 输出 `vector_query`
- [ ] 输出 `keyword_query`
- [ ] 输出 `filters`
- [ ] 支持提取时间
- [ ] 支持提取 category
- [ ] 支持提取 topic
- [ ] 支持提取 source/type
- [ ] 支持当前/历史意图判断
- [ ] JSON Schema 校验
- [ ] 异常时回退到原始 Query

输出示例：

```json
{
  "intent": "查询RAG相关学习笔记",
  "vector_query": "RAG Metadata 的作用和设计方式",
  "keyword_query": ["RAG", "Metadata", "元数据"],
  "filters": {
    "category": "学习",
    "status": "active"
  }
}
```

## V2.2 Metadata Filter

- [ ] 定义允许过滤的 Metadata 字段
- [ ] Query Understanding 输出过滤条件
- [ ] 系统组装 Qdrant Filter
- [ ] 支持 `category`
- [ ] 支持 `topic`
- [ ] 支持 `year`
- [ ] 支持 `source`
- [ ] 支持 `status`
- [ ] 支持 `version`
- [ ] 调试界面展示最终 Filter

## V2.3 全文检索 / BM25

- [ ] 为 Chunk 建立全文索引
- [ ] 支持 Keyword Query
- [ ] 支持 Top K
- [ ] 输出关键词检索分数
- [ ] 测试专有名词、数字、缩写类问题
- [ ] 对比纯向量检索效果

## V2.4 Hybrid Search

- [ ] 同时执行 Vector Search
- [ ] 同时执行 BM25 Search
- [ ] 合并两路召回结果
- [ ] Chunk 去重
- [ ] 设计融合排序方法
- [ ] 记录两路召回来源
- [ ] 对比 Hybrid 与纯 Vector 效果

## V2.5 Rerank

- [ ] 选择本地或 API Reranker
- [ ] 输入 User Query + Candidate Chunks
- [ ] 对候选 Chunk 重新打分
- [ ] 默认 Recall Top 10~20
- [ ] Rerank 后取 Top 3~5
- [ ] 打印 Rerank 前后排序
- [ ] 对比评测指标

## V2.6 Context 优化

- [ ] Chunk 去重
- [ ] 相邻 Chunk 补全
- [ ] Context 长度控制
- [ ] 来源排序
- [ ] 重要 Chunk 优先
- [ ] 避免同一文档重复占满上下文

## V2.7 V2 评测

- [ ] 扩展到 50~100 个问题
- [ ] 记录 Recall@K
- [ ] 记录正确来源排名
- [ ] 比较 Vector Only
- [ ] 比较 BM25 Only
- [ ] 比较 Hybrid
- [ ] 比较 Hybrid + Rerank
- [ ] 记录延迟
- [ ] 记录 LLM API 成本
- [ ] 输出一份简单评测报告

## V2 完成标准

- [ ] Query Understanding 稳定输出结构化结果
- [ ] Metadata Filter 可正常工作
- [ ] 全文检索跑通
- [ ] Hybrid Search 跑通
- [ ] Rerank 跑通
- [ ] 有明确的 V1 vs V2 评测数据
- [ ] 能解释“为什么 V2 比 V1 更准”

---

# V3：个人知识系统

## 目标

从“学习型 Demo”升级成：

**可以长期维护和真正使用的个人知识库。**

## V3.1 增量入库

- [ ] 记录文件哈希
- [ ] 检测新文件
- [ ] 检测文件修改
- [ ] 未修改文件不重复 Embedding
- [ ] 修改文件仅更新对应 Chunk
- [ ] 支持删除文件后同步删除向量数据
- [ ] 记录最后入库时间

## V3.2 版本管理

Metadata 正式加入：

```text
document_id
version
status
effective_date
expire_date
updated_at
```

- [ ] 同一文档生成稳定 `document_id`
- [ ] 新版本自动增加 version
- [ ] 当前版本 `status=active`
- [ ] 历史版本 `status=expired`
- [ ] 默认查询 active
- [ ] 支持查询历史版本
- [ ] 支持按时间匹配历史内容
- [ ] 支持版本来源追溯
- [ ] UI 展示版本信息

## V3.3 自动 Metadata

- [ ] 自动生成 title
- [ ] 自动识别 category
- [ ] 自动识别 topic
- [ ] 自动生成 tags
- [ ] 自动提取日期
- [ ] 人工可修改 Metadata
- [ ] 自动 Metadata 必须可追溯/可编辑

## V3.4 知识管理

- [ ] 文件列表
- [ ] 查看文件状态
- [ ] 查看 Chunk 数
- [ ] 查看更新时间
- [ ] 查看版本
- [ ] 手动重新入库
- [ ] 删除知识
- [ ] Metadata 编辑
- [ ] 按分类筛选知识

## V3.5 引用体验

- [ ] 回答展示引用编号
- [ ] 展示文件名
- [ ] 展示章节
- [ ] 展示页码
- [ ] 展示 Chunk 原文
- [ ] 支持点击展开来源
- [ ] 支持复制来源内容

## V3.6 长期知识使用

- [ ] 支持工作/学习/生活分类
- [ ] 支持时间过滤
- [ ] 支持 Topic 过滤
- [ ] 支持“我过去是怎么想的”类查询
- [ ] 支持历史版本对比
- [ ] 支持按项目查询
- [ ] 支持按年份查询
- [ ] 支持知识总结
- [ ] 支持跨文档总结

## V3.7 完整评测体系

至少覆盖：

- [ ] 单文档事实问题
- [ ] 跨文档问题
- [ ] 模糊表达
- [ ] 同义表达
- [ ] 专有名词
- [ ] 时间查询
- [ ] 历史版本查询
- [ ] 无答案问题
- [ ] 多来源冲突
- [ ] 引用准确性
- [ ] 幻觉检查
- [ ] 响应时间
- [ ] API 成本

## V3 完成标准

- [ ] 知识库可以长期增量维护
- [ ] 修改文件无需全量重建
- [ ] 版本管理可用
- [ ] 支持时间/版本查询
- [ ] 引用可追溯
- [ ] 有稳定评测集
- [ ] 日常工作/学习中可以真实使用

---

# 当前开发状态

## 当前版本

**V1：基础 RAG**

## 当前阶段

**V1 已于 2026-08-29 开发完成并通过端到端验收**（LLM 已配置 DeepSeek deepseek-v4-flash）。首轮评测：检索命中率 10/10、回答有据率 10/10、无答案拒答率 2/2，报告存于 `storage/`。

唯一遗留：用自己的真实知识出 20+ 道题，替换/扩充 `eval_set.json` 再跑一轮人工评测，即可正式进入 V2。

## 当前第一个里程碑

~~先完成：项目初始化 → Parser → Cleaner → Chunker~~（已完成）

~~此时暂时不要急着接 Qdrant。~~（已接入并验证）

---

# Vibe Coding 使用原则

每完成一个节点，都做三件事：

- [ ] 代码能运行
- [ ] 能看到该节点的输入和输出
- [ ] 自己能解释这一节点在 RAG 中解决什么问题

不要只以“程序没报错”为完成标准。

例如 Chunk 节点完成，不是：

> 代码成功生成了 100 个 Chunk

而应该是：

> 我能看到原文是怎么被切开的；
> 我知道 Chunk Size 和 Overlap 是多少；
> 我能判断是否出现语义断裂；
> 我知道 Metadata 有没有正确继承。

这样才能真正达到“通过做项目学习 RAG”的目标。

---

# 开发进度记录

## V1

- [x] V1.1 项目初始化
- [x] V1.2 文档解析
- [x] V1.3 清洗与结构化
- [x] V1.4 Chunk
- [x] V1.5 Embedding
- [x] V1.6 Qdrant
- [x] V1.7 Query Embedding
- [x] V1.8 Vector Retrieval
- [x] V1.9 Context Assembly
- [x] V1.10 Prompt
- [x] V1.11 LLM API
- [x] V1.12 引用
- [x] V1.13 Streamlit
- [x] V1.14 基础评测（首轮 12 题：检索 10/10、有据 10/10、拒答 2/2；待用自己的知识扩到 20+ 题）

## V2

- [ ] V2.1 Query Understanding / Rewrite
- [ ] V2.2 Metadata Filter
- [ ] V2.3 BM25 / 全文检索
- [ ] V2.4 Hybrid Search
- [ ] V2.5 Rerank
- [ ] V2.6 Context 优化
- [ ] V2.7 V2 评测

## V3

- [ ] V3.1 增量入库
- [ ] V3.2 版本管理
- [ ] V3.3 自动 Metadata
- [ ] V3.4 知识管理
- [ ] V3.5 引用体验
- [ ] V3.6 长期知识使用
- [ ] V3.7 完整评测体系
