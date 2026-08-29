# Personal RAG — 知识库管理开发计划

## 1. 开发目标

当前 RAG V1 已经完成基础链路：

```text
文件解析
→ 清洗
→ Chunk
→ Embedding
→ Qdrant
→ 向量检索
→ Context
→ Prompt
→ LLM
→ 返回答案
```

本阶段目标不是重新设计 RAG，而是在现有 V1 基础上增加一套规范的个人知识库管理机制。

最终架构确定为：

```text
一个主知识库
+
一个 Qdrant Collection
+
多级本地目录
+
Metadata 逻辑分类
```

原则：

```text
目录负责：文件怎么管理

Metadata负责：知识怎么分类

Qdrant负责：知识怎么统一检索

RAG负责：知识怎么被使用
```

---

# 2. 当前阶段状态

当前开发阶段：

**Knowledge Management V1**

状态：

- [x] 已完成（2026-08-29，测试 9/9 通过，评测三项 100%）

完成本阶段后，系统应该支持：

- [x] 所有知识统一存放在一个 `knowledge/` 主目录
- [x] 文件可以按照 work / learning / life / reference 分类
- [x] 系统可以根据目录自动生成部分 Metadata
- [x] Chunk 自动继承 Document Metadata
- [x] 所有 Chunk 存入同一个 Qdrant Collection
- [x] 可以按照 Metadata 过滤知识
- [x] 默认只检索 `status=active` 的知识
- [x] 可以看到每个 Chunk 来源于哪个文件、目录、章节
- [x] 后续 V2/V3 不需要重新设计数据结构

---

# 3. 本地知识目录设计

项目新增：

```text
knowledge/
```

完整目录：

```text
knowledge/
│
├── work/
│   ├── projects/
│   ├── product/
│   ├── management/
│   ├── interview/
│   └── experience/
│
├── learning/
│   ├── ai/
│   │   ├── rag/
│   │   ├── agent/
│   │   ├── llm/
│   │   └── prompt/
│   │
│   ├── product/
│   ├── technology/
│   └── reading/
│
├── life/
│   ├── thinking/
│   ├── experience/
│   ├── travel/
│   └── notes/
│
├── reference/
│   ├── books/
│   ├── articles/
│   └── documents/
│
└── archive/
```

---

# 4. 目录含义

一级目录固定为：

```text
work
learning
life
reference
archive
```

对应 Metadata：

```text
domain
```

例如：

```text
knowledge/work/projects/demo.md
```

自动得到：

```json
{
  "domain": "work",
  "category": "projects"
}
```

例如：

```text
knowledge/learning/ai/rag/rag-notes.md
```

得到：

```json
{
  "domain": "learning",
  "category": "ai"
}
```

注意：

`rag` 不直接作为 category。

它可以作为：

```text
topic = RAG
```

---

# 5. Domain 规范

Domain 必须使用固定枚举：

```text
work
learning
life
reference
archive
```

禁止自动产生：

```text
works
study
学习
工作
personal
```

等其他值。

系统内部统一使用英文。

UI 可以显示中文。

例如：

```text
work → 工作
learning → 学习
life → 生活
reference → 参考资料
archive → 归档
```

---

# 6. Category 规范

Category 来源于：

```text
knowledge/<domain>/<category>/
```

例如：

```text
knowledge/work/projects/
```

得到：

```json
{
  "domain": "work",
  "category": "projects"
}
```

例如：

```text
knowledge/learning/ai/
```

得到：

```json
{
  "domain": "learning",
  "category": "ai"
}
```

Category 暂时允许自由目录名。

但是：

- [ ] 自动转换成小写
- [ ] 自动去除首尾空格
- [ ] 空值设置为 `general`

---

# 7. Topic 设计

Topic 用于表达：

> 这篇知识主要讲什么。

例如：

```text
RAG
Agent
LLM
AI产品经理
智能客服
项目管理
```

Topic 不完全依赖目录。

第一阶段支持两种来源：

### 方式一：根据目录推断

例如：

```text
knowledge/learning/ai/rag/demo.md
```

可以得到：

```json
{
  "topic": ["RAG"]
}
```

### 方式二：Markdown Front Matter

