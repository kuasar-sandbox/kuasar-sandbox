#!/usr/bin/env python3
"""
agent_llm_proxy.py — High-Throughput Local LLM Mock Proxy for Autonomous Agents (OpenClaw)

Implements OpenAI (/v1/chat/completions) and Anthropic (/v1/messages) APIs.
Drives OpenClaw agents through a deterministic, realistic multi-turn tool-use
lifecycle (workspace discovery -> AST analysis -> file mutation -> unit testing)
without hitting external rate limits (HTTP 429) or token billing.

Supports configurable thinking/generation delays to benchmark in-memory VM
pause/resume and balloon deflation while the agent waits for LLM responses.
"""

import argparse
import json
import logging
import re
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agent_llm_proxy")

# Global metrics
METRICS = {
    "requests_total": 0,
    "tokens_total": 0,
    "active_conns": 0,
    "turn_1_count": 0,
    "turn_2_count": 0,
    "turn_3_count": 0,
    "turn_4_count": 0,
    "start_time": time.time(),
}
METRICS_LOCK = threading.Lock()


class ProxyConfig:
    port = 8088
    host = "0.0.0.0"
    delay_s = 0.0  # Simulated LLM generation delay in seconds
    repo_path = "/workspace/repo"


def generate_openai_response(messages, tools, req_id):
    """
    Evaluates agent history and returns the next tool call or final response
    in OpenAI chat completion format.
    """
    msg_count = len(messages)
    # Determine the turn based on message history
    has_tool_results = any(m.get("role") == "tool" for m in messages)
    tool_messages = [m for m in messages if m.get("role") == "tool"]

    if len(tool_messages) == 0:
        # Turn 1: Workspace discovery
        with METRICS_LOCK:
            METRICS["turn_1_count"] += 1
        tool_call = {
            "id": f"call_{req_id}_1",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({
                    "command": f"cd {ProxyConfig.repo_path} && git status && git log -n 1 --oneline && ls -la src/ tests/"
                })
            }
        }
        content = None
        tool_calls = [tool_call]
        finish_reason = "tool_calls"

    elif len(tool_messages) == 1:
        # Turn 2: Run test suite and check code
        with METRICS_LOCK:
            METRICS["turn_2_count"] += 1
        tool_call = {
            "id": f"call_{req_id}_2",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({
                    "command": f"cd {ProxyConfig.repo_path} && python3 -m unittest discover tests/ && cat src/calc.py"
                })
            }
        }
        content = None
        tool_calls = [tool_call]
        finish_reason = "tool_calls"

    elif len(tool_messages) == 2:
        # Turn 3: Mutate codebase, add power() function, and verify
        with METRICS_LOCK:
            METRICS["turn_3_count"] += 1
        tool_call = {
            "id": f"call_{req_id}_3",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({
                    "command": (
                        f"cd {ProxyConfig.repo_path} && "
                        "echo 'def power(a, b): return a ** b' >> src/calc.py && "
                        "python3 -c 'import src.calc as c; assert c.power(2, 10) == 1024, \"power failed\"; print(\"power OK\")' && "
                        "git diff src/calc.py"
                    )
                })
            }
        }
        content = None
        tool_calls = [tool_call]
        finish_reason = "tool_calls"

    else:
        # Turn 4: Final completion message
        with METRICS_LOCK:
            METRICS["turn_4_count"] += 1
        content = (
            "Task complete. I inspected the repository, executed all unit tests, "
            "implemented the power(a, b) function in src/calc.py, and verified the changes via git diff."
        )
        tool_calls = None
        finish_reason = "stop"

    msg_obj = {"role": "assistant"}
    if content:
        msg_obj["content"] = content
    if tool_calls:
        msg_obj["tool_calls"] = tool_calls

    resp = {
        "id": f"chatcmpl-{req_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "openclaw-mock-gpt4",
        "choices": [{
            "index": 0,
            "message": msg_obj,
            "finish_reason": finish_reason
        }],
        "usage": {
            "prompt_tokens": 120 * (len(tool_messages) + 1),
            "completion_tokens": 64,
            "total_tokens": 120 * (len(tool_messages) + 1) + 64
        }
    }
    return resp


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class MockLLMHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default HTTP access logs for high-throughput benchmarking
        pass

    def do_GET(self):
        if self.path in ("/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with METRICS_LOCK:
                uptime = time.time() - METRICS["start_time"]
                data = {
                    "status": "ok",
                    "uptime_s": uptime,
                    "metrics": dict(METRICS),
                    "qps": METRICS["requests_total"] / max(uptime, 1e-3)
                }
            self.wfile.write(json.dumps(data, indent=2).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        with METRICS_LOCK:
            METRICS["requests_total"] += 1
            METRICS["active_conns"] += 1
            req_id = METRICS["requests_total"]

        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(body)

            # Apply simulated thinking / inference delay if configured
            if ProxyConfig.delay_s > 0:
                time.sleep(ProxyConfig.delay_s)

            if self.path.endswith("/chat/completions"):
                messages = payload.get("messages", [])
                tools = payload.get("tools", [])
                resp_data = generate_openai_response(messages, tools, req_id)
            elif self.path.endswith("/messages"):  # Anthropic format
                messages = payload.get("messages", [])
                resp_data = {
                    "id": f"msg_{req_id}",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "OK"}],
                    "model": "claude-3-5-sonnet",
                    "usage": {"input_tokens": 100, "output_tokens": 50}
                }
            else:
                resp_data = {"status": "ok", "id": req_id}

            with METRICS_LOCK:
                METRICS["tokens_total"] += resp_data.get("usage", {}).get("total_tokens", 100)

            resp_bytes = json.dumps(resp_data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)

        except Exception as e:
            logger.error(f"Error handling request: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        finally:
            with METRICS_LOCK:
                METRICS["active_conns"] -= 1


def main():
    parser = argparse.ArgumentParser(description="High-Throughput Agent Mock LLM Proxy")
    parser.add_argument("--port", type=int, default=8088, help="Port to listen on (default 8088)")
    parser.add_argument("--host", default="0.0.0.0", help="Host address (default 0.0.0.0)")
    parser.add_argument("--delay", type=float, default=0.0, help="Simulated generation delay in seconds")
    parser.add_argument("--repo-path", default="/workspace/repo", help="Path to repo in sandbox")
    args = parser.parse_args()

    ProxyConfig.port = args.port
    ProxyConfig.host = args.host
    ProxyConfig.delay_s = args.delay
    ProxyConfig.repo_path = args.repo_path

    server = ThreadingHTTPServer((ProxyConfig.host, ProxyConfig.port), MockLLMHandler)
    logger.info(f"Agent LLM Mock Proxy running on http://{ProxyConfig.host}:{ProxyConfig.port} (delay={ProxyConfig.delay_s}s)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down proxy server...")
        server.shutdown()


if __name__ == "__main__":
    main()
