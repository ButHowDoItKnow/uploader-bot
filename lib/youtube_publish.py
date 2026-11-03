#!/usr/bin/env python3
"""
youtube_publish.py — the "Cyrex Publisher" YouTube side: log in once, check access, upload privately,
pull retention curves.

Credentials live outside the git repo, in ~/.config/cyrex/tokens/:
    google_client.json   OAuth Desktop client (downloaded from Google Cloud, project cyrex-publisher)
    youtube_token.json   refresh token written by `auth` (chmod 600). Delete it, or revoke at
                         myaccount.google.com/permissions, to cut access.

Usage (with Projects/edu-videos/.venv-publish/bin/python):
    youtube_publish.py auth                  # opens the browser once; sign in as buthowdoitknow@gmail.com
    youtube_publish.py whoami                # channel name, handle, video count, analytics reachable?
    youtube_publish.py upload VIDEO.mp4 --title T [--description D] [--tags a,b]   # always private
    youtube_publish.py retention VIDEO_ID [--csv OUT.csv]   # audienceWatchRatio per 1 % of the video

Install:  pip install google-auth-oauthlib google-api-python-client
"""
import argparse
import csv
import datetime as dt
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

TOKENS = Path.home() / ".config/cyrex/tokens"
CLIENT = TOKENS / "google_client.json"
TOKEN = TOKENS / "youtube_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",      # edit titles/descriptions/captions
    "https://www.googleapis.com/auth/yt-analytics.readonly",  # retention curves
]


def creds():
    c = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if c and c.expired and c.refresh_token:
        c.refresh(Request())
        save(c)
    if not c or not c.valid:
        sys.exit("not logged in: run `youtube_publish.py auth` first")
    return c


def save(c):
    TOKEN.write_text(c.to_json())
    os.chmod(TOKEN, 0o600)


def cmd_auth(_):
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    c = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                              success_message="Cyrex Publisher is connected. You can close this tab.")
    save(c)
    print("saved", TOKEN, "(refresh token:", "yes" if c.refresh_token else "NO", ")")


def cmd_whoami(_):
    c = creds()
    yt = build("youtube", "v3", credentials=c)
    ch = yt.channels().list(part="snippet,statistics,status", mine=True).execute()["items"][0]
    s = ch["snippet"]
    print(f"channel: {s['title']}  {s.get('customUrl', '')}  id={ch['id']}")
    print(f"videos: {ch['statistics'].get('videoCount')}  subscribers: {ch['statistics'].get('subscriberCount')}"
          f"  madeForKids: {ch['status'].get('madeForKids')}")
    ya = build("youtubeAnalytics", "v2", credentials=c)
    today = dt.date.today()
    r = ya.reports().query(ids="channel==MINE", startDate=str(today - dt.timedelta(days=28)),
                           endDate=str(today), metrics="views").execute()
    print("analytics: reachable, views last 28 d =", (r.get("rows") or [[0]])[0][0])


def cmd_upload(a):
    yt = build("youtube", "v3", credentials=creds())
    body = {
        "snippet": {"title": a.title, "description": a.description,
                    "tags": [t for t in a.tags.split(",") if t], "categoryId": "28"},  # Science & Technology
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(a.video, chunksize=8 << 20, resumable=True))
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"  {status.progress():.0%}", flush=True)
    print("uploaded (private):", f"https://youtu.be/{resp['id']}", "-> flip to Public in YouTube Studio")


def cmd_retention(a):
    ya = build("youtubeAnalytics", "v2", credentials=creds())
    r = ya.reports().query(ids="channel==MINE", startDate="2026-01-01", endDate=str(dt.date.today()),
                           metrics="audienceWatchRatio,relativeRetentionPerformance",
                           dimensions="elapsedVideoTimeRatio", filters=f"video=={a.video_id}").execute()
    rows = r.get("rows") or []
    if not rows:
        print("no retention data yet (YouTube needs some views first, and lags ~1-2 days)")
        return
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["elapsed_ratio", "audience_watch_ratio", "relative_retention"])
            w.writerows(rows)
        print("wrote", a.csv, len(rows), "rows")
    else:
        for row in rows:
            print(*row, sep="\t")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth").set_defaults(fn=cmd_auth)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    u = sub.add_parser("upload")
    u.add_argument("video")
    u.add_argument("--title", required=True)
    u.add_argument("--description", default="")
    u.add_argument("--tags", default="")
    u.set_defaults(fn=cmd_upload)
    r = sub.add_parser("retention")
    r.add_argument("video_id")
    r.add_argument("--csv")
    r.set_defaults(fn=cmd_retention)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
