#!/usr/bin/env python3
"""
uploader-bot: releases the next queued Short to YouTube + Instagram when the channel's views dry up.

Runs every 30 min on GitHub Actions (tick.yml). Each tick:
  1. polls public view counts (YouTube videos.list + Instagram media insights) -> state/polls.csv
  2. views/hour = growth over the last ~2 h, summed over both platforms
  3. posts the next queued video when ALL hold (rules agreed with CTV 2026-11-03):
       - inside a posting window, America/New_York: 07:00-10:00, 11:30-13:30, 15:00-24:00
       - views/hour < DROP x the peak views/hour since the last post (the boom has faded)
       - >= MIN_GAP_H since the last post, and < MAX_PER_DAY posts today (NY date)
     Fallback: >= MAX_GAP_H since the last post and inside a window -> post anyway (channel never goes silent).
  4. opens a GitHub issue "Posted: <title>" (GitHub notifies CTV), or one when something fails.

Putting a video in queue.json IS CTV's approval to post it. Pause: repo variable BOT_PAUSED=1 (polling continues),
or disable the workflow. Videos live as assets on the release `queue` (not in git); the repo is public so
Instagram can fetch them by URL (the Instagram Login API has no file upload).

    python bot.py tick [--dry-run]       # what Actions runs; --dry-run polls + decides, never posts
    python bot.py status                 # queue + last rates
    python bot.py enqueue SLUG VIDEO.mp4 # local: upload the mp4 to the `queue` release (queue.json entry by hand)
    python bot.py push-secrets           # local: copy current YT/IG tokens into the repo's Actions secrets
"""
import argparse, csv, datetime as dt, json, os, subprocess, sys, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))
QUEUE, STATE, POLLS = ROOT / "queue.json", ROOT / "state/state.json", ROOT / "state/polls.csv"
NY = ZoneInfo("America/New_York")
WINDOWS = [((7, 0), (10, 0)), ((11, 30), (13, 30)), ((15, 0), (24, 0))]
DROP, MIN_GAP_H, MAX_GAP_H, MAX_PER_DAY, RATE_WINDOW_H = 0.25, 18, 48, 2, 2.0
REPO = os.environ.get("GITHUB_REPOSITORY", "ButHowDoItKnow/uploader-bot")
TOKENS = Path.home() / ".config/cyrex/tokens"


def now():
    return dt.datetime.now(dt.timezone.utc)


def load(p, default):
    return json.loads(p.read_text()) if p.exists() else default


def save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1) + "\n")


def in_window(t):
    m = t.astimezone(NY)
    hm = m.hour * 60 + m.minute
    return any(a[0] * 60 + a[1] <= hm < b[0] * 60 + b[1] for a, b in WINDOWS)


# ---------- polling ----------
def yt_total():
    import youtube_publish as yp
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=yp.creds(), cache_discovery=False)
    up = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, tok = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails", playlistId=up, maxResults=50, pageToken=tok).execute()
        ids += [i["contentDetails"]["videoId"] for i in r["items"]]
        tok = r.get("nextPageToken")
        if not tok:
            break
    tot = 0
    for k in range(0, len(ids), 50):
        for v in yt.videos().list(part="statistics", id=",".join(ids[k:k + 50])).execute()["items"]:
            tot += int(v["statistics"].get("viewCount", 0))
    return tot


def ig_total():
    import instagram_publish as ip
    tok = ip.token()
    url, tot = f"{ip.API}/me/media", 0
    params = {"fields": "id,media_type", "limit": 50, "access_token": tok}
    media = []
    while url:
        r = ip.call("GET", url, params)
        media += r.get("data", [])
        url, params = r.get("paging", {}).get("next"), None
    for m in media:
        try:
            r = ip.call("GET", f"{ip.API}/{m['id']}/insights", {"metric": "views", "access_token": tok})
            tot += int(r["data"][0]["values"][0]["value"])
        except Exception:
            pass                       # a media type without the views metric: skip it
    return tot


def poll():
    row = {"ts": now().isoformat(timespec="seconds"), "yt": "", "ig": ""}
    for k, f in (("yt", yt_total), ("ig", ig_total)):
        try:
            row[k] = f()
        except Exception as e:
            print(f"poll {k} failed: {e}")
    new = not POLLS.exists()
    POLLS.parent.mkdir(parents=True, exist_ok=True)
    with POLLS.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ts", "yt", "ig"])
        if new:
            w.writeheader()
        w.writerow(row)
    return row