如果文件本身声明：

```markdown
---
topic:
  - RAG
  - 向量数据库
---
```

则 Front Matter 优先。

优先级：

```text
Front Matter
>
目录推断
>
空值
```

---

# 8. Tags 设计

Tags 用来描述更细粒度的知识概念。

例如：

```json
{
  "topic": ["RAG"],
  "tags": [
    "Chunk",
    "Embedding",
    "Metadata",
    "Rerank"
  ]
}
```

第一阶段：

- [ ] 支持从 Markdown Front Matter 读取
- [ ] 非 Markdown 文件默认 `tags=[]`
- [ ] 暂时不调用 LLM 自动生成 Tags

LLM 自动标签放到后续版本。

---

# 9. Metadata 标准 Schema

所有 Document 和 Chunk 统一使用以下 Metadata Schema：

```json
{
  "document_id": "",
  "chunk_id": "",

  "source": "",
  "path": "",
  "file_type": "",

  "title": "",
  "section": "",
  "section_path": "",

  "domain": "",
  "category": "",

  "topic": [],
  "tags": [],

  "version": "1.0",
  "status": "active",

  "created_at": null,
  "updated_at": null,
  "indexed_at": null,

  "page": null
}
```

---

# 10. 必填 Metadata

以下字段必须存在：

```text
document_id
chunk_id
source
path
file_type
domain
category
version
status
indexed_at
```

即使无法解析，也不能缺失字段。

例如：

```json
{
  "title": null,
  "section": null,
  "page": null
}
```

而不是直接没有字段。

---

# 11. document_id 设计

要求：

同一个文件，在没有重建身份的情况下：

```text
document_id 必须稳定
```

推荐根据：

```text
relative_path
```

生成。

例如：

```text
learning/ai/rag/rag学习笔记.md
```

生成：

```text
doc_<hash>
```

例如：

```text
doc_a913bc12
```

要求：

- [ ] 同一路径重新入库 document_id 不变
- [ ] 不使用随机 UUID 作为唯一逻辑身份
- [ ] 文件改名后可以视为新文档，V3 再解决迁移问题

---

# 12. chunk_id 设计

每个 Chunk：

```text
document_id + chunk_index
```

例如：

```text
doc_a913bc12_0001
doc_a913bc12_0002
doc_a913bc12_0003
```

要求：

- [ ] Chunk ID 可读
- [ ] Chunk ID 稳定
- [ ] 方便删除整篇文档
- [ ] 方便排查 Chunk

---

# 13. Version 设计

当前阶段：

默认：

```json
{
  "version": "1.0"
}
```

必须存储，但暂时不自动升级。

当前版本只实现：

- [ ] version 字段存在
- [ ] 默认值 1.0
- [ ] 可以通过 Front Matter 手动指定

例如：

```markdown
---
version: "1.2"
---
```

真正自动版本管理放 V3。

---

# 14. Status 设计

当前支持：

```text
active
archive
```

默认：

```text
active
```

规则：

普通文件：

```text
status = active
```

如果文件位于：

```text
knowledge/archive/
```

则：

```text
status = archive
```

后续再扩展：

```text
draft
expired
deleted
```

当前不实现。

---

# 15. 默认检索规则

所有正常 RAG 查询：

默认增加过滤条件：

```text
status = active
```

也就是说：

```text
archive
```

内容默认不会参与回答。

以后用户明确要求：

```text
查询归档内容
```

再允许：

```text
status = archive
```

---

# 16. Markdown Front Matter

支持 Markdown 文件使用：

```markdown
---
title: RAG学习笔记

domain: learning

category: ai

topic:
  - RAG
  - 向量数据库

tags:
  - Chunk
  - Embedding
  - Metadata

version: "1.0"

status: active
---

# RAG

正文……
```

读取优先级：

```text
Front Matter
>
目录自动解析
>
系统默认值
```

例如目录：

```text
learning/ai/rag/
```

自动：

```text
domain=learning
category=ai
topic=RAG
```

但是 Front Matter：

```yaml
topic:
  - RAG
  - AI产品
```

则最终：

```json
{
  "topic": [
    "RAG",
    "AI产品"
  ]
}
```

---

