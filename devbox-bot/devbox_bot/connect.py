"""Connect-protocol plumbing on stdlib ThreadingHTTPServer.

Serves unary ``POST /<pkg.Service>/<Method>`` (application/proto and
application/json) and server-streaming ``application/connect+proto``
handlers, with a catch-all that returns a valid empty response and logs
``grokbot.rpc.unhandled <Service>/<Method>`` once per method.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .codec import (
    ConnectError,
    codec,
    connect_envelope,
    end_stream_envelope,
    from_json_dict,
    is_streaming,
    lookup_method,
    parse_envelope,
    to_json_dict,
)

log = logging.getLogger("devbox_bot.connect")

_unhandled_seen: set[str] = set()
_unhandled_lock = threading.Lock()


def log_unhandled(service_name: str, method_name: str) -> None:
    key = f"{service_name}/{method_name}"
    with _unhandled_lock:
        if key in _unhandled_seen:
            return
        _unhandled_seen.add(key)
    log.info("grokbot.rpc.unhandled %s", key)


def unhandled_methods() -> list[str]:
    with _unhandled_lock:
        return sorted(_unhandled_seen)


def reset_unhandled() -> None:
    with _unhandled_lock:
        _unhandled_seen.clear()


class ConnectRouter:
    """Map ``(service_name, method_name)`` -> handler.

    Handlers receive the decoded request message (or None for empty
    bodies) and a request context dict ``{"authorization": ...}`` and
    return either a protobuf message / dict (unary) or an iterable of
    protobuf messages / dicts (server-streaming).
    """

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], object] = {}
        self._streaming: set[tuple[str, str]] = set()

    def unary(self, service: str, method: str):
        def deco(fn):
            self._handlers[(service, method)] = fn
            return fn
        return deco

    def streaming(self, service: str, method: str):
        def deco(fn):
            self._handlers[(service, method)] = fn
            self._streaming.add((service, method))
            return fn
        return deco

    def _find(self, service_name: str, method_name: str):
        hit = lookup_method(service_name, method_name)
        if hit is None:
            return None, None
        type_name, spec = hit
        fn = self._handlers.get((type_name, spec["name"]))
        return (type_name, spec), fn

    # returns (status, content_type, body-bytes)
    def dispatch(self, service_name: str, method_name: str,
                 content_type: str, raw_body: bytes,
                 ctx: dict | None = None) -> tuple[int, str, bytes]:
        ctx = dict(ctx or {})
        found = self._find(service_name, method_name)
        if found[0] is None:
            log_unhandled(service_name, method_name)
            return 404, "application/json", ConnectError(
                "unimplemented",
                f"{service_name}/{method_name}").body().__repr__().encode()
        (type_name, spec), fn = found
        # decode request
        try:
            if "application/json" in content_type:
                req = from_json_dict(
                    spec["input"],
                    json.loads(raw_body or b"{}"))
            elif "connect" in content_type:
                req = codec().decode(
                    spec["input"], parse_envelope(raw_body))
            else:
                req = codec().decode(spec["input"], raw_body)
        except Exception as exc:  # noqa: BLE001 — any decode failure is a 400
            err = ConnectError("invalid_argument", f"decode: {exc}")
            return err.status, "application/json", json.dumps(
                err.body()).encode()

        if fn is None or (
                is_streaming(spec)
                and (type_name, spec["name"]) not in self._streaming):
            log_unhandled(type_name, spec["name"])
            if is_streaming(spec):
                return (200, "application/connect+proto",
                        end_stream_envelope())
            empty = codec().new(spec["output"])
            if "application/json" in content_type:
                return (200, "application/json",
                        json.dumps(to_json_dict(empty)).encode())
            return 200, "application/proto", codec().encode(empty)

        try:
            if is_streaming(spec):
                frames = []
                for out in fn(req, ctx) or []:
                    if isinstance(out, dict):
                        out = from_json_dict(spec["output"], out)
                    frames.append(connect_envelope(codec().encode(out)))
                frames.append(end_stream_envelope())
                return 200, "application/connect+proto", b"".join(frames)
            out = fn(req, ctx)
            if isinstance(out, dict):
                out = from_json_dict(spec["output"], out)
            if out is None:
                out = codec().new(spec["output"])
            if "application/json" in content_type:
                return (200, "application/json",
                        json.dumps(to_json_dict(out)).encode())
            return 200, "application/proto", codec().encode(out)
        except ConnectError as exc:
            if is_streaming(spec):
                return (200, "application/connect+proto",
                        end_stream_envelope(exc))
            return (exc.status, "application/json",
                    json.dumps(exc.body()).encode())
        except Exception as exc:
            log.exception("connect handler failed: %s/%s",
                          type_name, spec["name"])
            err = ConnectError("internal", str(exc)[:200])
            if is_streaming(spec):
                return (200, "application/connect+proto",
                        end_stream_envelope(err))
            return (err.status, "application/json",
                    json.dumps(err.body()).encode())


def make_handler(router: ConnectRouter,
                 extra_routes: dict | None = None):
    """Build a BaseHTTPRequestHandler class serving ``router``.

    ``extra_routes`` maps ``(method, path)`` -> callable(request_handler)
    that writes the response itself (used for non-Connect endpoints such
    as /loginDeepControl and /oauth/token).
    """
    extra = dict(extra_routes or {})

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "devbox-bot"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, content_type: str, body: bytes):
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("content-length") or 0)
            if length:
                return self.rfile.read(length)
            if "chunked" in self.headers.get(
                    "transfer-encoding", "").lower():
                return self._read_chunked()
            return b""

        def _read_chunked(self) -> bytes:
            """Consume a chunked request body; without this the chunk
            trailer corrupts the next request on keep-alive sockets."""
            chunks = []
            while True:
                size_line = self.rfile.readline(65536).strip()
                try:
                    size = int(size_line.split(b";", 1)[0], 16)
                except ValueError:
                    break
                if size == 0:
                    # consume trailers until the blank line
                    while self.rfile.readline(65536) not in (
                            b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline(65536)  # trailing CRLF
            return b"".join(chunks)

        def _handle(self, method: str):
            path = self.path.split("?", 1)[0]
            fn = extra.get((method, path))
            if fn is not None:
                fn(self)
                return
            if method != "POST":
                self._send(404, "application/json",
                           b'{"code":"not_found"}')
                return
            if not path.startswith("/") or "/" not in path[1:]:
                self._send(404, "application/json",
                           b'{"code":"not_found"}')
                return
            service, _, method_name = path[1:].rpartition("/")
            if not service or not method_name:
                self._send(404, "application/json",
                           b'{"code":"not_found"}')
                return
            content_type = self.headers.get("content-type", "")
            status, ctype, body = router.dispatch(
                service, method_name, content_type, self._read_body(),
                {"authorization": self.headers.get("authorization", ""),
                 "headers": self.headers})
            self._send(status, ctype, body)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    Handler._send_json = staticmethod(
        lambda self, status, obj: self._send(
            status, "application/json", json.dumps(obj).encode()))
    return Handler


def serve(router: ConnectRouter, host: str, port: int,
          extra_routes: dict | None = None) -> ThreadingHTTPServer:
    handler = make_handler(router, extra_routes)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
