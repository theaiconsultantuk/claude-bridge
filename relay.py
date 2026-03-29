"""
OpenClaw Relay — bridges OpenClaw Telegram messages to the Claude Bridge API.

Connects to OpenClaw WebSocket, polls chat history every 3 seconds, and routes:
  - "run: <task>"    → POST /jobs   (creates a Claude Code job)
  - "/approve <id>"  → POST /jobs/<id>/approve
  - "/reject <id>"   → POST /jobs/<id>/reject

The relay runs alongside the bridge container on the same Docker network.
"""
import asyncio
import base64
import json
import logging
import os
import time

import httpx
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [relay] %(message)s")
log = logging.getLogger(__name__)

# OpenClaw connection
OC_URL = os.environ["OPENCLAW_URL"]  # e.g. wss://openclaw.theaiconsultant.co.uk
OC_USER = os.environ["OPENCLAW_USER"]
OC_PASS = os.environ["OPENCLAW_PASS"]
OC_TOKEN = os.environ["OPENCLAW_TOKEN"]
OC_SESSION = os.environ.get("OPENCLAW_SESSION", "agent:main:main")

# Bridge connection
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://claude-bridge:8765")
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")

# Prefixes that trigger job creation (case-insensitive)
JOB_PREFIXES = ("run:", "claude:", "task:", "!run ", "!claude ")

POLL_INTERVAL = 3  # seconds between chat history polls

# Track last processed message timestamp
last_ts: int = int(time.time() * 1000)  # start from now, ignore history


def basic_auth() -> str:
    return base64.b64encode(f"{OC_USER}:{OC_PASS}".encode()).decode()


def bridge_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if BRIDGE_SECRET:
        h["Authorization"] = f"Bearer {BRIDGE_SECRET}"
    return h


def extract_text(msg: dict) -> str:
    """Extract plain text from a chat message."""
    for block in msg.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", "").strip()
    return ""


async def rpc(ws, method: str, params: dict = {}, rid: str = None):
    rid = rid or method
    await ws.send(json.dumps({"type": "req", "id": rid, "method": method, "params": params}))
    for _ in range(30):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if msg.get("id") == rid:
            return msg
    return None


async def connect_openclaw():
    """Establish and authenticate an OpenClaw WebSocket connection."""
    headers = {"Authorization": f"Basic {basic_auth()}"}
    ws = await websockets.connect(OC_URL, additional_headers=headers)

    # Consume challenge
    await asyncio.wait_for(ws.recv(), timeout=5)

    r = await rpc(ws, "connect", {
        "minProtocol": 1,
        "maxProtocol": 100,
        "client": {
            "id": "gateway-client",
            "mode": "backend",
            "version": "1.0.0",
            "platform": "linux",
        },
        "auth": {"token": OC_TOKEN},
    }, "conn")

    if not (r and r.get("ok")):
        await ws.close()
        raise RuntimeError(f"OpenClaw auth failed: {r}")

    log.info("Connected to OpenClaw (protocol %s)", r["payload"].get("protocol"))
    return ws


async def post_bridge(path: str, body: dict = None):
    """POST to the bridge API."""
    async with httpx.AsyncClient() as client:
        url = f"{BRIDGE_URL}{path}"
        r = await client.post(url, json=body or {}, headers=bridge_headers(), timeout=10)
        r.raise_for_status()
        return r.json()


async def handle_message(text: str):
    """Route a user message to the appropriate bridge endpoint."""
    global last_ts

    lower = text.lower()

    # Job creation
    for prefix in JOB_PREFIXES:
        if lower.startswith(prefix):
            task = text[len(prefix):].strip()
            if not task:
                return
            log.info("Creating job: %s", task[:80])
            result = await post_bridge("/jobs", {"task": task})
            log.info("Job created: %s", result.get("id"))
            return

    # Approval / rejection
    parts = text.split()
    if len(parts) == 2 and parts[0].lower() in ("/approve", "!approve"):
        job_id = parts[1]
        log.info("Approving job: %s", job_id)
        await post_bridge(f"/jobs/{job_id}/approve")
        return

    if len(parts) == 2 and parts[0].lower() in ("/reject", "!reject"):
        job_id = parts[1]
        log.info("Rejecting job: %s", job_id)
        await post_bridge(f"/jobs/{job_id}/reject")
        return


async def poll_loop(ws):
    """Poll chat history and process new user messages."""
    global last_ts

    r = await rpc(ws, "chat.history", {"sessionKey": OC_SESSION, "limit": 10}, "ch")
    if not (r and r.get("ok")):
        log.warning("chat.history failed: %s", r)
        return

    messages = r["payload"].get("messages", [])

    for msg in messages:
        if msg.get("role") != "user":
            continue

        ts = msg.get("timestamp", 0)
        if ts <= last_ts:
            continue

        last_ts = ts
        text = extract_text(msg)
        if text:
            log.info("New user message [ts=%s]: %s", ts, text[:100])
            try:
                await handle_message(text)
            except Exception as e:
                log.error("Error handling message: %s", e)


async def run():
    """Main loop with reconnect."""
    global last_ts

    log.info("Relay starting. Bridge: %s  OpenClaw session: %s", BRIDGE_URL, OC_SESSION)
    log.info("Watching for prefixes: %s  and /approve /reject", JOB_PREFIXES)

    while True:
        try:
            ws = await connect_openclaw()
            try:
                while True:
                    await poll_loop(ws)
                    await asyncio.sleep(POLL_INTERVAL)
            finally:
                await ws.close()
        except Exception as e:
            log.error("Connection error: %s — reconnecting in 10s", e)
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(run())
