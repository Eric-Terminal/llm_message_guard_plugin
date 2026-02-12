#!/usr/bin/env python3
"""本地调试用：伪 OpenAI Chat Completions 服务。

用途：
- 监听本机端口（默认 10030）
- 打印并保存原始请求体，方便验证 messages 结构
- 返回最小兼容响应，避免上游调用报错
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


VALID_PATHS = {"/chat/completions", "/v1/chat/completions"}


def safe_preview(text: Any, limit: int = 100) -> str:
    value = str(text or "")
    value = value.replace("\n", "\\n")
    return value[:limit] + ("..." if len(value) > limit else "")


def summarize_messages(messages: list[dict[str, Any]]) -> str:
    role_counter = Counter()
    lines: list[str] = []

    for idx, msg in enumerate(messages, start=1):
        role = str(msg.get("role", "unknown"))
        role_counter[role] += 1

        content = msg.get("content", "")
        if isinstance(content, list):
            preview = safe_preview(json.dumps(content, ensure_ascii=False))
        else:
            preview = safe_preview(content)

        lines.append(f"  {idx:02d}. role={role:<9} content={preview}")

    role_summary = ", ".join(f"{k}:{v}" for k, v in role_counter.items()) or "无"
    return "\n".join([
        f"[消息统计] 总数={len(messages)} | 角色分布={role_summary}",
        *lines,
    ])


class MockOpenAIHandler(BaseHTTPRequestHandler):
    server_version = "MockOpenAI/1.0"

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                return {"_raw": body}
            return body
        except Exception:
            return {"_raw_text": raw.decode("utf-8", errors="replace")}

    def _dump_request(self, payload: dict[str, Any]) -> None:
        logs_dir: Path = self.server.logs_dir  # type: ignore[attr-defined]
        logs_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = logs_dir / f"request_{now}.json"

        wrapped = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "method": self.command,
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
            "payload": payload,
        }

        log_path.write_text(json.dumps(wrapped, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n" + "=" * 80)
        print(f"[收到请求] {self.command} {self.path}")
        print(f"[日志文件] {log_path}")

        model = payload.get("model", "")
        stream = payload.get("stream", False)
        print(f"[请求参数] model={model} stream={stream}")

        messages = payload.get("messages", [])
        if isinstance(messages, list):
            print(summarize_messages(messages))
        else:
            print(f"[消息统计] messages 非数组: {type(messages).__name__}")

        print("[原始JSON]")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("=" * 80 + "\n")

    def _json_response(self, status: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _stream_response(self, payload: dict[str, Any]) -> None:
        model_name = str(payload.get("model", "mock-model"))
        created = int(time.time())
        request_id = f"chatcmpl-mock-{created}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        chunks = [
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "mock"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " response"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]

        for chunk in chunks:
            line = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
            self.wfile.write(line)
            self.wfile.flush()

        self.wfile.write(b"data: [DONE]\\n\\n")
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json_body()
        self._dump_request(payload)

        if self.path not in VALID_PATHS:
            self._json_response(
                404,
                {
                    "error": {
                        "message": f"仅支持路径: {', '.join(sorted(VALID_PATHS))}",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        stream = bool(payload.get("stream", False))

        if stream:
            self._stream_response(payload)
            return

        model_name = str(payload.get("model", "mock-model"))
        created = int(time.time())

        response = {
            "id": f"chatcmpl-mock-{created}",
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "mock response",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 2,
                "total_tokens": 102,
            },
        }
        self._json_response(200, response)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 关闭BaseHTTPRequestHandler默认访问日志，避免刷屏
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地伪 OpenAI 接口服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=10030, help="监听端口，默认 10030")
    parser.add_argument(
        "--logs-dir",
        default="./mock_openai_logs",
        help="原始请求落盘目录，默认 ./mock_openai_logs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logs_dir = Path(os.path.abspath(args.logs_dir))

    server = ThreadingHTTPServer((args.host, args.port), MockOpenAIHandler)
    server.logs_dir = logs_dir  # type: ignore[attr-defined]

    print(f"[启动] Mock OpenAI 监听 http://{args.host}:{args.port}")
    print(f"[落盘] 请求日志目录: {logs_dir}")
    print("[支持] POST /chat/completions 和 /v1/chat/completions")
    print("[停止] Ctrl+C")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[退出] 已停止监听")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
