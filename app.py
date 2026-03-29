"""
Claude Bridge — connects OpenClaw to Claude Code CLI
Receives jobs via HTTP, gates them behind Telegram approval, runs claude --print
"""
import asyncio
import os
import subprocess
import uuid
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="Claude Bridge", version="1.0.0")

# Config from environment
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
REQUIRE_APPROVAL = os.environ.get("REQUIRE_APPROVAL", "true").lower() == "true"
MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "4000"))
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")

# In-memory job store (sufficient for personal use)
jobs: dict[str, dict] = {}


# ─── Models ───────────────────────────────────────────────────────────────────

class CreateJobRequest(BaseModel):
    task: str
    context: Optional[str] = None
    auto_approve: bool = False  # skip approval for low-risk tasks


class JobResponse(BaseModel):
    id: str
    status: str
    task: str
    created_at: str
    output: Optional[str] = None
    error: Optional[str] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def tg(text: str):
    """Send message to Telegram."""
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )


def run_claude(task: str, context: Optional[str] = None) -> tuple[bool, str]:
    """Run claude --print with the given task. Returns (success, output)."""
    prompt = task
    if context:
        prompt = f"{context}\n\n---\n\n{prompt}"

    env = {**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY}

    try:
        result = subprocess.run(
            ["claude", "--print", "--max-tokens", str(MAX_TOKENS), prompt],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
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
        return False, "claude CLI not found — is @anthropic-ai/claude-code installed?"
    except Exception as e:
        return False, str(e)


def check_auth(authorization: Optional[str]):
    if BRIDGE_SECRET and authorization != f"Bearer {BRIDGE_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def health():
    return {"status": "ok", "jobs": len(jobs)}


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
        # Run immediately
        asyncio.create_task(_run_job(job_id))
        job["status"] = "running"
    else:
        # Request Telegram approval
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


# Telegram webhook for /approve and /reject commands
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    message = data.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    # Only accept from authorised chat
    if chat_id != TELEGRAM_CHAT_ID:
        return {"ok": True}

    if text.startswith("/approve "):
        job_id = text.split()[1]
        job = jobs.get(job_id)
        if job and job["status"] == "pending":
            job["status"] = "running"
            asyncio.create_task(_run_job(job_id))
            await tg(f"✅ Job `{job_id}` approved. Running now...")
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
        pending = [j for j in jobs.values() if j["status"] == "pending"]
        recent = list(jobs.values())[-5:]
        lines = ["*Recent jobs:*"]
        for j in recent:
            icon = {"pending": "⏳", "running": "🔄", "completed": "✅", "rejected": "❌", "failed": "💥"}.get(j["status"], "?")
            lines.append(f"{icon} `{j['id']}` {j['status']} — {j['task'][:60]}")
        await tg("\n".join(lines) or "No jobs yet.")

    return {"ok": True}


# ─── Telegram polling (no public URL needed) ──────────────────────────────────

@app.on_event("startup")
async def start_telegram_polling():
    asyncio.create_task(_poll_telegram())


async def _poll_telegram():
    """Long-poll Telegram for /approve and /reject commands."""
    offset = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                    timeout=35,
                )
                updates = r.json().get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    text = message.get("text", "").strip()
                    if chat_id == TELEGRAM_CHAT_ID:
                        await _handle_telegram_command(text)
            except Exception:
                await asyncio.sleep(5)


async def _handle_telegram_command(text: str):
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


# ─── Background tasks ─────────────────────────────────────────────────────────

async def _request_approval(job_id: str):
    job = jobs[job_id]
    task_preview = job["task"][:200]
    await tg(
        f"📋 *New job queued* — `{job_id}`\n\n"
        f"*Task:* {task_preview}\n\n"
        f"Reply with:\n"
        f"`/approve {job_id}` — run it\n"
        f"`/reject {job_id}` — discard"
    )


async def _run_job(job_id: str):
    job = jobs[job_id]
    await tg(f"🔄 Running job `{job_id}`...\n_{job['task'][:100]}_")

    loop = asyncio.get_event_loop()
    success, output = await loop.run_in_executor(
        None, run_claude, job["task"], job.get("context")
    )

    job["status"] = "completed" if success else "failed"
    job["output"] = output

    # Trim output for Telegram (4096 char limit)
    tg_output = output[:3000] + ("\n\n_(truncated)_" if len(output) > 3000 else "")
    icon = "✅" if success else "💥"
    await tg(f"{icon} Job `{job_id}` {'complete' if success else 'failed'}:\n\n{tg_output}")
