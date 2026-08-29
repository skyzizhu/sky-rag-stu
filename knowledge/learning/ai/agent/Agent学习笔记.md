---
topic:
  - Agent
  - RAG
tags:
  - ReAct
  - ToolUse
---

# Agent 学习笔记

## 什么是 Agent

Agent（智能体）是能自主感知环境、拆解任务、调用工具并完成目标的 AI 系统。
和普通对话机器人最大的区别：Agent 会自己规划步骤，而不只是回答一句话。

## Agent 的三大组件

规划（Planning）：把大目标拆成可执行的小步骤。
记忆（Memory）：短期记忆是当前任务的上下文，长期记忆通常靠外部知识库。
工具使用（Tool Use）：调用搜索、代码执行、API 等外部能力。

## ReAct 模式

ReAct = Reasoning + Acting，即「思考 → 行动 → 观察」的循环。
模型先想一步该做什么，再执行动作，然后观察结果，决定下一步。
这是目前最常用的 Agent 工作模式。

## Agent 和 RAG 的关系

RAG 可以看作 Agent 的一种工具：当 Agent 需要外部知识时，
调用知识库检索来补全上下文。我的个人知识库项目就是给未来的 Agent 打基础。
