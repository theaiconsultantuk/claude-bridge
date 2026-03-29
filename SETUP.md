# Bridge Setup Guide

## What this does

OpenClaw receives a Telegram message → calls this bridge → sends you a Telegram
approval request → you reply `/approve <id>` → Claude Code runs the task →
output sent back to Telegram.

```
You → Telegram → OpenClaw → POST /jobs → Bridge → Telegram approval
                                                         ↓ /approve
                                                   claude --print
                                                         ↓
                                               Output → Telegram
```

---

## Step 1 — SSH to VPS and do one-time setup

```bash
ssh root@141.136.42.179

# While here, also clean up the orphaned security-cron container:
docker ps -a | grep security-cron
docker rm $(docker ps -aqf "name=security-cron") 2>/dev/null || true

# Verify node/npm available (Coolify uses Docker so host doesn't need it)
docker --version
```

---

## Step 2 — Push bridge code to a Git repo

The bridge needs to be in a Git repo so Coolify can deploy it.

```bash
# On your local machine:
cd C:/tmp/claude/bridge
git init
git add .
git commit -m "Initial claude bridge service"
git remote add origin https://github.com/YOUR_USERNAME/claude-bridge.git
git push -u origin main
```

Or use a private repo — Coolify can use a deploy key.

---

## Step 3 — Add to Coolify

### Option A: Add as a new Coolify service (recommended)

1. Coolify UI → New Resource → Docker Compose → paste docker-compose.bridge.yml
2. Add env vars:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = 520171715
   - `ANTHROPIC_API_KEY` = your key
   - `BRIDGE_SECRET` = generate a random 32-char string
   - `REQUIRE_APPROVAL` = true
3. Deploy

### Option B: Add to existing OpenClaw compose

Add the `claude-bridge` service block to the OpenClaw docker-compose in Coolify.
Note: both services need to be on the same Docker network for OpenClaw to call
`http://claude-bridge:8765`.

---

## Step 4 — Set up Telegram webhook

After deploying, register the webhook so Telegram sends messages to the bridge:

```bash
# Replace TOKEN and YOUR_BRIDGE_URL
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://openclaw.theaiconsultant.co.uk/telegram-bridge/webhook"
```

**Problem:** The bridge isn't publicly exposed (good for security). You have two options:

**Option A (recommended): Poll instead of webhook**
Add a polling loop to the bridge — no public URL needed.
Replace the webhook endpoint with a background task that calls:
`https://api.telegram.org/bot{TOKEN}/getUpdates`

**Option B: Expose via Traefik**
Add a Traefik label to expose the bridge at `https://bridge.theaiconsultant.co.uk`
(or a subpath of the OpenClaw domain).

The polling approach is simpler for personal use.

---

## Step 5 — Configure OpenClaw to use the bridge

In your Telegram chat with @Pauly_CB_Bot, you can trigger jobs via:

```
Send me a task and I'll run it via Claude Code:
run: review the n8n deployment health and suggest improvements
```

Or configure an OpenClaw command/hook to automatically route certain requests
to the bridge via HTTP call.

---

## Step 6 — Test

```
You → Telegram: "run: ls /workspace"
Bot → Telegram: "📋 New job queued: `abc123` — run: ls /workspace
                  /approve abc123 or /reject abc123"
You → Telegram: "/approve abc123"
Bot → Telegram: "✅ Job complete: [output]"
```

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | — | Your Telegram user ID |
| `ANTHROPIC_API_KEY` | Yes | — | Claude API key |
| `BRIDGE_SECRET` | No | (open) | Bearer token for OpenClaw → Bridge auth |
| `REQUIRE_APPROVAL` | No | true | Set false to auto-run all jobs |
| `CLAUDE_MAX_TOKENS` | No | 4000 | Max output tokens per job |
| `WORKSPACE` | No | /workspace | Directory Claude Code operates in |

---

## Polling mode (replaces webhook for private deployments)

Add this to app.py to use polling instead of webhook:

```python
@app.on_event("startup")
async def start_polling():
    asyncio.create_task(_poll_telegram())

async def _poll_telegram():
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
                        await _handle_command(text)
            except Exception:
                await asyncio.sleep(5)
```
