#!/usr/bin/env python3
"""Eval: a replayed signed webhook delivery must run the route script exactly once.

Builds the real ``WebhookAdapter`` app (aiohttp ``TestClient``) with one secret-configured route
whose pre-filter script appends a line to a counter file. Posts one valid signed delivery
(accepted, script runs once), then the byte-identical replay (duplicate, script runs zero more
times, no cron/dispatch), asserting each step with a printed PASS/FAIL line and exiting non-zero
on any failure.

Run standalone:

    python evals/webhook_auth/replayed_delivery_runs_script_once.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _sign(secret: str, msg_id: str, ts: str, body: bytes) -> str:
    """Standard Webhooks signature (same as evals/webhook_auth/standard_webhooks_ab.py)."""
    key = base64.b64decode(secret.removeprefix("whsec_")) if secret.startswith("whsec_") else secret.encode()
    digest = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _make_adapter(routes, rate_limit=5):
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes, "rate_limit": rate_limit}
    return WebhookAdapter(PlatformConfig(enabled=True, extra=extra))


def _create_app(adapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.read_text(encoding="utf-8").splitlines() if _)


async def main() -> int:
    sys.path.insert(0, os.getcwd())
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"PASS: {name}" if ok else f"FAIL: {name} {detail}")
        if not ok:
            failures.append(name)

    # A fresh HERMES_HOME holds the route script (run_route_script resolves scripts under
    # HERMES_HOME/scripts) and the counter file it appends to.
    hermes_home = Path(tempfile.mkdtemp(prefix="webhook-replay-"))
    (hermes_home / "scripts").mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(hermes_home)

    counter = hermes_home / "scripts" / "runs.txt"
    script_path = hermes_home / "scripts" / "count_run.py"
    script_path.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        f"open({str(counter)!r}, 'a').write('run\\n')\n"
        "print(json.dumps({}))\n",
        encoding="utf-8",
    )

    secret = "replay-test-secret"
    route_name = "replay-route"
    routes = {
        route_name: {
            "secret": secret,
            "script": "count_run.py",
            "prompt": "Event: {event}",
            "deliver": "log",
        }
    }
    adapter = _make_adapter(routes)
    adapter.handle_message = AsyncMock()  # no agent run; count dispatches instead

    app = _create_app(adapter)

    body = b'{"event_type":"invoice.paid","n":1}'
    msg_id = "deliv-replay-1"
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": _sign(secret, msg_id, ts, body),
    }

    async with TestClient(TestServer(app)) as cli:
        # 1) Valid signed delivery -> accepted, script runs exactly once.
        resp = await cli.post(f"/webhooks/{route_name}", data=body, headers=headers)
        text = await resp.text()
        check("first delivery accepted (202)", resp.status == 202, f"status={resp.status} body={text[:120]}")
        data = json.loads(text)
        check("first delivery status == accepted", data.get("status") == "accepted", f"body={text[:120]}")
        check("script ran exactly once", _count_lines(counter) == 1, f"count={_count_lines(counter)}")

        # 2) Byte-identical replay (same signature, same delivery id) -> duplicate, zero more runs.
        resp2 = await cli.post(f"/webhooks/{route_name}", data=body, headers=headers)
        text2 = await resp2.text()
        data2 = json.loads(text2)
        check("replay answered 200 duplicate", resp2.status == 200, f"status={resp2.status}")
        check("replay status == duplicate", data2.get("status") == "duplicate", f"body={text2[:120]}")
        check("replay ran script 0 more times", _count_lines(counter) == 1, f"count={_count_lines(counter)}")
        check("replay dispatched no agent run", adapter.handle_message.await_count == 1,
              f"dispatch_count={adapter.handle_message.await_count}")

    await adapter.disconnect()
    if failures:
        print(f"\n{len(failures)} assertion(s) failed")
        return 1
    print("\nAll assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
