"""
Claude Bridge — connects OpenClaw to Claude Code CLI
Receives jobs via HTTP, gates them behind Telegram approval, runs claude via SSH
on the VPS host (using Max subscription OAuth) or falls back to local API key.
"""
import asyncio
import base64
import os
import stat
import subprocess
import tempfile
import uuid
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="Claude Bridge", version="1.1.0")

# Config from environment
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
BRIDGE_TELEGRAM_POLL = os.environ.get("BRIDGE_TELEGRAM_POLL", "false").lower() == "true"
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")
REQUIRE_APPROVAL = os.environ.get("REQUIRE_APPROVAL", "true").lower() == "true"
MAX_BUDGET_USD = float(os.environ.get("CLAUDE_MAX_BUDGET_USD", "1.0"))
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")

# SSH / VPS config — when set, jobs run on the host via SSH (Max subscription)
VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_SSH_KEY_B64 = os.environ.get("VPS_SSH_KEY_B64", "")  # base64-encoded private key
VPS_WORKSPACE = os.environ.get("VPS_WORKSPACE", "/root/claude-workspace")

# Fallback API key (used only if VPS_HOST not set)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# In-memory job store (sufficient for personal use)
jobs: dict[str, dict] = {}

# SSH key temp file path (set at startup)
_ssh_key_file: Optional[str] = None


# ─── SSH key setup ─────────────────────────────────────────────────────────────

def _setup_ssh_key() -> Optional[str]:
    """Write the SSH key from env var to a temp file. Returns the path."""
    if not VPS_SSH_KEY_B64:
        return None
    try:
        key_bytes = base64.b64decode(VPS_SSH_KEY_B64)
        fd, path = tempfile.mkstemp(prefix="bridge_ssh_", suffix=".key")
        os.write(fd, key_bytes)
        os.close(fd)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600
        return path
    except Exception as e:
        print(f"[bridge] WARNING: Could not set up SSH key: {e}")
        return None


# ─── Models ───────────────────────────────────────────────────────────────────

class CreateJobRequest(BaseModel):
    task: str
    context: Optional[str] = None
    auto_approve: bool = False


class JobResponse(BaseModel):
    id: str
    status: str
    task: str
    created_at: str
    output: Optional[str] = None
    error: Optional[str] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def tg(text: str, reply_markup: dict = None):
    """Send message to Telegram."""
    async with httpx.AsyncClient() as client:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )


def run_claude(task: str, context: Optional[str] = None) -> tuple[bool, str]:
    """Run claude. Uses SSH to VPS host if configured, otherwise local API key."""
    prompt = task
    if context:
        prompt = f"{context}\n\n---\n\n{prompt}"

    if VPS_HOST and _ssh_key_file:
        return _run_via_ssh(prompt)
    elif ANTHROPIC_API_KEY:
        return _run_local(prompt)
    else:
        return False, "No execution backend configured (set VPS_HOST+VPS_SSH_KEY_B64 or ANTHROPIC_API_KEY)"


def _parse_model_tier(prompt: str) -> tuple[str, float, str]:
    """
    Parse optional model tier prefix from prompt.
    Supports: [haiku], [sonnet], [opus] at start of task.
    Returns (cleaned_prompt, budget_usd, model_flag).
    """
    import re
    tiers = {
        "haiku":  (1.00, "claude-haiku-4-5-20251001"),
        "sonnet": (MAX_BUDGET_USD, "claude-sonnet-4-6"),
        "opus":   (10.00, "claude-opus-4-6"),
    }
    m = re.match(r"^\[(haiku|sonnet|opus)\]\s*", prompt, re.IGNORECASE)
    if m:
        tier = m.group(1).lower()
        budget, model = tiers[tier]
        cleaned = prompt[m.end():]
        return cleaned, budget, model
    # Default: sonnet at configured budget
    return prompt, MAX_BUDGET_USD, "claude-sonnet-4-6"