def rates(since=None):
    """[(ts, views/hour)] — each poll vs the poll ~RATE_WINDOW_H earlier (per platform; skips gaps > 2x the window)."""
    rows = [r for r in csv.DictReader(POLLS.open())] if POLLS.exists() else []
    pts = [(dt.datetime.fromisoformat(r["ts"]), r) for r in rows]
    out = []
    for i, (t, r) in enumerate(pts):
        base = [(t0, r0) for t0, r0 in pts[:i] if RATE_WINDOW_H <= (t - t0).total_seconds() / 3600 <= 2 * RATE_WINDOW_H]
        if not base:
            continue
        t0, r0 = base[-1]
        h, v = (t - t0).total_seconds() / 3600, 0.0
        for k in ("yt", "ig"):
            if r[k] and r0[k]:
                v += max(0, int(r[k]) - int(r0[k])) / h
        if since is None or t >= since:
            out.append((t, v))
    return out


# ---------- decision ----------
def decide(st, t):
    last = dt.datetime.fromisoformat(st["last_post_at"]) if st.get("last_post_at") else None
    gap = (t - last).total_seconds() / 3600 if last else 1e9
    today = t.astimezone(NY).date().isoformat()
    n_today = sum(1 for p in st.get("posts", []) if dt.datetime.fromisoformat(p).astimezone(NY).date().isoformat() == today)
    rs = rates(since=last)
    cur = rs[-1][1] if rs and (t - rs[-1][0]).total_seconds() < 3600 else None
    peak = max((v for _, v in rs), default=0)
    info = dict(window=in_window(t), gap_h=round(gap, 1), today=n_today, rate=cur and round(cur, 1), peak=round(peak, 1))
    if not info["window"]:
        return False, "outside posting window", info
    if n_today >= MAX_PER_DAY:
        return False, "daily cap reached", info
    if gap >= MAX_GAP_H:
        return True, f"fallback: {gap:.0f} h since last post", info
    if gap < MIN_GAP_H:
        return False, f"min gap ({gap:.1f} < {MIN_GAP_H} h)", info
    if cur is None or peak <= 0:
        return False, "not enough poll data yet", info
    if cur < DROP * peak:
        return True, f"drought: {cur:.0f}/h < {DROP:.0%} of peak {peak:.0f}/h", info
    return False, f"still active: {cur:.0f}/h vs peak {peak:.0f}/h", info


# ---------- posting ----------
def gh(*args, **kw):
    return subprocess.run(["gh", *args], check=True, text=True, capture_output=True, **kw).stdout


def fetch(item):
    dst = ROOT / "dl" / item["asset"]
    if not dst.exists():
        dst.parent.mkdir(exist_ok=True)
        gh("release", "download", "queue", "--repo", REPO, "-p", item["asset"], "-D", str(dst.parent), "--clobber")
    return dst


