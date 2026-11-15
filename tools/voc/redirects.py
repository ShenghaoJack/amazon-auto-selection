"""Terminal ASIN redirect notices, distinct from retryable network failures."""
import json
import re

from core.mcp import MCPBusinessError


class ASINRedirected(MCPBusinessError):
    def __init__(self, tool, notice, payload=None):
        self.notice = notice
        super().__init__(tool, "ASIN_REDIRECTED", "[ASIN_REDIRECTED] ASIN已重定向至%s；原查询不自动重试，评论归属未被替换"
                         % notice["destination_asin"], payload)


def notice_from_error(error, source, asin, site):
    if isinstance(error, MCPBusinessError):
        value = {"message": error.message, "payload": error.payload}
    else:
        value = error
    text = json.dumps(value, ensure_ascii=False, default=str)
    match = re.search(r"\[ASIN_REDIRECTED\].*?(?:Redirected\s+to\s+ASIN\s*|ASIN\s*[:：]\s*)([A-Z0-9]{10})\b", text, re.I)
    if not match:
        return None
    return {"source": source, "queried_asin": str(asin or "").strip().upper(), "site": site,
            "destination_asin": match.group(1).upper(), "billed_notice": "billed normally" in text.lower(),
            "billing_amount": None, "action": "不重试原ASIN，不自动查询跳转目标；按步骤18范围核查",
            "message": text[:2500]}
