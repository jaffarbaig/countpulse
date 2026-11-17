"""
CountPulse backend — live YouTube subscriber / view counter.

How it works
------------
* Clients never call YouTube directly. They call this server every ~2s (free).
* A background poller calls YouTube Data API v3 `channels.list` every POLL_SECONDS
  for every channel someone viewed recently, 50 channels per request (1 quota unit).
* YouTube rounds public subscriber counts to 3 significant figures, so the server
  estimates the live number inside the rounding bucket using the channel's
  observed growth rate, and never lets the estimate leave [api_value, api_value + bucket).
* View counts are exact; we only smooth them between YouTube updates.

Run:  YT_API_KEY=xxx uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import hmac

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# The API key comes from the env, or from KEY_FILE once set through the /setup page.
KEY_FILE = Path(os.getenv("KEY_FILE", "/data/yt_key"))
SETUP_CODE = os.getenv("SETUP_CODE", "")
YT_API_KEY = os.getenv("YT_API_KEY", "")
if not YT_API_KEY and KEY_FILE.exists():
    YT_API_KEY = KEY_FILE.read_text().strip()
YT_BASE = os.getenv("YT_BASE", "https://www.googleapis.com/youtube/v3")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "30"))
ACTIVE_WINDOW = float(os.getenv("ACTIVE_WINDOW", "600"))  # keep polling 10 min after last view
HISTORY_SECONDS = 3 * 3600

# Channels always polled (homepage leaderboard). Edit freely.
FEATURED = [
    "UCX6OQ3DkcsbYNE6H8uQQuVA",  # MrBeast
    "UCq-Fj5jknLsUf-MWSy4_brA",  # T-Series
    "UCbCmjCuTUZos6Inko4u57UQ",  # Cocomelon
    "UCpEhnqL0y41EpW2TvWAHD7Q",  # SET India
    "UCk8GzjMOrta8yxDcKfylJYw",  # Kids Diana Show
    "UCvlE5gTbOvjiolFlEm-c_Ow",  # Vlad and Niki
    "UC-lHJZR3Gqxm24_Vd_AJ5Yw",  # PewDiePie
    "UCFFbwnve3yF62-tVXkTyHqg",  # Zee Music Company
]

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# ----------------------------------------------------------------------------
# Estimation helpers
# ----------------------------------------------------------------------------
def parse_ts(s: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def lifetime_rate(total: float, published: float) -> float:
    """Average growth per second since publish: a starting guess before we observe real movement."""
    age = time.time() - published
    return total / age if published and age > 60 else 0.0


def observed_rate(hist, idx: int, window: float):
    """Rate between the first sample and the last *change* in the window (YouTube updates in steps)."""
    h = [p for p in hist if p[0] >= time.time() - window]
    if len(h) < 2:
        return None
    first = h[0]
    last = next((p for p in reversed(h) if p[idx] != first[idx]), None)
    if last is None or last[0] <= first[0]:
        return None
    return max(0.0, (last[idx] - first[idx]) / (last[0] - first[0]))


def bucket_size(api_subs: int) -> int:
    """Size of YouTube's rounding step: 3 significant figures above 1,000."""
    if api_subs < 1000:
        return 1
    digits = int(math.log10(api_subs)) + 1
    return 10 ** (digits - 3)


