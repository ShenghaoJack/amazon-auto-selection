# tools/report_rules —— vendored 数值规则

| 项 | 值 |
|---|---|
| 来源 | `~/.codex/skills/build-amazon-integrated-html-report/scripts/analysis_rules.py` |
| 同步时间 | 2026-10-12 |
| 源 sha256 | `2e7006e6d867f018105d36a2e013cf4d7c35997b27491fa0cda311eb942ec301` |
| 自检 | `python tools/report_rules/analysis_rules.py --self-test`(11 项, 全过) |

**用途**: 关键词四条准入、双榜 ±8pp、Google 同尺度季节门槛(最近三个完整年)、相对变化、VOC 分项打分
(正/负向 1–5 分量表 × 权重 × 20 → 0–100; 安全等级 A/B 触发优先级下限 P1)。

**重新同步步骤**: ①复制技能源文件到本目录 ②更新头部注释与上表 sha256 ③跑自检 ④跑 `python -m pytest tests/test_report_rules.py -q`(内含与源文件的一致性校验)。