# 17. 非 Markdown 文件

支持：

```text
txt
rtf
html
docx
pdf
```

这些文件没有 Front Matter。

Metadata 来源：

```text
文件路径
+
文件信息
+
Parser解析结果
```

例如：

```text
knowledge/work/projects/智能客服复盘.pdf
```

自动生成：

```json
{
  "source": "智能客服复盘.pdf",
  "domain": "work",
  "category": "projects",
  "file_type": "pdf",
  "version": "1.0",
  "status": "active"
}
```

Topic 暂时允许：

```json
{
  "topic": []
}
```

后续 V3 再自动识别。

---

# 18. Title 获取规则

优先级：

```text
Front Matter title
>
Markdown H1
>
Word/PDF解析标题
>
文件名
```

例如：

```text
RAG学习笔记.md
```

没有标题信息：

```json
{
  "title": "RAG学习笔记"
}
```

---

# 19. Section 获取规则

Chunk 必须尽量知道自己属于哪个章节。

Markdown：

```markdown
# RAG

## Metadata

### Metadata Filter
```

生成：

```json
{
  "section": "Metadata Filter",
  "section_path": "RAG > Metadata > Metadata Filter"
}
```

Word/PDF：

如果 Parser 能识别：

```text
Heading 1
Heading 2
```

同样保存。

无法识别：

```json
{
  "section": null,
  "section_path": null
}
```

---

# 20. Chunk 继承 Metadata

Document：

```json
{
  "document_id": "doc_001",
  "domain": "learning",
  "category": "ai",
  "topic": ["RAG"],
  "version": "1.0",
  "status": "active"
}
```

切成 Chunk 后：

每个 Chunk 必须完整继承。

Chunk 只额外增加：

```text
chunk_id
section
section_path
page
```

以及：

```text
text
```

---

# 21. Qdrant Collection

只创建一个：

```text
personal_knowledge
```

禁止创建：

```text
work_collection
learning_collection
life_collection
```

当前所有个人知识统一进入：

```text
personal_knowledge
```

---

# 22. Qdrant Point 结构

每一个 Chunk 对应一个 Point。

结构：

```json
{
  "id": "doc_xxx_0001",

  "vector": [],

  "payload": {
    "text": "",

    "document_id": "",
    "chunk_id": "",

    "source": "",
    "path": "",

    "file_type": "",

    "title": "",
    "section": "",
    "section_path": "",

    "domain": "",
    "category": "",

    "topic": [],
    "tags": [],

    "version": "1.0",
    "status": "active",

    "created_at": null,
    "updated_at": null,
    "indexed_at": "",

    "page": null
  }
}
```

---

# 23. Qdrant Payload Index

为以后 Metadata Filter 做准备。

建议为这些字段创建 Payload Index：

```text
document_id
source
domain
category
topic
tags
status
version
file_type
```

时间字段后续需要时再增加。

---

# 24. 入库流程

完整入库流程：

```text
扫描 knowledge/
↓
找到支持的文件
↓
计算 relative_path
↓
解析目录
↓
生成基础 Metadata
↓
Parser 解析正文
↓
如果 Markdown：
读取 Front Matter
↓
Metadata Merge
↓
Cleaner
↓
Chunk
↓
Chunk 继承 Metadata
↓
Embedding
↓
写入 personal_knowledge
```

---

# 25. Metadata Merge 规则

优先级必须明确。

```text
系统强制字段
>
Front Matter
>
目录推断
>
默认值
```

系统强制字段包括：

```text
document_id
chunk_id
source
path
file_type
indexed_at
```

Front Matter 不能修改这些字段。

例如：

用户 Front Matter 写：

```yaml
document_id: abc
```

系统必须忽略。

---

# 26. 默认值

缺失字段：

```json
{
  "domain": "reference",
  "category": "general",
  "topic": [],
  "tags": [],
  "version": "1.0",
  "status": "active"
}
```

但：

如果文件位于：

```text
archive/
```

强制：

```text
status = archive
```

---

# 27. 检索流程调整

当前 V1 查询：

```text
Query
→ Embedding
→ Qdrant Search
```

修改成：

```text
Query
→ Embedding
→ Default Filter
→ Qdrant Search
```