@dataclass
class Channel:
    id: str
    title: str = ""
    handle: str = ""
    thumb: str = ""
    country: str = ""
    api_subs: int = 0
    views: int = 0
    videos: int = 0
    hidden_subs: bool = False
    published: float = 0.0
    fetched_at: float = 0.0
    last_seen: float = field(default_factory=time.time)
    # estimation state
    anchor_value: float = 0.0
    anchor_time: float = 0.0
    crossings: deque = field(default_factory=lambda: deque(maxlen=20))  # (t, api_subs) upward changes
    view_hist: deque = field(default_factory=lambda: deque(maxlen=400))  # (t, views)

    # --- rates -------------------------------------------------------------
    def views_rate(self) -> float:
        r = observed_rate(self.view_hist, 1, HISTORY_SECONDS)
        return r if r else lifetime_rate(self.views, self.published)

    def subs_rate(self) -> float:
        # Best signal: two observed bucket crossings.
        if len(self.crossings) >= 2:
            (t0, s0), (t1, s1) = self.crossings[0], self.crossings[-1]
            if t1 > t0 and s1 > s0:
                return (s1 - s0) / (t1 - t0)
        # Fallback: views velocity x lifetime subs/views ratio.
        vr = self.views_rate()
        if vr > 0 and self.views > 0:
            return vr * (self.api_subs / self.views)
        return lifetime_rate(self.api_subs, self.published)

    # --- update from a fresh API row ----------------------------------------
    def ingest(self, subs: int, views: int, videos: int, now: float) -> None:
        b = bucket_size(subs)
        if self.fetched_at == 0:
            self.anchor_value, self.anchor_time = subs + b * 0.5, now
        elif subs > self.api_subs:
            # Real count just crossed into a new bucket.
            self.crossings.append((now, subs))
            self.anchor_value, self.anchor_time = float(subs), now
        elif subs < self.api_subs:
            self.crossings.clear()
            self.anchor_value, self.anchor_time = subs + b * 0.5, now
        self.api_subs, self.views, self.videos = subs, views, videos
        self.view_hist.append((now, views))
        self.fetched_at = now

    def estimate(self, now: float) -> float:
        b = bucket_size(self.api_subs)
        if b == 1:
            return float(self.api_subs)
        est = self.anchor_value + self.subs_rate() * (now - self.anchor_time)
        return max(float(self.api_subs), min(est, self.api_subs + b - 1))

    def to_json(self, now: float) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "handle": self.handle,
            "thumb": self.thumb,
            "country": self.country,
            "hiddenSubs": self.hidden_subs,
            "apiSubs": self.api_subs,
            "bucket": bucket_size(self.api_subs),
            "estSubs": round(self.estimate(now), 2),
            "subsRate": self.subs_rate(),
            "views": self.views,
            "viewsRate": self.views_rate(),
            "videos": self.videos,
            "fetchedAt": self.fetched_at,
            "serverTime": now,
        }


@dataclass
class Video:
    id: str
    title: str = ""
    channel: str = ""
    channel_id: str = ""
    thumb: str = ""
    views: int = 0
    likes: int = 0
    comments: int = 0
    published: float = 0.0
    fetched_at: float = 0.0
    last_seen: float = field(default_factory=time.time)
    hist: deque = field(default_factory=lambda: deque(maxlen=400))  # (t, views, likes, comments)

    def rates(self) -> tuple[float, float, float]:
        totals = (self.views, self.likes, self.comments)
        out = []
        for i in (1, 2, 3):
            r = observed_rate(self.hist, i, 3600)
            out.append(r if r else lifetime_rate(totals[i - 1], self.published))
        return tuple(out)  # type: ignore

    def to_json(self, now: float) -> dict:
        vr, lr, cr = self.rates()
        return {
            "id": self.id, "title": self.title, "channel": self.channel, "channelId": self.channel_id,
            "thumb": self.thumb, "views": self.views, "likes": self.likes, "comments": self.comments,
            "viewsRate": vr, "likesRate": lr, "commentsRate": cr,
            "fetchedAt": self.fetched_at, "serverTime": now,
        }


VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})")


