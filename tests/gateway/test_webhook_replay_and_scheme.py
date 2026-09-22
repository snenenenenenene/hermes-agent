"""Regression tests for webhook replay idempotency and per-route signature scheme pinning.

Covers three fixes in ``gateway/platforms/webhook.py``:

1. A duplicate delivery id is checked BEFORE the route pre-filter script runs, so a byte-identical
   replay causes zero side effects (the script does not run again).
2. A script drop (``{"status": "ignored", "reason": "script"}``) un-records the delivery id, so a
   genuine retry after a drop is still processed rather than swallowed as a duplicate.
3. A per-route ``scheme`` field pins the accepted signature scheme: a route pinned to ``svix``
   refuses a legacy body-only HMAC (``X-Webhook-Signature``) with 401, and the ``stripe`` scheme
   validates Stripe's ``Stripe-Signature: t=<ts>,v1=<hex>`` (and rejects wrong key, tampered body,
   missing ``v1=``, and stale timestamp).
"""

import base64
import hashlib
import hmac
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes, "rate_limit": 30}
    extra.update(extra_kw)
    return WebhookAdapter(PlatformConfig(enabled=True, extra=extra))


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _generic_signature(body: bytes, secret: str) -> str:
    """Legacy body-only HMAC (X-Webhook-Signature) — the weaker scheme a pinned route must refuse."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _svix_signature(body: bytes, secret: str, msg_id: str, timestamp: str) -> str:
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    return "v1," + base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode()


def _stripe_signature(body: bytes, secret: str, timestamp: str) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


class TestDuplicateBeforeScript:
    @pytest.mark.asyncio
    async def test_duplicate_delivery_does_not_rerun_script(self, tmp_path, monkeypatch):
        """A byte-identical replay returns 200 duplicate and runs the script zero more times."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "scripts").mkdir()
        counter = tmp_path / "runs.txt"
        (tmp_path / "scripts" / "count.py").write_text(
            "import json, sys\n"
            "sys.stdin.read()\n"
            f"open({str(counter)!r}, 'a').write('run\\n')\n"
            "print(json.dumps({}))\n",
            encoding="utf-8",
        )
        routes = {"replay": {"secret": _INSECURE_NO_AUTH, "script": "count.py", "prompt": "x"}}
        adapter = _make_adapter(routes)
        adapter.handle_message = AsyncMock()
        app = _create_app(adapter)
        headers = {"X-GitHub-Delivery": "dup-1", "Content-Type": "application/json"}

        async with TestClient(TestServer(app)) as cli:
            first = await cli.post("/webhooks/replay", json={"n": 1}, headers=headers)
            assert first.status == 202
            assert counter.read_text() == "run\n"

            replay = await cli.post("/webhooks/replay", json={"n": 1}, headers=headers)
            data = await replay.json()
            assert replay.status == 200
            assert data["status"] == "duplicate"
            assert counter.read_text() == "run\n"  # script did not run a second time
        assert adapter.handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_script_drop_unrecords_delivery_so_retry_reprocesses(self, tmp_path, monkeypatch):
        """A drop is not a completed delivery: the same delivery id is processed again on retry."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "scripts").mkdir()
        counter = tmp_path / "runs.txt"
        (tmp_path / "scripts" / "drop.py").write_text(
            "import sys\n"
            "sys.stdin.read()\n"
            f"open({str(counter)!r}, 'a').write('run\\n')\n"
            "print('[SILENT]')\n",
            encoding="utf-8",
        )
        routes = {"drop": {"secret": _INSECURE_NO_AUTH, "script": "drop.py", "prompt": "x"}}
        adapter = _make_adapter(routes)
        adapter.handle_message = AsyncMock()
        app = _create_app(adapter)
        headers = {"X-GitHub-Delivery": "drop-1", "Content-Type": "application/json"}

        async with TestClient(TestServer(app)) as cli:
            first = await cli.post("/webhooks/drop", json={"n": 1}, headers=headers)
            data = await first.json()
            assert data["status"] == "ignored"
            assert data["reason"] == "script"
            assert counter.read_text() == "run\n"

            # Same delivery id again: must NOT be a duplicate — the script must run again.
            retry = await cli.post("/webhooks/drop", json={"n": 1}, headers=headers)
            data = await retry.json()
            assert data["status"] == "ignored"
            assert data["reason"] == "script"
            assert counter.read_text() == "run\nrun\n"
        adapter.handle_message.assert_not_called()


class TestSchemePinning:
    @pytest.mark.asyncio
    async def test_pinned_scheme_refuses_legacy_body_only_hmac(self):
        """A route pinned to ``svix`` must 401 a valid legacy body-only HMAC of the same secret."""
        secret = "pin-secret"
        body = b'{"event_type":"invoice.paid"}'
        routes = {"pinned": {"secret": secret, "scheme": "svix", "prompt": "x"}}
        adapter = _make_adapter(routes)
        adapter.handle_message = AsyncMock()
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/pinned",
                data=body,
                headers={"X-Webhook-Signature": _generic_signature(body, secret)},
            )
            assert resp.status == 401
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_pinned_scheme_still_accepts_its_own_scheme(self):
        """The pinned scheme itself validates normally."""
        secret = "pin-secret"
        body = b'{"event_type":"invoice.paid"}'
        ts = str(int(time.time()))
        routes = {"pinned": {"secret": secret, "scheme": "svix", "prompt": "x"}}
        adapter = _make_adapter(routes)
        adapter.handle_message = AsyncMock()
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/pinned",
                data=body,
                headers={
                    "svix-id": "msg-1",
                    "svix-timestamp": ts,
                    "svix-signature": _svix_signature(body, secret, "msg-1", ts),
                },
            )
            assert resp.status == 202
            data = await resp.json()
            assert data["status"] == "accepted"


class TestStripeScheme:
    SECRET = "stripe-whsec-secret"
    BODY = b'{"type":"payment_intent.succeeded"}'

    @staticmethod
    def _route() -> dict:
        return {"stripe": {"secret": TestStripeScheme.SECRET, "scheme": "stripe", "prompt": "x"}}

    @pytest.mark.asyncio
    async def test_valid_stripe_signature_accepted(self):
        ts = str(int(time.time()))
        sig = _stripe_signature(self.BODY, self.SECRET, ts)
        adapter = _make_adapter(self._route())
        adapter.handle_message = AsyncMock()
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/stripe",
                data=self.BODY,
                headers={"Stripe-Signature": f"t={ts},v1={sig}"},
            )
            assert resp.status == 202
            data = await resp.json()
            assert data["status"] == "accepted"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["wrong_key", "tampered_body", "missing_v1", "stale_timestamp"])
    async def test_stripe_signature_rejected(self, name):
        ts = str(int(time.time()))
        stale_ts = str(int(time.time()) - 600)
        body = self.BODY
        secret = self.SECRET

        if name == "wrong_key":
            header = f"t={ts},v1={_stripe_signature(body, 'wrong-secret', ts)}"
            payload = body
        elif name == "tampered_body":
            # signature computed over the correct body, but a different body is sent
            header = f"t={ts},v1={_stripe_signature(body, secret, ts)}"
            payload = b'{"type":"tampered"}'
        elif name == "missing_v1":
            header = f"t={ts}"
            payload = body
        else:  # stale_timestamp: a correct signature for a timestamp outside the replay window
            header = f"t={stale_ts},v1={_stripe_signature(body, secret, stale_ts)}"
            payload = body

        adapter = _make_adapter(self._route())
        adapter.handle_message = AsyncMock()
        app = _create_app(adapter)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/webhooks/stripe",
                data=payload,
                headers={"Stripe-Signature": header},
            )
            assert resp.status == 401
        adapter.handle_message.assert_not_called()
