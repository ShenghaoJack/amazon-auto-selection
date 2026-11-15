# -*- coding: utf-8 -*-
"""验证新版重判提示词：字段说明 + 用户要求 + 唯一值参考 + 格式，且 hint 带 % 不报错。"""
import json
import sys
sys.stdout.reconfigure(encoding="utf-8")

CAT = r"D:\deepseek工作区\auto-workflow-python\data\Dehumidifiers-US-202609"
spec = json.load(open(CAT + r"\Dehumidifiers-US-202609-attribute-spec.json", encoding="utf-8-sig"))
hint = ""
for a in spec.get("attributes", []):
    if a.get("name") == "产品类型":
        hint = str(a.get("hint") or a.get("hint_short") or "")
        break
uniq = ["悬挂式吸湿袋", "硅胶干燥剂包", "除湿机", "小型除湿机", "家用除湿机", "干燥棒", "商用除湿机", "半导体除湿机"]
req = "按技术路线重新命名产品类型，不要用小型/家用/商用这类场景名"
attr = "产品类型"
lines = ['{"xl_row": 2, "标题": "xxx"}', '{"xl_row": 3, "标题": "yyy"}']

_rule = ("请逐行判断该属性的值。\n"
         "【字段说明】" + (hint or "（该属性没有判定说明，按属性名的语义判断）") + "\n"
         + ("【用户的命名要求】" + req + "\n" if req else "")
         + ("【当前唯一值（供参考，**不是限定范围**）】" + "、".join(uniq) + "\n" if uniq else "")
         + "按上面说明判断后直接写出该属性的值；判断不出写「未明确」。\n")
prompt = ("你是商品属性「" + attr + "」的复核标注器。下面是 2 行商品**文本**资料"
          "（标题/五点描述/详细参数）。\n" + _rule
          + "严格输出 JSON：{\"rows\":[{\"xl_row\":行号,\"值\":\"...\","
            "\"依据\":\"原文摘录(≤30字)\",\"来源\":\"标题/五点描述/详细参数\"}]}，"
            "不要输出其它文字。\n\n商品资料：\n" + "\n".join(lines))

print(prompt)
print("\n=== OK，长度", len(prompt), "| 含 唯一值:", "当前唯一值" in prompt, "| 含 用户要求:", "用户的命名要求" in prompt)
