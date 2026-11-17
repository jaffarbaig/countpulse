# CountPulse: live YouTube counters with no ads

A livecounts.io-style app by OneClikAI. Python FastAPI backend with an HTML/JS front end (one file, no build step).

## Features (v1.1)
- Live subscriber counter with a rolling odometer, updating every 2s, 5s or 10s
- Live **video** counter: paste any video / Shorts / live link to see views, likes and comments
- Compare two channels: live gap, gap change per day, and when one overtakes the other
- Milestone countdown, with confetti when a channel crosses a round number
- Future projections: tomorrow, 1 week, 1 month, 1 year
- Top channels: sort by size or by fastest growth
- Unlimited favourites, saved in the browser
- Fullscreen focus mode, for TVs and watch parties
- Embed / OBS browser-source overlay (dark, light or transparent)
- International (1,234,567) or Indian (12,34,567, L/Cr) number format
- No ads, ever

## How "live" works
YouTube's API rounds subscriber counts to 3 significant figures. For example, 452,345,678 is returned as 452,000,000.
The backend estimates the real number inside that rounding range using the channel's growth rate, which comes from two sources:
1. the times the rounded value steps up (best signal), or
2. view velocity × the channel's subscribers-per-view ratio (fallback).

The estimate is **clamped** so it never leaves `[api_value, api_value + bucket)`. View counts are exact.

## Quota math (free tier: 10,000 units/day)
- `channels.list` and `videos.list` cost **1 unit for up to 50 IDs**. The server polls every channel or video *anyone* viewed in the last 10 minutes, once per `POLL_SECONDS` (default 30s), so browsers never hit YouTube.
- Polling 50 channels every 30s ≈ 2,880 units/day. Raise `POLL_SECONDS` or request a quota increase as you grow.
- Searching by name (`search.list`) costs 100 units, so results are cached. @handles and channel URLs cost 1 unit.

## Run locally
```bash
cd backend
pip install -r requirements.txt
export YT_API_KEY=your_key   # Google Cloud Console → enable "YouTube Data API v3" → Credentials → API key
uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```
Without a key (or when opened as a plain file), the front end runs in **demo mode** with simulated numbers.

## Deploy (Hostinger VPS / any Ubuntu server)
Type this in the VPS terminal (no pasting needed):
```
git clone https://github.com/jaffarbaig/countpulse && bash countpulse/deploy.sh
```
It prints a 6-digit setup code and installs in the background. After 3–5 minutes, open `https://<domain>/setup`, enter the code and paste your YouTube API key. Progress: `tail /root/deploy.log`.

To update later: `git -C countpulse pull && bash countpulse/update.sh` (keeps key, nginx site and HTTPS).

Other hosts: any host that runs Python works: Render, Railway, AWS App Runner, a GCP Cloud Run container, or an EC2/Lightsail VM.
```bash
docker build -t countpulse . && docker run -p 8000:8000 -e YT_API_KEY=xxx countpulse
```
Run **one** instance, because estimation state is held in memory. Before scaling out, move state to Redis.

## Env vars
| Var | Default | Meaning |
|---|---|---|
| `YT_API_KEY` | – | YouTube Data API v3 key |
| `POLL_SECONDS` | 30 | How often the server polls YouTube |
| `ACTIVE_WINDOW` | 600 | Seconds to keep polling a channel after its last viewer |

Edit `FEATURED` in `backend/main.py` to change the homepage leaderboard (add Telugu/Hindi channels here).

## Embed URL
`https://your-domain.com/?embed=1&c=<channelId>&metric=subs|views&theme=dark|light|clear`

## Roadmap
- History charts (Postgres or SQLite, one snapshot per channel every 10 min) and Social Blade-style daily tables
- WhatsApp/Telegram milestone alerts
- PWA install + home-screen widget
- Chrome extension that shows the live count on YouTube channel pages
- Twitch and Kick counters (official APIs)
- Hindi/Telugu UI
- Creator plan via Razorpay: branded overlays, alerts, a featured spot (no ads)
