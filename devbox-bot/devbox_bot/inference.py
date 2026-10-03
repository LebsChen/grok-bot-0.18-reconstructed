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


def _requested_model(req) -> str:
    return req.model_id or (
        req.requested_model.model_id
        if req.HasField("requested_model") else "")


def request_to_openai(req, model_map: dict | None = None,
                      default_model: str = "") -> dict:
    """Translate a decoded InferenceStreamRequest into a /chat/completions
    request body."""
    model_map = model_map or {}
    model = _requested_model(req)
    body = {
        "model": model_map.get(model) or default_model or model,
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
                     timeout: float = 120.0,
                     default_model: str = ""):
    """Yield InferenceStreamResponse messages for one request."""
    out_type = "aiserver.v1.InferenceStreamResponse"

    def frame(**kwargs):
        return codec().new(out_type, **kwargs)

    invocation = req.invocation_id or f"inv-{secrets.token_hex(8)}"
    yield frame(invocation_id={"invocation_id": invocation})
    try:
        requested_model = _requested_model(req)
        body = request_to_openai(req, model_map, default_model)
    except Exception as exc:  # noqa: BLE001 — any field may fail
        yield frame(error={
            "message": f"request translation failed: {exc}",
            "code": "UNKNOWN", "error_type": 1})
        return
    payload = json.dumps(body).encode("utf-8")
    log.info(
        "inference request: requested=%s upstream=%s messages=%d "
        "tools=%d body_bytes=%d",
        requested_model or "<empty>", body["model"] or "<empty>",
        len(body["messages"]), len(body.get("tools") or []), len(payload))
    http_req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={"content-type": "application/json",
                 "authorization": f"Bearer {api_key}",
                 "accept": "text/event-stream"},
        method="POST")
    text_parts = 0
    thinking_parts = 0
    tool_state: dict[int, dict] = {}
    finish_reason: str | None = None
    has_usage = False

    def log_stream_end():
        tool_names = ",".join(
            state["name"] for _, state in sorted(tool_state.items())
            if state["name"]) or "none"
        log.info(
            "inference stream ended: text_parts=%d thinking_parts=%d "
            "tool_calls=%d finish_reason=%s usage=%s tools=%s",
            text_parts, thinking_parts, len(tool_state),
            finish_reason or "none", "y" if has_usage else "n",
            tool_names)

    try:
        resp = urllib.request.urlopen(http_req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")[:400]
        log.warning("upstream %d: %s", exc.code, text[:300])
        yield frame(
            **_error_frame(f"upstream {exc.code}: {text}"[:400],
                           exc.code))
        log_stream_end()
        return
    except Exception as exc:  # noqa: BLE001 — DNS/TLS/socket
        log.warning("upstream unreachable: %s", exc)
        yield frame(error={
            "message": f"upstream unreachable: {exc}",
            "code": "OVERLOADED", "error_type": 7})
        log_stream_end()
        return

    saw_done = False
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                event_payload = line[5:].strip()
                if event_payload == "[DONE]":
                    saw_done = True
                    break
                try:
                    event = json.loads(event_payload)
                except ValueError:
                    continue
                usage = event.get("usage")
                if usage:
                    has_usage = True
                    yield frame(usage={
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens":
                            usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0)})
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        thinking_parts += 1
                        yield frame(thinking_part={
                            "text": delta["reasoning_content"]})
                    if delta.get("content"):
                        text_parts += 1
                        yield frame(text_part={"text": delta["content"]})
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        state = tool_state.setdefault(
                            idx, {
                                "id": "", "name": "", "args": "",
                                "started": False, "completed": False})
                        if tc.get("id"):
                            state["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            state["name"] = fn["name"]
                        args_delta = fn.get("arguments") or ""
                        if args_delta:
                            state["args"] += args_delta
                        started_now = False
                        if (state["id"] and state["name"]
                                and not state["started"]):
                            state["started"] = True
                            started_now = True
                            yield frame(tool_call_part={
                                "tool_call_id": state["id"],
                                "tool_name": state["name"],
                                "tool_index": idx})
                        if started_now and state["args"]:
                            yield frame(tool_call_part={
                                "tool_call_id": state["id"],
                                "args": state["args"],
                                "tool_index": idx})
                        elif state["started"] and args_delta:
                            yield frame(tool_call_part={
                                "tool_call_id": state["id"],
                                "args": args_delta,
                                "tool_index": idx})
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                        for idx, state in tool_state.items():
                            if state["completed"]:
                                continue
                            if (state["id"] and state["name"]
                                    and not state["started"]):
                                state["started"] = True
                                yield frame(tool_call_part={
                                    "tool_call_id": state["id"],
                                    "tool_name": state["name"],
                                    "tool_index": idx})
                            yield frame(tool_call_part={
                                "tool_call_id": state["id"],
                                "tool_name": state["name"],
                                "args": state["args"] or "{}",
                                "is_complete": True,
                                "tool_index": idx})
                            state["completed"] = True
                        yield frame(text_part={"is_final": True})
    except Exception as exc:  # noqa: BLE001 — stream read failure
        log.warning("upstream unreachable: %s", exc)
        yield frame(error={
            "message": f"upstream unreachable: {exc}",
            "code": "OVERLOADED", "error_type": 7})
    else:
        pending = sum(
            1 for state in tool_state.values() if not state["completed"])
        if finish_reason is None and (not saw_done or pending):
            log.warning(
                "upstream stream truncated: done=%s pending_tools=%d",
                saw_done, pending)
            yield frame(error={
                "message": "upstream stream ended without finish_reason",
                "code": "OVERLOADED", "error_type": 7})
    finally:
        log_stream_end()