def _run_via_ssh(prompt: str) -> tuple[bool, str]:
    """Run claude --print on the VPS host via SSH using the Max OAuth session."""
    if not _ssh_key_file:
        return False, "SSH key not configured"

    # Parse model tier prefix
    clean_prompt, budget, model = _parse_model_tier(prompt)

    # Encode the entire Python script (which contains the base64-encoded task)
    # This avoids any shell escaping issues on the remote side.
    task_b64 = base64.b64encode(clean_prompt.encode()).decode()

    py_script = (
        "import subprocess,sys,base64,os;"
        f"task=base64.b64decode(b'{task_b64}').decode();"
        f"r=subprocess.run(['claude','--print','--max-budget-usd','{budget}',task],"
        f"capture_output=True,text=True,timeout=290,"
        f"cwd='{VPS_WORKSPACE}',env={{**os.environ}});"
        "sys.stdout.write(r.stdout);"
        "sys.stderr.write(r.stderr);"
        "sys.exit(r.returncode)"
    )
    py_b64 = base64.b64encode(py_script.encode()).decode()

    ssh_cmd = f"python3 -c \"import base64;exec(base64.b64decode(b'{py_b64}').decode())\""

    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", _ssh_key_file,
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=60",
                "-o", "ServerAliveCountMax=5",
                f"root@{VPS_HOST}",
                ssh_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=310,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            err = result.stderr.strip() or f"Exit code {result.returncode}"
            return False, err
    except subprocess.TimeoutExpired:
        return False, "Timed out after 5 minutes"
    except Exception as e:
        return False, str(e)


