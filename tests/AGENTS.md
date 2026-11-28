# 测试数据隔离

- 入库、检索和管理测试必须继承 `tests._helpers.IngestHarness`，并调用 `use_fakes()`；认证测试使用 `TempAuthDB`。
- Streamlit AppTest 必须使用 `isolate_app_settings()`；所有知识、台账、知识板块、认证数据均放在系统临时目录，Qdrant 使用内存模式，向量化使用 FakeEmbedding。
- 不得对 `get_config()` 返回的缓存配置原地修改；使用 `dataclasses.replace` 构造独立配置。
- 不得在测试里连接正式服务、启动外部命令、运行 `ingest.py --rebuild` 或修改正式 `storage/`、`knowledge/`、`settings.json`。不得关闭或绕过 `tests._safety` 保护。
- 标准命令：`.venv/bin/python -m unittest discover -s tests -t . -v`。`scripts/test_km_scenarios.py` 是读取现有库的手动诊断，不属于自动化回归入口。
