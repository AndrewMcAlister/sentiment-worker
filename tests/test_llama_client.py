"""Tests for the llama.cpp client.

The HTTP layer is faked at ``urllib.request.urlopen`` so every request shape and
every failure branch is exercised without a server.  The one thing that is not
faked is the parser: a malformed reply must fail the article, never guess.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import config
import llama_client


# ── helpers ──────────────────────────────────────────────────────────────────


class FakeResponse:
    """The context-manager shape ``urlopen`` returns, over a fixed body."""

    def __init__(self, body) -> None:
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def read(self) -> bytes:
        return self.body


def http_error(code: int, detail: bytes = b"busy") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        config.LLAMA_CHAT_URL, code, "server said no", None, io.BytesIO(detail)
    )


def chat_reply(content, *, model: str = "qwen-test") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 100},
        },
    }


# ── the 1-10 scale ───────────────────────────────────────────────────────────


def test_the_scale_maps_the_endpoints_and_neutral():
    assert llama_client.to_ten(-1.0) == 1.0
    assert llama_client.to_ten(0.0) == 5.5
    assert llama_client.to_ten(1.0) == 10.0
    assert llama_client.to_ten(0.2) == 6.4


def test_clamp_ten_keeps_a_reading_inside_the_file_contract():
    assert llama_client.clamp_ten(-2.0) == 1.0
    assert llama_client.clamp_ten(11.0) == 10.0
    assert llama_client.clamp_ten(5.44) == 5.4


# ── the strict parser ────────────────────────────────────────────────────────


def test_a_well_formed_reply_is_parsed():
    assert llama_client.parse_reply("positive|0.40|high") == {
        "label": "positive",
        "score": 0.4,
        "confidence": "high",
    }


def test_the_parser_tolerates_space_and_case_but_not_a_fourth_field():
    assert llama_client.parse_reply("  NEGATIVE | -0.5 | LOW \n")["label"] == "negative"
    with pytest.raises(llama_client.LlamaError, match="expected 3 fields"):
        llama_client.parse_reply("positive|0.4|high|extra")


@pytest.mark.parametrize(
    "reply, message",
    [
        ("up|0.4|high", "label"),
        ("positive|0.4|certain", "confidence"),
        ("positive|bullish|high", "not a number"),
        ("positive|1.5|high", "outside -1..1"),
        ("positive|nan|high", "outside -1..1"),
        ("", "empty"),
    ],
)
def test_every_malformed_reply_is_refused(reply, message):
    with pytest.raises(llama_client.LlamaError, match=message):
        llama_client.parse_reply(reply)


# ── which model the router says is loaded ────────────────────────────────────


def test_the_loaded_preset_wins_over_an_unloaded_one():
    entries = [
        {"id": "other", "status": {"value": "unloaded"}},
        {"id": "loaded-preset", "status": {"value": "loaded"}},
    ]
    assert llama_client.loaded_model(entries) == "loaded-preset"


def test_a_plain_server_publishes_one_model_with_no_status():
    assert llama_client.loaded_model([{"id": "single"}]) == "single"


def test_several_statusless_models_cannot_be_chosen_between():
    assert llama_client.loaded_model([{"id": "one"}, {"id": "two"}]) == ""


def test_nothing_loaded_and_nothing_published_is_an_empty_answer():
    assert llama_client.loaded_model([]) == ""
    assert llama_client.loaded_model([{"id": "idle", "status": {"value": "unloaded"}}]) == ""


def test_the_loaded_model_is_asked_for_by_name(monkeypatch):
    monkeypatch.setattr(
        llama_client,
        "models",
        lambda timeout=None: [{"id": "from-router", "status": {"value": "loaded"}}],
    )
    monkeypatch.setattr(config, "LLAMA_MODEL", "configured-fallback")

    assert llama_client.model_name() == "from-router"


def test_the_configured_name_is_only_the_fallback(monkeypatch):
    monkeypatch.setattr(llama_client, "models", lambda timeout=None: [])
    monkeypatch.setattr(config, "LLAMA_MODEL", "configured-fallback")

    assert llama_client.model_name() == "configured-fallback"


def test_no_loaded_model_and_no_fallback_names_what_the_router_saw(monkeypatch):
    monkeypatch.setattr(
        llama_client,
        "models",
        lambda timeout=None: [{"id": "idle", "status": {"value": "unloaded"}}],
    )
    monkeypatch.setattr(config, "LLAMA_MODEL", "")

    with pytest.raises(llama_client.LlamaError, match="idle .unloaded."):
        llama_client.model_name()


def test_is_up_is_the_liveness_probe(monkeypatch):
    monkeypatch.setattr(llama_client, "models", lambda timeout=None: [{"id": "single"}])
    assert llama_client.is_up() is True

    def down(timeout=None):
        raise llama_client.LlamaDown("connection refused")

    monkeypatch.setattr(llama_client, "models", down)
    assert llama_client.is_up() is False

# ── the model list over HTTP ────────────────────────────────────────────────


def test_the_model_list_is_read_from_the_router(monkeypatch):
    seen = {}

    def urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return FakeResponse({"data": [{"id": "one"}, "not an entry"]})

    monkeypatch.setattr(llama_client.urllib.request, "urlopen", urlopen)

    assert llama_client.models(timeout=1.5) == [{"id": "one"}]
    assert seen == {"url": config.LLAMA_MODELS_URL, "timeout": 1.5}


def test_an_unreachable_model_list_is_llama_down(monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(llama_client.urllib.request, "urlopen", urlopen)

    with pytest.raises(llama_client.LlamaDown, match="not answering"):
        llama_client.models()


def test_a_server_error_on_the_model_list_is_llama_down(monkeypatch):
    def urlopen(request, timeout=None):
        raise http_error(503)

    monkeypatch.setattr(llama_client.urllib.request, "urlopen", urlopen)

    with pytest.raises(llama_client.LlamaDown, match="HTTP 503"):
        llama_client.models()


def test_a_model_list_without_json_or_data_is_a_client_error(monkeypatch):
    monkeypatch.setattr(
        llama_client.urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeResponse(b"<html>"),
    )
    with pytest.raises(llama_client.LlamaError, match="not JSON"):
        llama_client.models()

    monkeypatch.setattr(
        llama_client.urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeResponse({"models": []}),
    )
    with pytest.raises(llama_client.LlamaError, match="no data array"):
        llama_client.models()


# ── one chat request ────────────────────────────────────────────────────────


def test_the_chat_request_is_the_measured_shape(monkeypatch):
    captured = {}

    def urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(chat_reply(" positive|0.40|high "))

    monkeypatch.setattr(llama_client.urllib.request, "urlopen", urlopen)

    verdict = llama_client.score("Title\n\nBody", model="qwen-test", timeout=2.0)

    assert captured["url"] == config.LLAMA_CHAT_URL
    assert captured["timeout"] == 2.0
    body = captured["body"]
    assert body["model"] == "qwen-test"
    assert body["messages"] == [
        {"role": "system", "content": llama_client.SYSTEM_PROMPT},
        {"role": "user", "content": "Title\n\nBody"},
    ]
    # Without this a reasoning preset spends the whole budget before `content`.
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == llama_client.MAX_TOKENS == 16
    assert body["temperature"] == 0.0
    assert body["stream"] is False

    assert verdict["label"] == "positive"
    assert verdict["score"] == 0.4
    assert verdict["confidence"] == "high"
    assert verdict["reply"] == "positive|0.40|high"
    assert verdict["model"] == "qwen-test"
    assert verdict["prompt_tokens"] == 120
    assert verdict["completion_tokens"] == 3
    assert verdict["cached_tokens"] == 100
    assert verdict["elapsed_ms"] >= 0.0


def test_a_request_without_a_model_name_is_refused_before_any_http(monkeypatch):
    def urlopen(request, timeout=None):  # pragma: no cover - must not be called
        raise AssertionError("a request was sent without a model")

    monkeypatch.setattr(llama_client.urllib.request, "urlopen", urlopen)

    with pytest.raises(llama_client.LlamaError, match="no model name"):
        llama_client.score("Title", model="")


def test_reasoning_without_content_names_the_real_failure(monkeypatch):
    payload = {"choices": [{"message": {"content": None, "reasoning_content": "let me think"}}]}
    monkeypatch.setattr(
        llama_client.urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeResponse(payload),
    )

    with pytest.raises(llama_client.LlamaError, match="reasoned instead"):
        llama_client.score("Title", model="qwen-test")



@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "no choices"),
        ({"choices": []}, "no choices"),
        ({"choices": [{"message": {"content": 5}}]}, "not text"),
        ({"choices": [{"message": {"content": "not a reply"}}]}, "expected 3 fields"),
    ],
)
def test_a_reply_without_a_usable_reading_is_a_client_error(monkeypatch, payload, message):
    monkeypatch.setattr(
        llama_client.urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeResponse(payload),
    )

    with pytest.raises(llama_client.LlamaError, match=message):
        llama_client.score("Title", model="qwen-test")


def test_a_4xx_answer_fails_the_article_but_not_the_pass(monkeypatch):
    def urlopen(request, timeout=None):
        raise http_error(400, b"bad request")

    monkeypatch.setattr(llama_client.urllib.request, "urlopen", urlopen)

    with pytest.raises(llama_client.LlamaError) as caught:
        llama_client.score("Title", model="qwen-test")
    assert not isinstance(caught.value, llama_client.LlamaDown)
    assert "HTTP 400" in str(caught.value)
    assert "bad request" in str(caught.value)


@pytest.mark.parametrize("code", [500, 503])
def test_a_5xx_answer_means_the_server_is_down(monkeypatch, code):
    def urlopen(request, timeout=None):
        raise http_error(code)

    monkeypatch.setattr(llama_client.urllib.request, "urlopen", urlopen)

    with pytest.raises(llama_client.LlamaDown, match=f"HTTP {code}"):
        llama_client.score("Title", model="qwen-test")


def test_an_unreachable_chat_endpoint_is_llama_down(monkeypatch):
    def urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(llama_client.urllib.request, "urlopen", urlopen)

    with pytest.raises(llama_client.LlamaDown, match="not answering"):
        llama_client.score("Title", model="qwen-test")



# ── article to reading ──────────────────────────────────────────────────────


def test_an_article_is_cut_to_the_prompt_caps():
    long = {"title": "T" * 400, "vendor_summary": "B" * 2000}
    text = llama_client.article_prompt(long)
    assert len(text) == config.MAX_ARTICLE_CHARS
    assert text.startswith("T" * config.MAX_TITLE_CHARS + "\n\n")

    assert llama_client.article_prompt({"title": "  A\n\t title ", "vendor_summary": ""}) == "A title"


def test_score_article_maps_the_verdict_onto_the_scale(monkeypatch):
    seen = {}

    def fake_score(text, *, model, timeout=None):
        seen["text"] = text
        seen["model"] = model
        seen["timeout"] = timeout
        return {
            "label": "positive",
            "score": 0.4,
            "confidence": "high",
            "reply": "positive|0.40|high",
            "model": "qwen-test",
            "cached_tokens": 90,
            "elapsed_ms": 12.5,
        }

    monkeypatch.setattr(llama_client, "score", fake_score)

    verdict = llama_client.score_article(
        {"title": "Results", "vendor_summary": "Revenue rose."}, model="qwen-test"
    )

    assert seen == {"text": "Results\n\nRevenue rose.", "model": "qwen-test", "timeout": None}
    assert verdict["score"] == 7.3
    assert verdict["raw"] == 0.4
    assert verdict["label"] == "positive"
    assert verdict["confidence"] == "high"
    assert verdict["cached_tokens"] == 90


def test_an_article_with_no_text_is_never_sent(monkeypatch):
    def score(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("an empty article was sent")

    monkeypatch.setattr(llama_client, "score", score)

    with pytest.raises(llama_client.LlamaError, match="no text"):
        llama_client.score_article({"title": "  ", "vendor_summary": ""}, model="qwen-test")

