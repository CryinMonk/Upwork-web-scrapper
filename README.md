# 🤖 Upwork Job Scraper Bot

A Discord bot that monitors Upwork for new job postings matching your keywords and delivers rich, real-time alerts directly to your Discord channels — with full job details, client history, and a direct apply link.

---

## ✨ Features

- **Real-time alerts** — polls Upwork every 5 minutes (configurable) and posts new jobs the moment they appear
- **Multi-keyword, multi-channel** — route different search terms to different Discord channels
- **Rich embeds** — budget, experience level, duration, proposals, client spend, payment verification, and skills at a glance
- **Threaded detail view** — full job description + expanded client stats in a thread under each post
- **No duplicates** — SQLite deduplication ensures every job is posted exactly once
- **Cloudflare-resistant** — uses `curl_cffi` TLS impersonation for fast refreshes and `nodriver` (real undetected Chrome) for auth token harvesting
- **Auto auth refresh** — CF cookies refreshed every 25 min (HTTP only), auth tokens every 11 hours (headless browser)
- **Resilient** — retry logic with backoff on Discord errors, SQLite errors, and Upwork API failures
- **Observability** — `!status` command with live uptime, memory, token refresh time, and error counts

---

## 📋 Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | f-strings with `=`, `match` statements used throughout |
| Google Chrome or Chromium | Required by `nodriver` for token harvesting |
| Xvfb | For headless Linux servers (`sudo apt install xvfb`). Not needed on Windows — see [WSL2 setup](#-running-on-windows-wsl2) |
| A Discord bot token | See [setup guide](#1-create-your-discord-bot) below |
| An Upwork account | Required for the one-time login bootstrap |
| WSL2 *(Windows only)* | Recommended way to run on Windows — see [WSL2 setup](#-running-on-windows-wsl2) |

---

## 🚀 Setup

### 1. Create Your Discord Bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**
2. Navigate to **Bot** → click **Add Bot**
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**
4. Copy your bot token — you'll need it in a moment
5. Go to **OAuth2 → URL Generator**, select scopes: `bot`, and permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Embed Links`, `Read Message History`
6. Open the generated URL and invite the bot to your server

---

### 2. Clone the Repository

```bash
git clone https://github.com/yourusername/upwork-job-scraper-bot.git
cd upwork-job-scraper-bot
```

---

### 3. Install System Dependencies

**Debian / Ubuntu:**
```bash
sudo apt update
sudo apt install -y xvfb chromium-browser
```

**macOS:**
```bash
brew install --cask google-chrome
# Xvfb is not needed on macOS
```

---

### 4. Install Python Dependencies

```bash
pip install -r requirements.txt
```

**requirements.txt:**
```
discord.py
curl_cffi
nodriver
python-dotenv
psutil
```

---

### 5. Configure Environment Variables

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_discord_bot_token_here
CHECK_INTERVAL=5
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | Your Discord bot token |
| `CHECK_INTERVAL` | ❌ | `5` | How often to poll Upwork, in minutes |

---

### 6. Create graphql_payloads.py

The bot imports `SEARCH_QUERY` and `DETAILS_QUERY` from a `graphql_payloads.py` file. Create this file with the Upwork GraphQL queries. You can capture these by opening the Upwork job search page in your browser's DevTools → Network tab → filter for `graphql` requests while searching for jobs.

```python
# graphql_payloads.py
SEARCH_QUERY = """
  ... # paste visitorJobSearch query here
"""

DETAILS_QUERY = """
  ... # paste visitor job details query here
"""
```

---

### 7. Bootstrap (One-Time Login)

On the very first run, the bot will open a visible Chrome window and wait for you to log in to Upwork manually. This only happens once — after that, cookies are refreshed automatically in the background.

```bash
python discordbot.py
```

When Chrome opens:
1. Log in to your Upwork account normally
2. Complete any CAPTCHA or 2FA if prompted
3. Once you see your Upwork dashboard, the bot will detect the session automatically and close the browser

The bot will write your credentials to `config.json` and start the scraper loop.

> **Note:** `config.json` contains sensitive session cookies. Add it to `.gitignore` — it should never be committed.

---

### 8. Add Keywords and Channels

Once the bot is running, use these commands in your Discord server:

```
!add python developer
!add react frontend
!add machine learning
```

Each command tracks that keyword in the channel where the command is typed. Jobs matching that keyword will be posted to that channel.

---

## 🗂️ Project Structure

```
upwork-job-scraper-bot/
├── discordbot.py        # Entry point — bot lifecycle, scraper loop, commands
├── browser_session.py   # Headless Chrome via nodriver — cookie/token harvesting
├── auth_manager.py      # Two-tier auth refresh: CF cookies (HTTP) + auth token (browser)
├── fetchdata.py         # Upwork GraphQL API client
├── helpers.py           # Discord embed builder and formatting utilities
├── thread_poster.py     # Creates Discord threads with detail embeds
├── thread_helpers.py    # Builds the two-embed thread layout
├── database.py          # SQLite persistence — jobs, channels, logs
├── memory.py            # RSS memory monitoring
├── shutdown.py          # SIGINT/SIGTERM graceful shutdown
├── graphql_payloads.py  # Upwork GraphQL query strings (you provide this)
├── .env                 # Environment variables (never commit)
├── config.json          # Auto-generated Upwork session cookies (never commit)
└── jobs.db              # Auto-generated SQLite database
```

---

## 💬 Bot Commands

| Command | Permission | Description |
|---|---|---|
| `!add <keyword>` | Manage Channels | Start tracking a keyword in the current channel |
| `!remove <keyword>` | Manage Channels | Stop tracking a keyword in the current channel |
| `!list` | Everyone | List all active keyword → channel mappings |
| `!status` | Everyone | Show a live dashboard: uptime, memory, token refresh, jobs posted, error count |

---

## 📊 What a Job Post Looks Like

**Channel embed (summary):**
```
📦 Senior Python Developer Needed for API Integration

┌─────────────────────────────────────────────┐
│ Brief job description preview...            │
└─────────────────────────────────────────────┘

Posted: 3m ago        Budget/Rate: $50-$70/hr    Level: Expert
Duration: 1-3 months  Detected: 14:32             Proposals: 4

Client Info: ✅ Verified | 📍 United States | 💰 $24.5K spent
Skills: Python, FastAPI, PostgreSQL, Docker, REST APIs

🔗 Apply Here
```

**Thread (full details):**
- Full job description (up to 4,000 characters)
- Client: payment status, total spend, hires made, hire rate, member since
- Job: budget, duration, experience level, type, proposal count
- All skills (up to 12)
- Direct apply link

---

## ⚙️ How Authentication Works

Upwork is protected by Cloudflare. The bot handles this with two independent layers:

**Fast refresh (every 25 minutes):**
`curl_cffi` hits the Upwork homepage using a Chrome-impersonating TLS fingerprint, extracting fresh CF clearance cookies without spinning up a browser.

**Deep refresh (every 11 hours):**
`nodriver` launches a real, unmodified Chrome binary under an Xvfb virtual display. It navigates to Upwork, passes any CF challenge, and extracts OAuth2 tokens from localStorage and cookies. The browser is closed immediately after.

If a request returns a 401 or 403, the bot triggers an immediate refresh and retries automatically.

---

## 🛡️ Running as a Service (Linux)

To keep the bot running after you close your terminal, create a systemd service:

```ini
# /etc/systemd/system/upwork-bot.service
[Unit]
Description=Upwork Job Scraper Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/upwork-job-scraper-bot
ExecStart=/usr/bin/python3 discordbot.py
Restart=on-failure
RestartSec=10
Environment=DISPLAY=:0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable upwork-bot
sudo systemctl start upwork-bot
sudo systemctl status upwork-bot
```

View logs:
```bash
sudo journalctl -u upwork-bot -f
```

---

## 🪟 Running on Windows (WSL2)

WSL2 is the recommended way to run this bot on Windows. It provides a real Linux environment, so the code runs without any modifications.

### Step 1 — Enable WSL2

Open **PowerShell as Administrator** and run:

```powershell
wsl --install
```

This installs WSL2 and Ubuntu automatically. Restart your PC when prompted.

After the restart, Ubuntu will open and ask you to create a Linux username and password. Complete that, then run everything below inside the Ubuntu terminal.

> **Windows 10 users:** If `wsl --install` fails, your Windows 10 version may need a manual WSL2 setup. Run `winver` to check — you need build 19041 or higher. Follow [Microsoft's manual install guide](https://learn.microsoft.com/en-us/windows/wsl/install-manual) if needed.

---

### Step 2 — Install System Dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip git xvfb
```

Install Google Chrome:

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb
```

Verify Chrome installed correctly:

```bash
google-chrome --version
```

---

### Step 3 — Clone the Repository

```bash
git clone https://github.com/yourusername/upwork-job-scraper-bot.git
cd upwork-job-scraper-bot
```

---

### Step 4 — Install Python Dependencies

```bash
pip install -r requirements.txt --break-system-packages
```

---

### Step 5 — Create Your .env File

```bash
nano .env
```

Paste the following, filling in your Discord token:

```env
DISCORD_TOKEN=your_discord_bot_token_here
CHECK_INTERVAL=5
```

Save with `Ctrl+O`, then exit with `Ctrl+X`.

---

### Step 6 — Create graphql_payloads.py

Same as the Linux setup — see [step 6 above](#6-create-graphql_payloadspy).

---

### Step 7 — Bootstrap (One-Time Login)

```bash
python3 discordbot.py
```

On **Windows 11**, Chrome will open visibly in a window on your desktop (WSLg handles the display automatically). On **Windows 10**, Chrome runs headlessly since there is no WSLg — the bootstrap will still complete but you may need to have your Upwork session already active in a regular browser so cookies can be harvested.

Log in to Upwork when the Chrome window appears, complete any 2FA, and wait for the bot to confirm it detected the session.

---

### Step 8 — Keep It Running After Closing the Terminal

WSL2 shuts down when all terminals are closed. Use `screen` to keep the bot alive in the background:

```bash
sudo apt install -y screen
screen -S upworkbot
python3 discordbot.py
```

Detach from the screen session (bot keeps running):

```
Ctrl+A  then  D
```

Reattach later to check on it:

```bash
screen -r upworkbot
```

---

### Step 9 — Auto-Start on Windows Boot (Optional)

To have the bot start automatically every time Windows boots, without opening any visible window:

**1.** Create a file called `launch_bot.vbs` anywhere on Windows (e.g. your Desktop):

```vbs
Set ws = CreateObject("WScript.Shell")
ws.Run "wsl -d Ubuntu -- bash -c 'cd ~/upwork-job-scraper-bot && screen -dmS upworkbot python3 discordbot.py'", 0, False
```

**2.** Press `Win+R`, type `shell:startup`, and press Enter.

**3.** Copy `launch_bot.vbs` into the folder that opens.

Windows will now silently launch the bot inside WSL2 on every boot, with no terminal window appearing.

Check that it started after a reboot:

```bash
# Open Ubuntu terminal and run:
screen -r upworkbot
```

---

### WSL2 Troubleshooting

**Chrome crashes immediately**

This usually means `/dev/shm` is too small. The bot already passes `--disable-dev-shm-usage` to Chrome which works around this, but if crashes persist:

```bash
sudo mount -t tmpfs -o size=512m tmpfs /dev/shm
```

**"Cannot open display" error**

Xvfb did not start. Check it is installed:

```bash
which Xvfb
# if nothing prints:
sudo apt install -y xvfb
```

**Bot does not start after Windows reboot**

Open an Ubuntu terminal and check if the screen session exists:

```bash
screen -ls
```

If it is not listed, start it manually:

```bash
cd ~/upwork-job-scraper-bot
screen -S upworkbot
python3 discordbot.py
```

Then detach with `Ctrl+A D`.

**WSL2 keeps shutting down**

WSL2 will shut down if there are no active processes. The `screen` session prevents this as long as the bot is running inside it. If WSL2 still shuts down, add this to `/etc/wsl.conf` inside Ubuntu:

```ini
[wsl2]
guiApplications=false
```

Then restart WSL2 from PowerShell:

```powershell
wsl --shutdown
wsl
```

---

## 🗄️ Database

The bot creates `jobs.db` automatically. It contains three tables:

| Table | Purpose |
|---|---|
| `posted_jobs` | Tracks posted job IDs to prevent duplicates; purged after 30 days |
| `search_channels` | Maps keywords to Discord channel IDs |
| `logs` | Structured application log archive; purged after 30 days |

---

## 🔧 Troubleshooting

**Bot posts no jobs after `!add`**
- Confirm the bot has `Send Messages` and `Embed Links` permissions in that channel
- Check that `config.json` exists and has a non-empty `COOKIES` field
- Run `!status` and check the error count — look at bot logs for details

**"Bootstrap failed" on startup**
- Make sure Chrome/Chromium is installed and accessible in `PATH`
- On Linux, ensure Xvfb is installed (`sudo apt install xvfb`)
- Delete `config.json` and rerun to trigger a fresh bootstrap

**401/403 errors in logs**
- This is normal occasionally — the bot handles these by refreshing cookies automatically
- If they persist for more than one cycle, delete `config.json` and restart to force a new bootstrap

**High memory usage warning (>500 MB)**
- The nodriver browser is closed immediately after each token refresh, so this shouldn't be a browser leak
- Restart the bot process; the 30-day DB cleanup runs each cycle and keeps the database bounded

---

## .gitignore

Make sure your `.gitignore` includes:

```
.env
config.json
jobs.db
.browser_profile/
__pycache__/
*.pyc
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
