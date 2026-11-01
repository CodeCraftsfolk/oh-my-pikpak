---
name: pikpak-drop
description: >-
  Hand off local files or folders through PikPak: upload them, create one expiring,
  password-protected share link, and reply with a ready-to-send message. Use whenever the
  user wants to send, share, deliver or hand off something too big or awkward for chat or
  email (Slack, WeChat, Telegram, Discord, email attachments), needs a download link for a
  file made in this session (rendered video, build artifact, dataset, report, logs,
  screenshots), or wants to open a local file on their phone, TV or another computer — even
  if PikPak is never mentioned. Also for 发给同事/客户、生成分享链接/下载链接、文件太大微信/邮件发不了、
  传到网盘给我个链接、手机上看这个视频. Not for browsing or organizing PikPak, magnet/offline
  downloads, or backups with no sharing (use pikpak-cli), and not for publishing code,
  deploying sites, or uploading to S3/GitHub releases.
compatibility: PikPak CLI (`pikpak`, logged in); Python 3.8+ for scripts/drop.py.
---

# PikPak Drop

Turns "send this to someone" into one link. `scripts/drop.py` uploads the given paths into a new dated folder inside PikPak › Drops, shares everything under one link, and prints JSON with a ready-to-send `message`.

Defaults, and why:
- **Random password, 7-day expiry.** Links get forwarded; an expiring, password-protected link limits the damage. Loosen only when the user asks (public README assets, permanent download pages).
- **One link for everything in the request.** The recipient has one thing to open.
- **A fresh folder per drop** (`Drops/2026-09-30 14-02-11 promo_final.mp4`). Sending `report.pdf` again never shows up as `report(1).pdf` for the recipient, and Drops doubles as a log of what was sent when.
- **Stops on secret-looking content before uploading anything** (`.env`, private keys, credential files, `.git/`, `node_modules/`), even deep inside folders. "Share the project folder" is exactly how API keys leak. OS clutter like `.DS_Store` is skipped silently.

## Run it

```bash
python3 <skill-dir>/scripts/drop.py <path> [<path> ...] [options]
```

Run it directly; there's no need for a dry run first. The script checks everything before it uploads, and when something needs a decision it stops without uploading. Use `--dry-run` only when the user asks what would be sent. Use `python` if `python3` doesn't exist (Windows). Pass absolute paths when unsure of the working directory.

| Want | Option |
|---|---|
| Expiry in days (`-1` = never) | `--expire-days 3` |
| No password (public link) | `--public` |
| A specific password | `--password 8492` |
| Link title | `--title "Q3 report"` |
| Chinese message text | `--lang zh` |
| Leave things out | `--exclude 'q3-report/.env' --exclude '*.log'` (glob on the shown path, the path inside the folder, or the name) |
| Include something already in PikPak | `--cloud 'Meetings/kickoff.mp4'` (path from the PikPak root; repeatable; works with or without local paths). `--id <fileID>` if you already have the ID |
| Put uploads in a specific PikPak folder | `--folder "Clients/Northwind"` (created if missing) |
| Limit how many times it can be saved | `--max-restores 5` |

Pass `--lang zh` when the user writes Chinese. Turn the user's wording into flags ("for a few days" → `--expire-days 3`, "anyone can open it" → `--public`) rather than asking.

## Reading the result

stdout is one JSON object; stderr is progress for humans.

| Exit | Meaning | Do |
|---|---|---|
| 0 | Done: `message`, `share` (`url`, `password`, `share_id`), `folder`, `items`, `excluded` | Reply (below). The JSON is the proof — don't list folders or shares to double-check |
| 3 | Blocked before any upload: `FLAGGED_FILES` or `INSUFFICIENT_SPACE` | See below |
| 4 | `NOT_INSTALLED`, `NOT_LOGGED_IN`, other auth errors | Setup |
| 2 | Bad arguments, missing local path, nothing left to upload, or `CLOUD_PATH_NOT_FOUND` (`error.details.similar` lists close names) | Fix and re-run |
| 1 | PikPak error: `error.code`, `error.hint`, `error.stage`, plus `folder` and `items` already uploaded | If `stage` is `share`, the uploads are done and the script already retried: run `error.details.command` (the exact share command) once more. Don't re-run the whole script; that uploads a second copy |

**FLAGGED_FILES** — `error.details.flagged` lists paths and reasons; each path works as-is with `--exclude`. Never upload them without the user's explicit OK. If you can ask, ask ("`q3-report/.env` looks like it holds API keys — leave it out?"). If the user told you to just get it done, re-run with `--exclude` for each flagged path and tell them what you left out and why. `--allow-flagged` is only for when the user confirms they mean to send it.

