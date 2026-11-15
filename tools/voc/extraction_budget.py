"""Bounded VOC inputs without assuming a provider-specific tokenizer.

UTF-8 bytes are a deliberately conservative token estimate, not a tokenizer
measurement or a statement of the selected model's context capacity.
"""
from __future__ import annotations

from dataclasses import dataclass
import json


def estimate_tokens(text):
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class ExtractionBudget:
    input_tokens: int = 24000
    context_tokens: int = 65536
    output_tokens: int = 16000
    window_chars: int = 10000
    overlap_chars: int = 1000
    split_depth: int = 6
    min_split_chars: int = 512
    # Reserve both corrective feedback and protocol overhead before packing.
    feedback_tokens: int = 2048
    envelope_tokens: int = 512

    @classmethod
    def from_config(cls, config):
        result = cls(**{field: int(config.get(key, default)) for field, key, default in (
            ("input_tokens", "VOC_EXTRACT_INPUT_TOKENS", 24000),
            ("context_tokens", "VOC_EXTRACT_CONTEXT_TOKENS", 65536),
            ("output_tokens", "VOC_EXTRACT_OUTPUT_TOKENS", 16000),
            ("window_chars", "VOC_EXTRACT_WINDOW_CHARS", 10000),
            ("overlap_chars", "VOC_EXTRACT_OVERLAP_CHARS", 1000),
            ("split_depth", "VOC_EXTRACT_SPLIT_DEPTH", 6),
        )})
        if (result.input_tokens < 4096 or result.output_tokens < 1
                or result.context_tokens < result.input_tokens + result.output_tokens + result.envelope_tokens
                or not 0 <= result.overlap_chars < result.window_chars
                or result.window_chars < 1024 or not 0 <= result.split_depth <= 12):
            raise ValueError("VOC提取预算无效：输入/输出/协议预留须容纳于上下文预算，窗口须大于重叠")
        return result

    def prompt(self, instruction, batch, feedback=""):
        # Bound feedback in bytes too, including multibyte exception messages.
        correction = ("上次校验错误：" + feedback + "。逐字复制原句并返回所有输入ID。\n") if feedback else ""
        if estimate_tokens(correction) > self.feedback_tokens:
            correction = correction.encode("utf-8")[:self.feedback_tokens - 1].decode("utf-8", errors="ignore") + "\n"
        return instruction + correction + json.dumps(batch, ensure_ascii=False)

    def fits(self, instruction, batch):
        return (estimate_tokens(self.prompt(instruction, batch)) + self.feedback_tokens
                <= self.input_tokens)

    def split(self, part, *, minimum=None):
        """Return overlapping source slices with exact Unicode codepoint offsets."""
        field = "text" if len(part["text"]) >= len(part["title"]) else "title"
        text = part[field]
        if len(text) < (minimum or self.min_split_chars) * 2:
            return []
        midpoint = len(text) // 2
        overlap = min(self.overlap_chars, len(text) // 4)
        bounds = [(0, midpoint + overlap // 2), (midpoint - overlap // 2, len(text))]
        offset_key = "offset" if field == "text" else "title_offset"
        return [dict(part, **{field: text[start:end], offset_key: part.get(offset_key, 0) + start,
                              "part_id": f"{part['part_id']}/{field}:{start}:{end}"})
                for start, end in bounds]

    def fit_parts(self, instruction, parts):
        pending, result = list(parts), []
        while pending:
            part = pending.pop(0)
            if self.fits(instruction, [part]):
                result.append(part)
                continue
            children = self.split(part, minimum=1)
            if not children:
                raise ValueError("单片段元数据已超过VOC输入预算，请提高预算；未截断评论")
            pending[0:0] = children
        return result


def covers_source(review, parts):
    """Prove character coverage, independently from model response ID coverage."""
    for field, offset_key in (("text", "offset"), ("title", "title_offset")):
        end = 0
        for start, stop in sorted((p.get(offset_key, 0), p.get(offset_key, 0) + len(p[field]))
                                  for p in parts if p[field]):
            if start > end:
                return False
            end = max(end, stop)
        if end != len(review[field]):
            return False
    return True