# ----------------------------------------------------------------------------
# YouTube client + poller
# ----------------------------------------------------------------------------
class Store:
    def __init__(self) -> None:
        self.videos: dict[str, Video] = {}
        self.channels: dict[str, Channel] = {}
        self.resolve_cache: dict[str, str] = {}
        self.client: Optional[httpx.AsyncClient] = None
        self.quota_used = 0

    async def yt(self, path: str, **params) -> dict:
        if not YT_API_KEY:
            raise HTTPException(503, "Server has no YT_API_KEY configured")
        params["key"] = YT_API_KEY
        r = await self.client.get(f"{YT_BASE}/{path}", params=params, timeout=10)
        self.quota_used += 100 if path == "search" else 1
        if r.status_code != 200:
            raise HTTPException(502, f"YouTube API error {r.status_code}: {r.text[:200]}")
        return r.json()

    async def refresh(self, ids: list[str]) -> None:
        now = time.time()
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            data = await self.yt("channels", part="snippet,statistics", id=",".join(batch), maxResults=50)
            for item in data.get("items", []):
                ch = self.channels.setdefault(item["id"], Channel(id=item["id"]))
                sn, st = item.get("snippet", {}), item.get("statistics", {})
                ch.title = sn.get("title", ch.title)
                ch.handle = sn.get("customUrl", ch.handle)
                ch.country = sn.get("country", ch.country)
                ch.published = parse_ts(sn.get("publishedAt", "")) or ch.published
                ch.thumb = (sn.get("thumbnails", {}).get("medium") or sn.get("thumbnails", {}).get("default") or {}).get("url", ch.thumb)
                ch.hidden_subs = bool(st.get("hiddenSubscriberCount", False))
                ch.ingest(int(st.get("subscriberCount", 0)), int(st.get("viewCount", 0)), int(st.get("videoCount", 0)), now)

    async def refresh_videos(self, ids: list[str]) -> None:
        now = time.time()
        for i in range(0, len(ids), 50):
            data = await self.yt("videos", part="snippet,statistics", id=",".join(ids[i : i + 50]), maxResults=50)
            for item in data.get("items", []):
                v = self.videos.setdefault(item["id"], Video(id=item["id"]))
                sn, st = item.get("snippet", {}), item.get("statistics", {})
                v.title, v.channel, v.channel_id = sn.get("title", ""), sn.get("channelTitle", ""), sn.get("channelId", "")
                v.published = parse_ts(sn.get("publishedAt", "")) or v.published
                v.thumb = (sn.get("thumbnails", {}).get("medium") or {}).get("url", v.thumb)
                v.views, v.likes, v.comments = int(st.get("viewCount", 0)), int(st.get("likeCount", 0)), int(st.get("commentCount", 0))
                v.hist.append((now, v.views, v.likes, v.comments))
                v.fetched_at = now

    def active_ids(self) -> list[str]:
        cutoff = time.time() - ACTIVE_WINDOW
        ids = {c.id for c in self.channels.values() if c.last_seen >= cutoff}
        return sorted(ids | set(FEATURED))

    def active_videos(self) -> list[str]:
        cutoff = time.time() - ACTIVE_WINDOW
        return [v.id for v in self.videos.values() if v.last_seen >= cutoff]

    async def poll_forever(self) -> None:
        while True:
            try:
                if YT_API_KEY:
                    await self.refresh(self.active_ids())
                    vids = self.active_videos()
                    if vids:
                        await self.refresh_videos(vids)
            except Exception as e:  # keep polling on transient errors
                print("poll error:", e)
            await asyncio.sleep(POLL_SECONDS)

    async def resolve(self, q: str) -> str:
        q = q.strip()
        key = q.lower()
        if key in self.resolve_cache:
            return self.resolve_cache[key]
        m = re.search(r"(UC[\w-]{22})", q)
        if m:
            cid = m.group(1)
        else:
            handle = None
            m = re.search(r"youtube\.com/@([\w.\-]+)", q)
            if m:
                handle = m.group(1)
            elif q.startswith("@"):
                handle = q[1:]
            elif re.fullmatch(r"[\w.\-]+", q):
                handle = q
            cid = None
            if handle:  # 1 quota unit
                d = await self.yt("channels", part="id", forHandle="@" + handle)
                if d.get("items"):
                    cid = d["items"][0]["id"]
            if not cid:  # 100 quota units — cached forever
                d = await self.yt("search", part="snippet", type="channel", q=q, maxResults=1)
                if d.get("items"):
                    cid = d["items"][0]["snippet"]["channelId"]
        if not cid:
            raise HTTPException(404, "Channel not found")
        self.resolve_cache[key] = cid
        return cid


