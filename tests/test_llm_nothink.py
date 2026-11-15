# 单元测试: 视觉通道强制非思考模式(需求 7.3 全局硬性)
from unittest import mock

from core.config import Config
from core.llm import LLMClient


def test_vision_forces_no_think():
    cfg = Config({})
    cfg.set("LLM_API_KEY", "k")
    cfg.set("VISION_API_KEY", "k")
    client = LLMClient(cfg)
    with mock.patch.object(client, "_request", return_value=('{"color": "red"}', "stop")) as req:
        result = client.vision("描述图片", ["https://img.example.com/x.jpg"])
    assert result == {"color": "red"}
    assert req.call_args.kwargs.get("no_think") is True
    assert req.call_args.kwargs.get("images") == ["https://img.example.com/x.jpg"]


def test_vision_forces_no_think_even_when_flag_off():
    cfg = Config({})
    cfg.set("LLM_API_KEY", "k")
    cfg.set("VISION_API_KEY", "k")
    cfg.set("LLM_NO_THINK", False)
    client = LLMClient(cfg)
    with mock.patch.object(client, "_request", return_value=('{"color": "blue"}', "stop")) as req:
        client.vision("描述图片", ["https://img.example.com/y.jpg"])
    assert req.call_args.kwargs.get("no_think") is True


def test_chat_follows_no_think_flag():
    cfg = Config({})
    cfg.set("LLM_API_KEY", "k")
    cfg.set("LLM_NO_THINK", True)
    client = LLMClient(cfg)
    with mock.patch.object(client, "_request", return_value=("ok", "stop")) as req:
        client.chat("hi")
    assert req.call_args.kwargs.get("no_think") is None


def test_vision_passes_detail_and_max_images_through():
    cfg = Config({})
    cfg.set("VISION_API_KEY", "k")
    client = LLMClient(cfg)
    with mock.patch.object(client, "_request", return_value=('{"color": "red"}', "stop")) as req:
        client.vision("看图", ["https://img.example.com/a.jpg"],
                      detail="high", max_images=9)
    assert req.call_args.kwargs.get("detail") == "high"
    assert req.call_args.kwargs.get("max_images") == 9


def test_vision_defaults_do_not_truncate():
    cfg = Config({})
    cfg.set("VISION_API_KEY", "k")
    client = LLMClient(cfg)
    with mock.patch.object(client, "_request", return_value=('{"color": "red"}', "stop")) as req:
        client.vision("看图", ["https://img.example.com/%d.jpg" % i for i in range(7)])
    assert req.call_args.kwargs.get("max_images") is None
    assert req.call_args.kwargs.get("detail") == "low"