**INSUFFICIENT_SPACE** — report needed vs. free from `error.details`; suggest freeing space or upgrading. Don't retry.

## Replying

Lead with the text the user can forward: the `message` field in a code block so it copies cleanly, lines unchanged so the link and password stay together. If the user named the recipient, you may add one short line addressed to them at the top of the block, in the user's language ("Hi Dana — here's the final promo render:").

Then a short note for the user: where it lives (PikPak › Drops), when it expires, anything left out, and how to revoke (`pikpak share rm <share_id>`).

```
Hi Dana — here's the final promo render:
promo_final.mp4
Link: https://mypikpak.com/s/VP2h...
Password: aegx
Expires in 7 days
```
Saved to PikPak › Drops; left out `.env`. Revoke anytime: `pikpak share rm VP2h...`

What the recipient gets (checked on desktop web, Sept 2026): after entering the password they see the files, and videos play right in the browser, no account needed. Downloading asks them to save the files to a free PikPak account first (or use the PikPak app). When the recipient needs the file itself rather than just to watch or review it, say so in the note so the user isn't surprised.

Keep raw JSON, internal file IDs and direct download URLs out of the reply. A share link opens a PikPak page: it can't serve as an image source in Markdown (`![](...)`), a `<video>` source, or a direct download URL for scripts. For README images, suggest committing them to the repo.

## Opening it on the user's own phone or TV

The share link works on their phone too, and the file sits in the PikPak app under Drops. `pikpak play <id> --url-only` prints a direct stream URL for players like VLC or IINA, but it expires in about 24 hours and is tied to the user's account: only for their own devices, never for someone else. If `qrencode` is installed, `qrencode -t ANSIUTF8 '<share url>'` prints a QR code to scan.

## Large uploads

Uploads run at the user's upload speed; multi-GB files can take many minutes, so run the script with a long timeout or in the background. If an upload is interrupted, re-running the same command resumes it. Content PikPak already has is matched instantly (`instant_upload: true`).

## Setup (exit 4)

Exit 4 carries `error.details.setup` with installation and login commands. Ask the user to review and run installers themselves. Installation must not record a referral code.

- Install: `curl -fsSL https://download.mypikpak.com/cli/install.sh | bash` (macOS/Linux), `irm https://download.mypikpak.com/cli/install.ps1 | iex` (Windows), or `brew install pikcloud/tap/pikpak-cli`.
- Existing account: `pikpak auth login` — no referral code, ever. With a browser the user runs it themselves. Headless: run `pikpak auth login -m device -F json`, show the user `verification_uri` and `user_code`, then poll `pikpak auth login --continue <device_code> -F json` until `status` is `success`. Or a Personal Access Token: `pikpak auth token <PAT>` or `PIKPAK_TOKEN`. Drop needs the `files:read`, `files:write` and `share` scopes.
- No account yet: disclose the optional referral code and that it is sent to PikPak at registration, offer registration without a code, and use the code only with the user's explicit consent. With consent, use `pikpak auth register --affiliate 342642` (or the configured code); without it use `pikpak auth register --affiliate=` even if a code is saved locally. Promise no reward, discount or quota. Registration is interactive (`-m device` for a headless box); there is no `--continue` polling as there is for login, and `-F json` only prints a signup link for the user to open.
- Never ask for the user's password. `pikpak auth register` refuses while someone is logged in; never log a user out or re-register an existing account to attach a code.

### Where the code lives

`config.json` next to this file holds the optional `affiliate_code`, shipped as `342642` (a string). `PIKPAK_AFFILIATE` overrides it for one run. Blank, the `YOUR_AFFILIATE_CODE` placeholder, or anything outside `[A-Za-z0-9._-]` is dropped. Uploading and sharing never touch the code; registration uses it only with explicit consent.

## Without Python

The same flow by hand (PikPak's root folder ID is the empty string):

```bash
pikpak ls --type folder -F json                 # find "Drops", else: pikpak mkdir Drops -F json
pikpak upload <path> -p <dropsID> -w -F json    # file_id; a folder gives root_folder_id
pikpak share create <id> [<id> ...] -t "<title>" -e 7 -P -F json
```

`share create` returns `share_url` and `pass_code`. Its `share_text` is only the URL, so write the message yourself and include the password. Look inside folders for secrets before uploading.

## Managing drops

- List links: `pikpak share ls -F json` (`expiration` in days, `-1` = never).
- Revoke a link: `pikpak share rm <share_id>`. The files stay in Drops.
- Delete the files: `pikpak rm <id>` (moves them to PikPak's trash).