store = Store()
app = FastAPI(title="CountPulse API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


@app.on_event("startup")
async def _startup() -> None:
    store.client = httpx.AsyncClient()
    asyncio.create_task(store.poll_forever())


@app.get("/api/health")
async def health():
    return {"ok": True, "live": bool(YT_API_KEY), "tracked": len(store.channels), "quotaUsedSinceStart": store.quota_used}


@app.get("/api/resolve")
async def resolve(q: str = Query(..., min_length=1, max_length=200)):
    cid = await store.resolve(q)
    if cid not in store.channels or store.channels[cid].fetched_at == 0:
        await store.refresh([cid])
    ch = store.channels.get(cid)
    if not ch:
        raise HTTPException(404, "Channel not found")
    ch.last_seen = time.time()
    return ch.to_json(time.time())


@app.get("/api/channels")
async def channels(ids: str = Query(..., max_length=2000)):
    wanted = [i for i in ids.split(",") if re.fullmatch(r"UC[\w-]{22}", i)][:50]
    missing = [i for i in wanted if i not in store.channels or store.channels[i].fetched_at == 0]
    if missing:
        await store.refresh(missing)
    now = time.time()
    out = []
    for i in wanted:
        ch = store.channels.get(i)
        if ch:
            ch.last_seen = now
            out.append(ch.to_json(now))
    return {"items": out}


@app.get("/api/top")
async def top():
    now = time.time()
    missing = [i for i in FEATURED if i not in store.channels or store.channels[i].fetched_at == 0]
    if missing and YT_API_KEY:
        await store.refresh(missing)
    items = [store.channels[i].to_json(now) for i in FEATURED if i in store.channels]
    items.sort(key=lambda c: c["estSubs"], reverse=True)
    return {"items": items}


@app.get("/api/video")
async def video(q: str = Query(..., min_length=1, max_length=300)):
    m = VIDEO_ID_RE.search(q) or re.fullmatch(r"([\w-]{11})", q.strip())
    if not m:
        raise HTTPException(400, "Paste a YouTube video link")
    vid = m.group(1)
    if vid not in store.videos:
        await store.refresh_videos([vid])
    v = store.videos.get(vid)
    if not v:
        raise HTTPException(404, "Video not found")
    v.last_seen = time.time()
    return v.to_json(time.time())


@app.get("/api/videos")
async def videos(ids: str = Query(..., max_length=700)):
    wanted = [i for i in ids.split(",") if re.fullmatch(r"[\w-]{11}", i)][:50]
    missing = [i for i in wanted if i not in store.videos]
    if missing:
        await store.refresh_videos(missing)
    now = time.time()
    out = []
    for i in wanted:
        v = store.videos.get(i)
        if v:
            v.last_seen = now
            out.append(v.to_json(now))
    return {"items": out}


# ----------------------------------------------------------------------------
# One-time setup page: paste the YouTube key in a browser instead of a terminal.
# Protected by the 6-digit SETUP_CODE printed by deploy.sh; disabled once a key is set.
# ----------------------------------------------------------------------------
_setup_failures = 0

SETUP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CountPulse setup</title><style>
body{font-family:system-ui,sans-serif;background:#0A0C11;color:#ECEFF6;display:grid;place-items:center;min-height:100vh;margin:0;padding:20px;box-sizing:border-box}
.c{background:#12161F;border:1px solid #232A3A;border-radius:16px;padding:24px;max-width:420px;width:100%}
h1{font-size:22px;margin:0 0 6px}p{color:#8A93A8;font-size:14px;line-height:1.5}
label{display:block;font-size:13px;color:#8A93A8;margin:14px 0 6px}
input{width:100%;box-sizing:border-box;padding:13px;border-radius:10px;border:1px solid #232A3A;background:#181D29;color:#fff;font-size:16px}
button{margin-top:18px;width:100%;padding:14px;border:0;border-radius:12px;font-size:16px;font-weight:600;color:#fff;background:linear-gradient(135deg,#FF4D5E,#FF8A3D)}
#m{margin-top:14px;font-size:14px;min-height:20px}</style></head><body><div class="c">
<h1>CountPulse setup</h1><p>Paste your YouTube Data API key. The 6-digit setup code is shown in your VPS terminal.</p>
<form id="f"><label>Setup code</label><input id="code" inputmode="numeric" autocomplete="off" maxlength="6">
<label>YouTube API key</label><input id="key" autocomplete="off" spellcheck="false">
<button>Save &amp; go live</button><div id="m"></div></form></div><script>
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const m=document.getElementById('m');m.style.color='#8A93A8';m.textContent='Checking key with YouTube…';
const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:document.getElementById('code').value.trim(),key:document.getElementById('key').value.trim()})});
const d=await r.json().catch(()=>({}));if(r.ok){m.style.color='#2EE6A8';m.textContent='Done! Opening CountPulse…';setTimeout(()=>location.href='/',1500)}else{m.style.color='#FF6B6B';m.textContent=d.detail||'Something went wrong'}}
</script></body></html>"""


class SetupIn(BaseModel):
    code: str
    key: str


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    if YT_API_KEY:
        return HTMLResponse("<meta name=viewport content='width=device-width'><body style='font-family:system-ui;background:#0A0C11;color:#ECEFF6;padding:40px'>CountPulse is already set up. <a style='color:#FF8A3D' href='/'>Open it</a></body>")
    return SETUP_HTML


@app.post("/api/setup")
async def setup(body: SetupIn):
    global YT_API_KEY, _setup_failures
    if YT_API_KEY:
        raise HTTPException(409, "Already set up")
    if not SETUP_CODE or _setup_failures >= 10:
        raise HTTPException(403, "Setup is locked. Re-run deploy.sh on the VPS for a new code.")
    if not hmac.compare_digest(body.code, SETUP_CODE):
        _setup_failures += 1
        raise HTTPException(403, "Wrong setup code")
    key = body.key.strip()
    if not re.fullmatch(r"[\w-]{20,60}", key):
        raise HTTPException(400, "That doesn't look like an API key")
    r = await store.client.get(f"{YT_BASE}/channels", params={"part": "id", "id": FEATURED[0], "key": key}, timeout=10)
    if r.status_code != 200:
        raise HTTPException(400, "YouTube rejected this key. Check that YouTube Data API v3 is enabled for it.")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key)
    KEY_FILE.chmod(0o600)
    YT_API_KEY = key
    await store.refresh(FEATURED)
    return {"ok": True}


# Serve the frontend from the same origin (so the app is one deploy).
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    import html as _html

    def page(title: str = "", desc: str = "", image: str = "") -> HTMLResponse:
        """index.html with link-preview tags (WhatsApp, X, Telegram) for shared links."""
        doc = (FRONTEND_DIR / "index.html").read_text()
        if title:
            t, d = _html.escape(title), _html.escape(desc)
            tags = (f"<title>{t}</title>\n<meta property=\"og:title\" content=\"{t}\">"
                    f"\n<meta property=\"og:description\" content=\"{d}\">\n<meta name=\"description\" content=\"{d}\">"
                    "\n<meta property=\"og:site_name\" content=\"CountPulse\">\n<meta name=\"twitter:card\" content=\"summary_large_image\">")
            if image:
                tags += f"\n<meta property=\"og:image\" content=\"{_html.escape(image)}\">"
            doc = re.sub(r"<title>.*?</title>", lambda _: tags, doc, count=1, flags=re.S)
        return HTMLResponse(doc)

    @app.get("/")
    async def index():
        return page()

    @app.get("/v/{vid}")
    async def share_video(vid: str):
        try:
            if re.fullmatch(r"[\w-]{11}", vid) and YT_API_KEY:
                if vid not in store.videos:
                    await store.refresh_videos([vid])
                v = store.videos.get(vid)
                if v:
                    return page(f"{v.title} - live view count", f"{v.views:,} views and counting. Watch it live on CountPulse.", v.thumb)
        except Exception:
            pass
        return page()

    @app.get("/c/{q}")
    async def share_channel(q: str):
        try:
            if YT_API_KEY:
                cid = await store.resolve(q)
                if cid not in store.channels:
                    await store.refresh([cid])
                ch = store.channels.get(cid)
                if ch:
                    return page(f"{ch.title} live subscriber count", "Watch the subscriber count tick up live on CountPulse.", ch.thumb)
        except Exception:
            pass
        return page()

    @app.get("/compare/{a}/{b}")
    async def share_compare(a: str, b: str):
        try:
            if YT_API_KEY:
                ids = [await store.resolve(a), await store.resolve(b)]
                missing = [i for i in ids if i not in store.channels]
                if missing:
                    await store.refresh(missing)
                A, B = store.channels.get(ids[0]), store.channels.get(ids[1])
                if A and B:
                    return page(f"{A.title} vs {B.title} - live sub count", "Who is winning right now? Live subscriber battle on CountPulse.", A.thumb)
        except Exception:
            pass
        return page()