def post_youtube(item, video):
    import youtube_publish as yp
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    yt = build("youtube", "v3", credentials=yp.creds(), cache_discovery=False)
    body = {"snippet": {"title": item["title"], "description": item["yt_description"], "tags": item.get("tags", []),
                        "categoryId": "28"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), chunksize=8 << 20, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def post_instagram(item, video):
    """Instagram fetches the video from the public `queue` release asset (Instagram Login API needs a video_url)."""
    import instagram_publish as ip
    tok = ip.token()
    uid = ip.me(tok)["user_id"]
    url = f"https://github.com/{REPO}/releases/download/queue/{item['asset']}"
    c = ip.call("POST", f"{ip.API}/{uid}/media", {
        "media_type": "REELS", "video_url": url, "caption": item["ig_caption"],
        "share_to_feed": "true", "thumb_offset": item.get("thumb_offset_ms", 0), "access_token": tok})
    for _ in range(120):
        s = ip.call("GET", f"{ip.API}/{c['id']}", {"fields": "status_code,status", "access_token": tok})
        if s.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
            break
        time.sleep(5)
    if s.get("status_code") != "FINISHED":
        raise RuntimeError(f"instagram container {c['id']}: {s}")
    r = ip.call("POST", f"{ip.API}/{uid}/media_publish", {"creation_id": c["id"], "access_token": tok})
    m = ip.call("GET", f"{ip.API}/{r['id']}", {"fields": "permalink", "access_token": tok})
    return r["id"], m.get("permalink")


def notify(title, body):
    if os.environ.get("GITHUB_ACTIONS"):
        try:
            gh("issue", "create", "--repo", REPO, "--title", title, "--body", body)
        except Exception as e:
            print("notify failed:", e)
    print(f"[notify] {title}\n{body}")


def post_next(q, st, t, why):
    item = next((i for i in q["items"] if i.get("status") != "posted"), None)
    if not item:
        notify("Queue is empty", f"The bot wanted to post ({why}) but the queue is empty. Add videos to queue.json.")
        return
    video = fetch(item)
    errs = []
    if not item.get("yt_id"):
        try:
            item["yt_id"] = post_youtube(item, video)
        except Exception as e:
            errs.append(f"YouTube: {e}")
    if not item.get("ig_id"):
        try:
            item["ig_id"], item["ig_url"] = post_instagram(item, video)
        except Exception as e:
            errs.append(f"Instagram: {e}")
    if item.get("yt_id") or item.get("ig_id"):    # at least one went live: that counts as the post
        item["posted_at"] = item.get("posted_at") or t.isoformat(timespec="seconds")
        st["last_post_at"] = item["posted_at"]
        st.setdefault("posts", []).append(t.isoformat(timespec="seconds"))
    item["status"] = "posted" if item.get("yt_id") and item.get("ig_id") else "partial"
    left = sum(1 for i in q["items"] if i.get("status") != "posted")
    links = f"YouTube: https://youtube.com/shorts/{item.get('yt_id')}\nInstagram: {item.get('ig_url')}"
    if errs:
        notify(f"Post PARTLY failed: {item['title']}", f"Reason to post: {why}\n{links}\n\nErrors (retried next tick):\n" + "\n".join(errs))
    else:
        notify(f"Posted: {item['title']}", f"Reason: {why}\n{links}\n\n{left} left in the queue.")


def cmd_tick(a):
    q, st, t = load(QUEUE, {"items": []}), load(STATE, {}), now()
    poll()
    partial = next((i for i in q["items"] if i.get("status") == "partial"), None)
    go, why, info = decide(st, t)
    print("decision:", go, "|", why, "|", info)
    st["last_tick"] = {"at": t.isoformat(timespec="seconds"), "post": go, "why": why, **info}
    paused = os.environ.get("BOT_PAUSED", "") not in ("", "0")
    if a.dry_run or paused:
        print("dry-run" if a.dry_run else "paused (BOT_PAUSED)", "-> not posting")
    elif partial and in_window(t):
        post_next(q, st, t, "retry the platform that failed last time")
    elif go:
        post_next(q, st, t, why)
    if not a.dry_run:
        save(QUEUE, q)
        save(STATE, st)
    exp = TOKENS / "instagram_token.expires"
    if exp.exists() and (dt.date.fromisoformat(exp.read_text().strip()) - t.date()).days < 14 and not st.get("warned_ig"):
        notify("Instagram token expires soon", "Run `python bot.py push-secrets` on the laptop (or ask Claude).")
        st["warned_ig"] = True
        save(STATE, st)


def cmd_status(_):
    q, st = load(QUEUE, {"items": []}), load(STATE, {})
    for i in q["items"]:
        print(f"{i.get('status', 'queued'):8} {i['slug']:14} {i['title']}")
    print("last tick:", st.get("last_tick"))
    for t, v in rates()[-6:]:
        print(t.astimezone(NY).strftime("%m-%d %H:%M"), f"{v:.0f} views/h")


def cmd_enqueue(a):
    try:
        gh("release", "view", "queue", "--repo", REPO)
    except subprocess.CalledProcessError:
        gh("release", "create", "queue", "--repo", REPO, "--title", "queue", "--notes", "Videos waiting to be posted.")
    gh("release", "upload", "queue", "--repo", REPO, "--clobber", f"{a.video}#{a.slug}.mp4")
    print(f"uploaded as {a.slug}.mp4 — add an item with \"asset\": \"{a.slug}.mp4\" to queue.json")


def cmd_push_secrets(_):
    import instagram_publish as ip
    ip.token()                                      # refreshes it locally if a day old
    gh("secret", "set", "YT_TOKEN_JSON", "--repo", REPO, input=(TOKENS / "youtube_token.json").read_text())
    gh("secret", "set", "IG_TOKEN", "--repo", REPO, input=(TOKENS / "instagram_token").read_text().strip())
    gh("secret", "set", "IG_TOKEN_EXPIRES", "--repo", REPO, input=(TOKENS / "instagram_token.expires").read_text().strip())
    print("secrets updated (YT_TOKEN_JSON, IG_TOKEN, IG_TOKEN_EXPIRES)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    t = s.add_parser("tick"); t.add_argument("--dry-run", action="store_true"); t.set_defaults(f=cmd_tick)
    s.add_parser("status").set_defaults(f=cmd_status)
    e = s.add_parser("enqueue"); e.add_argument("slug"); e.add_argument("video"); e.set_defaults(f=cmd_enqueue)
    s.add_parser("push-secrets").set_defaults(f=cmd_push_secrets)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
