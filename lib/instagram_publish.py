#!/usr/bin/env python3
"""
instagram_publish.py — the "Cyrex Publisher" Instagram side (Instagram API with Instagram Login).

Posting is two steps on purpose (approve-first): `upload` creates a Reels container and uploads the video
(nothing is visible yet); `publish CONTAINER_ID` makes it live, only after CTV says yes. Containers expire
after 24 h. The 60-day token is refreshed on every run once it is a day old.

Files in ~/.config/cyrex/tokens/ (chmod 600, outside the git repo):
    instagram_token          long-lived token
    instagram_token.expires  YYYY-MM-DD it expires (rewritten on refresh)

Usage (standard library only):
    instagram_publish.py whoami
    instagram_publish.py upload VIDEO_URL --caption-file CAPTION.txt [--thumb-offset-ms 2000]
        (Instagram-Login apps can't upload bytes: VIDEO_URL must be a public https link Instagram fetches;
         host it briefly, e.g. a GitHub release asset, and delete it once the container is FINISHED)
    instagram_publish.py status CONTAINER_ID
    instagram_publish.py publish CONTAINER_ID       # goes live
    instagram_publish.py insights MEDIA_ID          # views, reach, avg watch time, ...
"""
import argparse
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKENS = Path.home() / ".config/cyrex/tokens"
TOKEN = TOKENS / "instagram_token"
EXPIRES = TOKENS / "instagram_token.expires"
API = "https://graph.instagram.com/v23.0"


def call(method, url, params=None, headers=None, data=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b"{}")
        raise SystemExit(f"HTTP {e.code}: {body.get('error', body)}")


def token():
    tok = TOKEN.read_text().strip()
    age = time.time() - TOKEN.stat().st_mtime
    if age > 86400:                          # refreshable once it's a day old; keeps the 60 days rolling
        r = call("GET", "https://graph.instagram.com/refresh_access_token",
                 {"grant_type": "ig_refresh_token", "access_token": tok})
        tok = r["access_token"]
        TOKEN.write_text(tok + "\n")
        os.chmod(TOKEN, 0o600)
        EXPIRES.write_text(str(dt.date.today() + dt.timedelta(seconds=r["expires_in"])) + "\n")
    return tok


def me(tok):
    return call("GET", API + "/me", {"fields": "user_id,username,account_type,media_count", "access_token": tok})


def cmd_whoami(_):
    tok = token()
    print(me(tok), "token expires", EXPIRES.read_text().strip() if EXPIRES.exists() else "?")


def cmd_upload(a):
    tok = token()
    uid = me(tok)["user_id"]
    caption = Path(a.caption_file).read_text().strip()
    c = call("POST", f"{API}/{uid}/media", {
        "media_type": "REELS", "video_url": a.video, "caption": caption,
        "share_to_feed": "true", "thumb_offset": a.thumb_offset_ms, "access_token": tok})
    print("container", c["id"], "- Instagram is fetching and processing the video")
    cmd_status(argparse.Namespace(container=c["id"]), wait=True)


def cmd_status(a, wait=False):
    tok = token()
    for _ in range(120 if wait else 1):
        s = call("GET", f"{API}/{a.container}", {"fields": "status_code,status", "access_token": tok})
        if not wait or s.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
            break
        time.sleep(5)
    print(a.container, s.get("status_code"), s.get("status", ""))
    if s.get("status_code") == "FINISHED":
        print("ready: `instagram_publish.py publish", a.container + "` makes it live")


def cmd_publish(a):
    tok = token()
    uid = me(tok)["user_id"]
    r = call("POST", f"{API}/{uid}/media_publish", {"creation_id": a.container, "access_token": tok})
    m = call("GET", f"{API}/{r['id']}", {"fields": "permalink,timestamp", "access_token": tok})
    print("published", r["id"], m.get("permalink"))


def cmd_insights(a):
    r = call("GET", f"{API}/{a.media_id}/insights", {
        "metric": "views,reach,likes,comments,shares,saved,ig_reels_avg_watch_time,ig_reels_video_view_total_time",
        "access_token": token()})
    for m in r.get("data", []):
        print(m["name"], m["values"][0]["value"])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    u = sub.add_parser("upload")
    u.add_argument("video")
    u.add_argument("--caption-file", required=True)
    u.add_argument("--thumb-offset-ms", type=int, default=2000)
    u.set_defaults(fn=cmd_upload)
    s = sub.add_parser("status")
    s.add_argument("container")
    s.set_defaults(fn=cmd_status)
    pb = sub.add_parser("publish")
    pb.add_argument("container")
    pb.set_defaults(fn=cmd_publish)
    i = sub.add_parser("insights")
    i.add_argument("media_id")
    i.set_defaults(fn=cmd_insights)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
