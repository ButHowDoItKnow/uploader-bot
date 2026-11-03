# uploader-bot
Posts the next queued Short to YouTube + Instagram when the channel's views dry up. Logic and rules: top of `bot.py`.
- Queue: `queue.json` (order = posting order) + mp4s on the `queue` release. Adding an item = approval to post it.
- Pause: repo variable `BOT_PAUSED=1` (`gh variable set BOT_PAUSED --body 1`), resume with `--body 0`.
- Every tick's poll goes to `state/polls.csv` (hour-by-hour view curve); posts and failures open an issue.
- Tokens: Actions secrets `YT_TOKEN_JSON`, `IG_TOKEN`, `IG_TOKEN_EXPIRES`; refresh with `python bot.py push-secrets` (IG token: 60 days).
