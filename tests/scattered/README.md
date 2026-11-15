# tests/scattered —— 手工自测脚本（不参与 pytest）

这些是从 `_weekly/`、`_session_backup/` 收拢过来的**即写即跑的 ad-hoc 自测脚本**：

- 直接 `python 文件名` 运行，靠 print 看结果；
- 不是 pytest 用例（没有 test_* 函数），已在 `pytest.ini` 的 `norecursedirs` 里排除；
- 多数脚本里 `sys.path.insert` 写死了项目根，移动位置不影响运行。