def _run_local(prompt: str) -> tuple[bool, str]:
    """Fallback: run claude --print locally using ANTHROPIC_API_KEY."""
    env = {**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY}
    try:
        result = subprocess.run(
            ["claude", "--print", "--max-budget-usd", str(MAX_BUDGET_USD), prompt],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=WORKSPACE,
            env=env,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip() or f"Exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, "Timed out after 5 minutes"
    except FileNotFoundError:
        return False, "claude CLI not found"
    except Exception as e:
        return False, str(e)


def check_auth(authorization: Optional[str]):
    if BRIDGE_SECRET and authorization != f"Bearer {BRIDGE_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def health():
    backend = "ssh" if (VPS_HOST and _ssh_key_file) else ("api-key" if ANTHROPIC_API_KEY else "none")
    return {"status": "ok", "jobs": len(jobs), "backend": backend}


@app.post("/jobs", response_model=JobResponse)
async def create_job(
    req: CreateJobRequest,
    authorization: Optional[str] = Header(None),
):
    check_auth(authorization)

    job_id = str(uuid.uuid4())[:8]
    job = {
        "id": job_id,
        "task": req.task,
        "context": req.context,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "output": None,
        "error": None,
    }
    jobs[job_id] = job

    if req.auto_approve or not REQUIRE_APPROVAL:
        asyncio.create_task(_run_job(job_id))
        job["status"] = "running"
    else:
        asyncio.create_task(_request_approval(job_id))

    return JobResponse(**job)


@app.post("/jobs/{job_id}/approve")
async def approve_job(job_id: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Job is {job['status']}, not pending")
    job["status"] = "running"
    asyncio.create_task(_run_job(job_id))
    return {"ok": True, "message": "Job approved and queued"}


@app.post("/jobs/{job_id}/reject")
async def reject_job(job_id: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job["status"] = "rejected"
    await tg(f"❌ Job `{job_id}` rejected.\n\nTask: _{job['task'][:100]}_")
    return {"ok": True}


@app.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(**job)


@app.get("/jobs")
async def list_jobs(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return [JobResponse(**j) for j in list(jobs.values())[-20:]]


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    # Handle inline button taps
    if "callback_query" in data:
        cq = data["callback_query"]
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id == TELEGRAM_CHAT_ID:
            cq_data = cq.get("data", "")
            cq_id = cq.get("id")
            if cq_data.startswith("approve:"):
                job_id = cq_data.split(":", 1)[1]
                await _handle_telegram_command(f"/approve {job_id}")
            elif cq_data.startswith("reject:"):
                job_id = cq_data.split(":", 1)[1]
                await _handle_telegram_command(f"/reject {job_id}")
            # Acknowledge the callback to remove the spinner
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                    json={"callback_query_id": cq_id},
                    timeout=5,
                )
        return {"ok": True}

    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    if chat_id != TELEGRAM_CHAT_ID:
        return {"ok": True}

    await _handle_telegram_command(text)
    return {"ok": True}


# ─── Telegram polling ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    global _ssh_key_file
    _ssh_key_file = _setup_ssh_key()
    backend = "ssh" if _ssh_key_file else ("api-key" if ANTHROPIC_API_KEY else "NONE")
    print(f"[bridge] Backend: {backend}", flush=True)
    if VPS_HOST:
        print(f"[bridge] VPS host: {VPS_HOST}  workspace: {VPS_WORKSPACE}", flush=True)
    if BRIDGE_TELEGRAM_POLL:
        asyncio.create_task(_poll_telegram())


async def _poll_telegram():
    """Long-poll Telegram for /approve and /reject commands."""
    offset = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
                    timeout=35,
                )
                for update in r.json().get("result", []):
                    offset = update["update_id"] + 1
                    # Inline button callback
                    if "callback_query" in update:
                        cq = update["callback_query"]
                        if str(cq.get("message", {}).get("chat", {}).get("id", "")) == TELEGRAM_CHAT_ID:
                            cq_data = cq.get("data", "")
                            if cq_data.startswith("approve:"):
                                await _handle_telegram_command(f"/approve {cq_data.split(':',1)[1]}")
                            elif cq_data.startswith("reject:"):
                                await _handle_telegram_command(f"/reject {cq_data.split(':',1)[1]}")
                            await client.post(
                                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                                json={"callback_query_id": cq["id"]},
                                timeout=5,
                            )
                        continue
                    msg = update.get("message", {})
                    if str(msg.get("chat", {}).get("id", "")) == TELEGRAM_CHAT_ID:
                        await _handle_telegram_command(msg.get("text", "").strip())
            except Exception:
                await asyncio.sleep(5)


async def _handle_telegram_command(text: str):
    # Job creation — same prefixes as relay
    lower = text.lower()
    for prefix in ("run:", "claude:", "task:", "!run ", "!claude "):
        if lower.startswith(prefix):
            task = text[len(prefix):].strip()
            if task:
                job_id = str(uuid.uuid4())[:8]
                job = {
                    "id": job_id,
                    "task": task,
                    "context": None,
                    "status": "pending",
                    "created_at": datetime.utcnow().isoformat(),
                    "output": None,
                    "error": None,
                }
                jobs[job_id] = job
                asyncio.create_task(_request_approval(job_id))
            return

    if text.startswith("/approve "):
        job_id = text.split()[1]
        job = jobs.get(job_id)
        if job and job["status"] == "pending":
            job["status"] = "running"
            asyncio.create_task(_run_job(job_id))
            await tg(f"✅ Job `{job_id}` approved — running now...")
        else:
            await tg(f"Job `{job_id}` not found or not pending.")

    elif text.startswith("/reject "):
        job_id = text.split()[1]
        job = jobs.get(job_id)
        if job and job["status"] == "pending":
            job["status"] = "rejected"
            await tg(f"❌ Job `{job_id}` rejected.")
        else:
            await tg(f"Job `{job_id}` not found or not pending.")

    elif text == "/jobs":
        recent = list(jobs.values())[-5:]
        icons = {"pending": "⏳", "running": "🔄", "completed": "✅", "rejected": "❌", "failed": "💥"}
        lines = ["*Recent jobs:*"] + [
            f"{icons.get(j['status'], '?')} `{j['id']}` {j['status']} — {j['task'][:60]}"
            for j in recent
        ]
        await tg("\n".join(lines) if recent else "No jobs yet.")

    elif text == "/status":
        backend = "ssh" if (_ssh_key_file and VPS_HOST) else ("api-key" if ANTHROPIC_API_KEY else "none")
        pending = sum(1 for j in jobs.values() if j["status"] == "pending")
        running = sum(1 for j in jobs.values() if j["status"] == "running")
        await tg(f"🤖 Bridge running\nBackend: `{backend}`\nPending: {pending}  Running: {running}  Total: {len(jobs)}")


# ─── Background tasks ─────────────────────────────────────────────────────────

async def _request_approval(job_id: str):
    job = jobs[job_id]
    task_preview = job["task"][:200]
    await tg(
        f"📋 *New job queued* — `{job_id}`\n\n*Task:* {task_preview}",
        reply_markup={
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{job_id}"},
                {"text": "❌ Reject",  "callback_data": f"reject:{job_id}"},
            ]]
        }
    )


async def _run_job(job_id: str):
    job = jobs[job_id]
    await tg(f"🔄 Running job `{job_id}`...\n_{job['task'][:100]}_")

    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(None, run_claude, job["task"], job.get("context"))

    # Heartbeat: ping every 45s so you know it's still alive on long jobs
    elapsed = 0
    while not future.done():
        await asyncio.sleep(5)
        elapsed += 5
        if elapsed % 45 == 0:
            await tg(f"⏳ Job `{job_id}` still running... ({elapsed}s)")

    success, output = await future

    job["status"] = "completed" if success else "failed"
    job["output"] = output

    tg_output = output[:3000] + ("\n\n_(truncated)_" if len(output) > 3000 else "")
    icon = "✅" if success else "💥"
    await tg(f"{icon} Job `{job_id}` {'complete' if success else 'failed'}:\n\n{tg_output}")
