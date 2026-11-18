# ShortPost

Free, open-source Windows tool for creators: drop one vertical video into a folder, let AI write separate text for each
platform, review it, and post or schedule it to YouTube Shorts, Instagram Reels, Facebook Reels, TikTok, Threads and
Bluesky (Pinterest is optional). Runs on your own PC; your videos and tokens never leave it.

Website: https://shortpost.medianopol.com/ - [Privacy](https://shortpost.medianopol.com/privacy-policy.html) - [Terms](https://shortpost.medianopol.com/terms-of-service.html) - MIT licence.

## Requirements

Windows 10/11, [uv](https://docs.astral.sh/uv/), FFmpeg, and either a free Google Gemini API key or a local
[Ollama](https://ollama.com) model for the text. Each platform needs your own account and, for most, your own free developer app.

## Before you use it: make it yours

`config.toml` and `affiliates.toml` ship with the author's values (time zones, brand hashtags, links). Edit them, or put
overrides in `%APPDATA%\ShortPost\config.toml`. Nothing is posted until you approve the preview.

## Install

```powershell
powershell -ExecutionPolicy Bypass -File install\install.ps1
uv run shortpost doctor
```

Then register the background task (runs `shortpost run` at logon and every 15 minutes while you are logged on):

```powershell
powershell -ExecutionPolicy Bypass -File install\register-task.ps1
```

## Daily use

1. Drop the video into `inbox\` (optionally a `<same-name>.txt` with notes, e.g. `link: dogma`).
2. Run `uv run shortpost review` (or `shortpost add <file>`). It shows the text for every platform and the date; answer Y to approve.
   The background run also sends an ntfy push when new files sit in the inbox.
3. YouTube and Facebook are scheduled on their side right away. Instagram, Threads, Bluesky and Pinterest post at 20:00
   Bucharest time on the same New York date, started by the background run.
4. When the TikTok push arrives, open TikTok, finish the draft in your inbox, paste the caption and post or schedule it.

Other commands: `list` (calendar), `show <id>`, `cancel <id>`, `reschedule <id> <date>`, `sync` (read days already scheduled on YouTube),
`run` (what the task runs), `doctor`.

## Setup per platform

Short version:

| Platform | One-time setup |
|---|---|
| YouTube | `shortpost auth youtube-browser`, log in to your Google account in the window that opens |
| Facebook + Instagram | Meta app + system-user token, then `shortpost auth facebook` |
| Threads | add yourself as Threads tester, paste a token into `shortpost auth threads` |
| TikTok | `shortpost auth tiktok` (uses the public ShortPost relay by default; or set `tiktok_client_key/secret` from your own developer app) |
| Bluesky | app password, `set-secret bluesky_handle/bluesky_app_password`, then set `platforms.bluesky.enabled = true` |
| Pinterest | developer app, `set-secret pinterest_app_id/secret`, `shortpost auth pinterest`; enable only after Standard access |
| Phone alerts | `shortpost set-secret ntfy_topic --generate`, subscribe to that topic in the ntfy app |

Secrets and tokens live in `%APPDATA%\ShortPost\`, never in the repo.

## Troubleshooting

- Run `uv run shortpost doctor` first. It lists missing tokens, expiring tokens and the Task Scheduler entry.
- Logs are in `logs\shortpost-YYYY-MM-DD.log` (files older than 30 days are deleted automatically).
- A post that fails is retried after 15, 60 and 240 minutes, then you get an ntfy push and `shortpost list` shows it as failed.
- A post whose time passed more than 20 hours ago while the PC was off is marked `missed` instead of posting late.
- "Another run is active": a previous `run` is still going. A lock older than 2 hours is ignored automatically.
- YouTube Studio automation failures leave a screenshot in `logs\screenshots\`.
