<!-- prd-status: provided -->
<!-- prd-reason: 项目内权威文档与外部发布链接 -->

# 附录与参考资料

## 项目内权威文档

| 文档 | 内容 |
|---|---|
| [README](https://github.com/skyzizhu/sky-rag-stu#readme) | 安装、使用、输入源说明 |
| prd.md（仓库根） | V1~V3.1 完整产品设计（含流水线节点定义、评测机制） |
| realize.md（仓库根） | 节点级实现学习笔记（入库/问答/评测三部分） |
| rag_pitfalls.md（仓库根） | 两条流水线易错问题复盘手册（33 条） |

## 外部链接

- GitHub 仓库：https://github.com/skyzizhu/sky-rag-stu
- V3.1 Release：https://github.com/skyzizhu/sky-rag-stu/releases/tag/v3.1
- 评测脚本：仓库根 `eval.py`（基础卷）、`eval_compare.py`（困难卷对比）

## 术语表

| 术语 | 含义 |
|---|---|
| 知识卡片 | 一个文本切片及其完整元数据，检索的最小单元 |
| 台账 | index_state.json，记录每个文件的入库指纹，增量判断依据 |
| 领域（domain） | 知识一级分类，六枚举：工作/学习/生活/项目/参考资料/归档 |
| Rerank | 用 LLM 对召回候选按相关度重排序，只排序不作过滤 |