默认：

```text
status = active
```

---

# 28. Metadata Filter API

Retriever 增加：

```python
search(
    query,
    filters=None,
    top_k=5
)
```

例如：

```python
filters = {
    "domain": "learning",
    "topic": "RAG",
    "status": "active"
}
```

系统转换成 Qdrant Filter。

---

# 29. 当前阶段不做 Query Understanding

注意：

本阶段虽然支持 Metadata Filter，

但是：

```text
暂时不让 LLM 自动理解用户条件
```

例如：

用户输入：

> 只查我的学习笔记

当前版本不自动转换。

先通过：

```text
手动 Filter
```

验证 Metadata Filter 机制正确。

Query Understanding 属于 V2。

---

# 30. 开发调试界面

Streamlit 增加 Debug 区域。

显示：

```text
Query

使用的 Metadata Filter

Top K

召回 Chunk

Chunk Score

Source

Domain

Category

Topic

Version

Status
```

目标：

开发过程中能够清楚知道：

> 为什么这条知识被召回？

---

# 31. 知识文件列表

增加一个简单 Knowledge 页面。

展示：

| 文件 | Domain | Category | Version | Status |
|---|---|---|---|---|

至少支持：

- [ ] 查看所有入库文件
- [ ] 查看文件路径
- [ ] 查看 Domain
- [ ] 查看 Category
- [ ] 查看 Version
- [ ] 查看 Status
- [ ] 查看 Chunk 数量

当前阶段只读即可。

编辑功能 V3 再做。

---

# 32. 测试数据

准备以下测试文件：

```text
knowledge/

work/projects/智能客服复盘.md

work/interview/AI产品经理面试.md

learning/ai/rag/RAG学习笔记.md

learning/ai/agent/Agent学习笔记.md

life/thinking/个人思考.md

reference/articles/RAG外部资料.pdf

archive/旧版RAG笔记.md
```

---

# 33. 必测场景

## 场景一

搜索：

```text
RAG Metadata 是什么？
```

不加 Domain Filter。

预期：

```text
learning
work
reference
```

均有可能被召回。

---

## 场景二

Filter：

```text
domain = learning
```

搜索：

```text
RAG Metadata
```

预期：

只返回：

```text
learning
```

---

## 场景三

Filter：

```text
domain = work
```

搜索：

```text
RAG
```

预期：

只返回：

```text
work
```

---

## 场景四

默认搜索：

```text
RAG
```

预期：

```text
status=archive
```

的数据不参与检索。

---

## 场景五

Filter：

```text
status = archive
```

搜索：

```text
RAG
```

预期：

可以找到旧版归档知识。

---

# 34. 开发任务 Checklist

## A. 目录系统

- [x] 创建 knowledge 主目录规范
- [x] 支持递归扫描目录
- [x] 识别 domain
- [x] 识别 category
- [x] 支持 archive 特殊规则
- [x] 保存 relative_path

---

## B. Metadata Schema

- [x] 创建统一 Metadata Model
- [x] 添加 document_id
- [x] 添加 chunk_id
- [x] 添加 source
- [x] 添加 path
- [x] 添加 file_type
- [x] 添加 title
- [x] 添加 section
- [x] 添加 section_path
- [x] 添加 domain
- [x] 添加 category
- [x] 添加 topic
- [x] 添加 tags
- [x] 添加 version
- [x] 添加 status
- [x] 添加 created_at
- [x] 添加 updated_at
- [x] 添加 indexed_at
- [x] 添加 page

---

## C. Markdown Front Matter

- [x] 支持 YAML Front Matter
- [x] 解析 title
- [x] 解析 domain
- [x] 解析 category
- [x] 解析 topic
- [x] 解析 tags
- [x] 解析 version
- [x] 解析 status
- [x] 实现 Metadata 优先级
- [x] 防止 Front Matter 修改系统字段

---

## D. Document ID

- [x] 根据 relative_path 生成稳定 document_id
- [x] 验证重复入库 ID 不变化
- [x] 建立测试用例（scripts/test_km_scenarios.py）

---

## E. Chunk Metadata

