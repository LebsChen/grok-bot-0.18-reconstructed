"""InferenceService.Stream translation: Connect request -> OpenAI-
compatible ``/chat/completions`` SSE -> InferenceStreamResponse frames.

Pure stdlib; works identically on the desktop side (desktop.py) and the
guest side (box.py). The only configuration is the LLM endpoint triple
(base_url, api_key, model map) injected by the caller.
"""

from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.request

from google.protobuf import json_format

from .codec import codec

log = logging.getLogger("devbox_bot.inference")

_ROLE_NAMES = {1: "user", 2: "assistant", 3: "tool", 4: "system"}
_ERROR_TYPES = {
    401: ("AUTHENTICATION", 5), 403: ("PERMISSION", 6),
    413: ("INPUT_TOKEN_LIMIT", 2), 429: ("RATE_LIMIT", 4),
    500: ("UNKNOWN", 1), 503: ("OVERLOADED", 7),
}


def _struct_to_py(struct_msg) -> object:
    return json_format.MessageToDict(
        struct_msg, preserving_proto_field_name=True)


def _message_to_openai(msg) -> dict:
    role = _ROLE_NAMES.get(msg.role, "user")
    out = {"role": role}
    text = msg.text or ""
    content_kind = msg.WhichOneof("content")
    # richer content parts override the flat text field
    if content_kind == "parts":
        parts = []
        for part in msg.parts.parts:
            kind = part.WhichOneof("part")
            if kind == "text":
                parts.append({"type": "text",
                              "text": part.text.text})
            elif kind == "image":
                parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{part.image.mime_type};"
                               f"base64,{part.image.data}"}})
        out["content"] = parts if parts else text
    else:
        out["content"] = text
    if content_kind == "tool_content":
        for part in msg.tool_content.parts:
            out["tool_call_id"] = part.tool_call_id
            if not out["content"]:
                try:
                    out["content"] = json.dumps(
                        _struct_to_py(part.result))
                except Exception:  # noqa: BLE001 — odd Value types
                    out["content"] = ""
            break
    if len(msg.tool_calls):
        calls = []
        for call in msg.tool_calls:
            args = call.raw_tool_call_args or json.dumps(
                _struct_to_py(call.args) if call.HasField("args") else {})
            calls.append({
                "id": call.tool_call_id, "type": "function",
                "function": {"name": call.tool_name,
                             "arguments": args}})
        if calls:
            out["tool_calls"] = calls
            out.setdefault("content", out.get("content") or None)
    return out


def request_to_openai(req, model_map: dict | None = None) -> dict:
    """Translate a decoded InferenceStreamRequest into a /chat/completions
    request body."""
    model_map = model_map or {}
    model = req.model_id or (
        req.requested_model.model_id
        if req.HasField("requested_model") else "")
    body = {
        "model": model_map.get(model, model),
        "messages": [_message_to_openai(m) for m in req.messages],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if req.tools:
        body["tools"] = [{
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": _struct_to_py(t.parameters)
                if t.HasField("parameters") else
                {"type": "object", "properties": {}},
            }} for t in req.tools]
    cfg = req.model_config if req.HasField("model_config") else None
    if cfg is not None:
        if cfg.max_tokens:
            body["max_tokens"] = cfg.max_tokens
        if cfg.temperature:
            body["temperature"] = cfg.temperature
        if cfg.top_p:
            body["top_p"] = cfg.top_p
        if cfg.stop_sequences:
            body["stop"] = list(cfg.stop_sequences)
    return body


def _error_frame(message: str, status: int | None = None) -> dict:
    name, code = _ERROR_TYPES.get(status or 0, ("UNKNOWN", 1))
    frame = {
        "error": {
            "message": message,
            "code": name,
            "error_type": code,
        }}
    if code == 2:
        frame["error"]["is_input_token_limit_error"] = True
    if code == 3:
        frame["error"]["is_output_token_limit_error"] = True
    return frame


def stream_inference(req, base_url: str, api_key: str,
                     model_map: dict | None = None,
                     timeout: float = 120.0):
    """Yield InferenceStreamResponse messages for one request."""
    out_type = "aiserver.v1.InferenceStreamResponse"

    def frame(**kwargs):
        return codec().new(out_type, **kwargs)

    invocation = req.invocation_id or f"inv-{secrets.token_hex(8)}"
    yield frame(invocation_id={"invocation_id": invocation})
    try:
        log.debug("inference req: %s",
                  str(req).replace("\n", " ")[:800])
        body = request_to_openai(req, model_map)
    except Exception as exc:  # noqa: BLE001 — any field may fail
        yield frame(error={
            "message": f"request translation failed: {exc}",
            "code": "UNKNOWN", "error_type": 1})
        return
    log.debug("inference upstream body: %s", json.dumps(body)[:600])
    http_req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {api_key}",
                 "accept": "text/event-stream"},
        method="POST")
    try:
        resp = urllib.request.urlopen(http_req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")[:400]
        yield frame(
            **_error_frame(f"upstream {exc.code}: {text}"[:400],
                           exc.code))
        return
    except Exception as exc:  # noqa: BLE001 — DNS/TLS/socket
        yield frame(error={
            "message": f"upstream unreachable: {exc}",
            "code": "OVERLOADED", "error_type": 7})
        return

    tool_state: dict[int, dict] = {}
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            usage = event.get("usage")
            if usage:
                yield frame(usage={
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens":
                        usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0)})
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    yield frame(thinking_part={
                        "text": delta["reasoning_content"]})
                if delta.get("content"):
                    yield frame(text_part={"text": delta["content"]})
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    state = tool_state.setdefault(
                        idx, {"id": "", "name": ""})
                    if tc.get("id"):
                        state["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        state["name"] = fn["name"]
                    if fn.get("arguments"):
                        yield frame(tool_call_part={
                            "tool_call_id": state["id"],
                            "tool_name": state["name"],
                            "args": fn["arguments"],
                            "tool_index": idx})
                if choice.get("finish_reason"):
                    for idx, state in tool_state.items():
                        yield frame(tool_call_part={
                            "tool_call_id": state["id"],
                            "tool_name": state["name"],
                            "is_complete": True,
                            "tool_index": idx})
                    yield frame(text_part={"is_final": True})
