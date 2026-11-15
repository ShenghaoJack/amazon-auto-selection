# -*- coding: utf-8 -*-
"""验证：属性 hint 里带 % 时，新的「字符串拼接」提示词不会再抛 TypeError。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

attr = "产品类型"
hint = "覆盖率≥60% 且 power>100W 写 压缩机除湿机；占比 50% 的写 半导体除湿机"
parts = [{"xl_row": 2}, {"xl_row": 3}]
lines = ['{"xl_row": %d}' % p["xl_row"] for p in parts]

_rule = ("请逐行判断该属性的值，**按下面这段判定说明**判断：\n"
         "【判定说明】" + (hint or "（该属性没有判定说明，按属性名的语义判断）")
         + "\n**不要限定在固定列表**，直接写出你判断的值；判断不出写「未明确」。\n")
prompt = ("你是商品属性「" + attr + "」的复核标注器。下面是 " + str(len(parts))
          + " 行商品**文本**资料（标题/五点描述/详细参数）。\n" + _rule
          + "严格输出 JSON：{\"rows\":[{\"xl_row\":行号,\"值\":\"...\"}]}，"
            "不要输出其它文字。\n\n商品资料：\n" + "\n".join(lines))
print("新写法 OK, prompt 长度 =", len(prompt))
print("含 60% :", "60%" in prompt)

# 旧写法对照：会抛 TypeError
old = ("请按 %s 判断：\n%s\n%s" % (attr, hint, "\n".join(lines)))
print("旧写法结果:", old[:20])