- [x] Chunk 完整继承 Document Metadata
- [x] 生成稳定 chunk_id
- [x] 保存 section
- [x] 保存 section_path
- [x] 保存 page
- [x] Debug 打印 Chunk Metadata

---

## F. Qdrant

- [x] Collection 固定 personal_knowledge
- [x] 修改 Point Payload
- [x] 写入完整 Metadata
- [x] 创建 Payload Index
- [x] 检查 Dashboard 数据
- [x] 支持根据 document_id 删除数据

---

## G. Metadata Filter

- [x] Retriever 支持 filters 参数
- [x] 支持 domain
- [x] 支持 category
- [x] 支持 topic
- [x] 支持 tags
- [x] 支持 version
- [x] 支持 status
- [x] 支持 source
- [x] 默认 status=active
- [x] Filter 为空时正常搜索

---

## H. Debug UI

- [x] 显示查询语句
- [x] 显示 Filter
- [x] 显示召回 Chunk
- [x] 显示 Score
- [x] 显示 Source
- [x] 显示 Domain
- [x] 显示 Category
- [x] 显示 Topic
- [x] 显示 Version
- [x] 显示 Status

---

## I. Knowledge 页面

- [x] 展示所有知识文件
- [x] 展示路径
- [x] 展示 Domain
- [x] 展示 Category
- [x] 展示 Topic
- [x] 展示 Version
- [x] 展示 Status
- [x] 展示 Chunk 数量

---

# 35. 本阶段完成标准

以下全部满足，本阶段才算完成：

- [x] 所有知识统一进入 `knowledge/`
- [x] 一个 Qdrant Collection：`personal_knowledge`
- [x] 文件夹可以自动生成 Domain / Category
- [x] Markdown 支持 Front Matter
- [x] Metadata Schema 统一
- [x] Document → Chunk Metadata 正确继承
- [x] Qdrant Payload 保存完整 Metadata
- [x] 默认搜索只查 active
- [x] 支持手动 Metadata Filter
- [x] archive 默认不参与普通搜索
- [x] Debug 页面可以看到 Metadata
- [x] Knowledge 页面可以查看文件状态
- [x] 至少完成 7 个测试文件的入库测试（实际 12 个：7 个计划测试文件 + 5 个迁移示例）
- [x] 至少完成 5 个 Metadata Filter 测试场景（scripts/test_km_scenarios.py，9/9 通过）

---

# 36. 当前不要开发的内容

本阶段禁止提前开发：

```text
LLM 自动分类
LLM 自动 Tags
Query Understanding
Query Rewrite
BM25
Hybrid Search
Rerank
自动版本升级
历史版本查询
文件自动监听
复杂知识管理后台
```

这些属于后续版本。

当前目标只有一个：

> 把知识的“存储结构、目录结构、Metadata结构和检索边界”打牢。

---

# 37. 开发顺序

严格按照以下顺序：

```text
Step 1
目录扫描

↓

Step 2
Metadata Schema

↓

Step 3
路径 → Domain / Category

↓

Step 4
Front Matter

↓

Step 5
Document ID / Chunk ID

↓

Step 6
Chunk Metadata 继承

↓

Step 7
Qdrant Payload 改造

↓

Step 8
Metadata Filter

↓

Step 9
Debug UI

↓

Step 10
Knowledge 页面

↓

Step 11
测试
```

不要一次性全部修改。

每完成一个 Step：

- [ ] 运行测试
- [ ] 打印输入
- [ ] 打印输出
- [ ] 确认数据结构
- [ ] 再进入下一步

---

# 38. 最终目标数据流

```text
knowledge/
│
├── work/
├── learning/
├── life/
├── reference/
└── archive/

       ↓

Parser

       ↓

Document
+
Metadata

       ↓

Cleaner

       ↓

Chunk
+
Metadata

       ↓

Embedding

       ↓

Qdrant

Collection:
personal_knowledge

       ↓

Metadata Filter
+
Vector Search

       ↓

Top K Chunk

       ↓

RAG
```

最终原则：

> 一个主知识库，一个 Qdrant Collection。

> 文件目录解决物理管理。

> Metadata 解决逻辑知识分类。

> Filter 解决检索范围。

> Vector Search 解决语义匹配。