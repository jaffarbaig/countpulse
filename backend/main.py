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

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

YT_API_KEY = os.getenv("YT_API_KEY", "")
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
    fetched_at: float = 0.0
    last_seen: float = field(default_factory=time.time)
    # estimation state
    anchor_value: float = 0.0
    anchor_time: float = 0.0
    crossings: deque = field(default_factory=lambda: deque(maxlen=20))  # (t, api_subs) upward changes
    view_hist: deque = field(default_factory=lambda: deque(maxlen=400))  # (t, views)

    # --- rates -------------------------------------------------------------
    def views_rate(self) -> float:
        h = [p for p in self.view_hist if p[0] >= time.time() - HISTORY_SECONDS]
        if len(h) >= 2 and h[-1][1] > h[0][1] and h[-1][0] > h[0][0]:
            return (h[-1][1] - h[0][1]) / (h[-1][0] - h[0][0])
        return 0.0

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
        return 0.0

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
    fetched_at: float = 0.0
    last_seen: float = field(default_factory=time.time)
    hist: deque = field(default_factory=lambda: deque(maxlen=400))  # (t, views, likes, comments)

    def rates(self) -> tuple[float, float, float]:
        h = [p for p in self.hist if p[0] >= time.time() - 3600]
        if len(h) < 2 or h[-1][0] <= h[0][0]:
            return 0.0, 0.0, 0.0
        dt = h[-1][0] - h[0][0]
        return tuple(max(0.0, (h[-1][i] - h[0][i]) / dt) for i in (1, 2, 3))  # type: ignore

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


# Serve the frontend from the same origin (so the app is one deploy).
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIR / "index.html")
