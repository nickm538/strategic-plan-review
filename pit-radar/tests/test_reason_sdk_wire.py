"""The classifier through the REAL anthropic SDK (1.13.0) with an in-memory httpx2.MockTransport: checks the
wire request (beta header, fallbacks, output_config, cached system block, no thinking) and that real
BetaMessage parsing (model, stop_reason, stop_details, usage, request id) and real HTTP error mapping work.
No network."""
from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from app.pipeline import classifier as clf
from tests.test_reason_support import DEFAULT_POST, NOW, make_item, verdict_dict

SCHEMA = clf.load_schema()
BLOCKS = clf.build_system_blocks(None)


def message_json(*, text=None, stop_reason="end_turn", model="claude-opus-5-5", stop_details=None, usage=None,
                 content=None):
    if content is None:
        content = [{"type": "text", "text": text}] if text is not None else []
    return {
        "id": "msg_wire_1", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
        "usage": usage or {"input_tokens": 812, "output_tokens": 455, "cache_read_input_tokens": 6120,
                           "cache_creation_input_tokens": 0},
    }


class Wire:
    """Scripted HTTP responses; records each request's headers and JSON body."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append((dict(request.headers), json.loads(request.content.decode())))
        status, body, headers = self.responses.pop(0)
        return httpx2.Response(status, json=body, headers={"request-id": "req_wire_1", **(headers or {})})

    def client(self) -> anthropic.AsyncAnthropic:
        http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler))
        return anthropic.AsyncAnthropic(api_key="test-key-not-real", base_url="https://api.anthropic.test",
                                        max_retries=0, http_client=http)


async def classify(wire: Wire, model="claude-opus-5-5"):
    async with wire.client() as client:
        return await clf.classify(make_item(DEFAULT_POST), client=client, model=model, effort="medium",
                                  system_blocks=BLOCKS, schema=SCHEMA, now=NOW)


@pytest.fixture(autouse=True)
def _reset():
    clf.reset_fallback_state()
    yield
    clf.reset_fallback_state()


async def test_wire_request_and_response_parsing():
    wire = Wire((200, message_json(text=json.dumps(verdict_dict())), None))
    res = await classify(wire)
    assert res.kind == "ok" and res.verdict.signal_type == "encampment"
    assert res.model == "claude-opus-5-5"
    assert res.usage.request_id == "req_wire_1"
    assert (res.usage.input_tokens, res.usage.output_tokens, res.usage.cache_read_tokens) == (812, 455, 6120)

    headers, body = wire.requests[0]
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    assert body["fallbacks"] == "default"
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 6000
    assert body["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert body["system"] == BLOCKS and body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][0]["role"] == "user" and "<post>" in body["messages"][0]["content"]
    assert "thinking" not in body and "betas" not in body and "timeout" not in body
    assert "test-key-not-real" not in json.dumps(body)


async def test_wire_haiku_has_no_fallback():
    wire = Wire((200, message_json(text=json.dumps(verdict_dict()), model="claude-haiku-5-5"), None))
    res = await classify(wire, model="claude-haiku-5-5")
    headers, body = wire.requests[0]
    assert res.kind == "ok"
    assert "fallbacks" not in body
    assert "server-side-fallback" not in headers.get("anthropic-beta", "")


async def test_wire_refusal_details():
    details = {"type": "refusal", "category": "general_harms", "explanation": None}
    wire = Wire((200, message_json(stop_reason="refusal", stop_details=details), None))
    res = await classify(wire)
    assert (res.kind, res.reason, res.verdict) == ("needs_human_review", "refusal:general_harms", None)


async def test_wire_fallback_iterations_priced_per_model():
    usage = {"input_tokens": 7000, "output_tokens": 500, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0,
             "iterations": [
                 {"type": "fallback_message", "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 0,
                  "cache_read_input_tokens": 6000, "cache_creation_input_tokens": 0},
                 {"type": "message", "model": "claude-opus-5", "input_tokens": 7000, "output_tokens": 500,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}]}
    content = [{"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
                "trigger": {"type": "refusal", "category": "general_harms"}},
               {"type": "text", "text": json.dumps(verdict_dict())}]
    wire = Wire((200, message_json(model="claude-opus-5", usage=usage, content=content), None))
    res = await classify(wire)
    assert res.kind == "ok" and res.model == "claude-opus-5"
    assert res.usage.input_tokens == 8000
    assert res.usage.cost_usd == pytest.approx((1000 * 4 + 6000 * 0.20 + 7000 * 5 + 500 * 25) / 1e6)


@pytest.mark.parametrize("status, headers, kind, reason, retry_after", [
    (400, None, "error", "bad_request", None),
    (401, None, "provider_down", "anthropic_auth", None),
    (403, None, "provider_down", "anthropic_forbidden", None),
    (404, None, "provider_down", "anthropic_model_not_found", None),
    (429, {"retry-after": "7"}, "retry_later", "rate_limited", 7.0),
    (500, None, "retry_later", "server_error_500", None),
    (529, None, "retry_later", "server_error_529", None),
])
async def test_wire_http_errors(status, headers, kind, reason, retry_after):
    err = {"type": "error", "error": {"type": "some_error", "message": "synthetic error"}}
    wire = Wire((status, err, headers))
    res = await classify(wire)
    assert (res.kind, res.reason, res.retry_after_s) == (kind, reason, retry_after)


async def test_wire_fallback_beta_rejection_recovers():
    err = {"type": "error", "error": {"type": "invalid_request_error",
                                      "message": "Unexpected value(s) `server-side-fallback-2026-07-01` for the "
                                                 "`anthropic-beta` header."}}
    wire = Wire((400, err, None), (200, message_json(text=json.dumps(verdict_dict())), None))
    res = await classify(wire)
    assert res.kind == "ok"
    headers2, body2 = wire.requests[1]
    assert "fallbacks" not in body2 and "server-side-fallback" not in headers2.get("anthropic-beta", "")
