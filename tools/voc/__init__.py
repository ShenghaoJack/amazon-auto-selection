"""VOC(评论之声)分析复用模块。

从 `_voc_test/` 测试脚本中抽出的**纯逻辑**(无网络/无 LLM 依赖之外的副作用), 供:
  · 测试环境 `_voc_test/*` 编排调用;
  · 将来正式接入流水线(steps/step19_voc.py + tools/cat_report/voc_reader.py)。

模块划分:
  · taxonomy  —— 属性/证据角色/严重度/安全等级/规范问题与场景清单(对齐 skill)
  · normalize —— 两源(Pangolinfo / 卖家精灵)字段归一与去重键
  · dedupe    —— 两级索引去重(主键 reviewId + 兜底键), 跨源合并与来源溯源
  · scoring   —— 正向驱动分 / 负向优先级分 / 安全下限规则
  · acquire   —— 采集页数计划与"额度耗尽"判定
"""
