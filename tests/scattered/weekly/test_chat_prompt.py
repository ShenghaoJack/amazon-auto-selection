# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"D:\deepseek工作区\auto-workflow-python")
from tools.attr_stability.onboard import build_vocab_chat_prompt as f
p = f("再生方式", [("微波加热", 10)], None, ["微波加热"],
      None, {"产品类型": ["小型除湿机"]},
      [{"标题": "测试标题", "五点描述": "五点内容", "详细参数": "参数内容"}])
print(p[:900])
