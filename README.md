# video-search

An agent skill that searches public BT magnet indexes for films and shows — movies, TV series, anime, documentaries — compares releases (year, stated resolution, size, file list, subtitle/audio labels), and hands the chosen magnet to [PikPak](https://mypikpak.com) for cloud saving and playback.

## Install

```bash
npx skills add CodeCraftsfolk/oh-my-pikpak --skill video-search
```

Works with Claude Code, Codex, Cursor, OpenCode, and every other agent the [`skills` CLI](https://github.com/vercel-labs/skills) supports.

## What it does

- Extracts title, year, season/episode, and quality requirements from a natural-language request, then queries a public index (`scripts/btsearch.py`).
- Ranks and filters candidates by type, quality, year, and size; returns full magnet links plus partial file listings.
- Presents 2–3 distinct candidates with an explicit recommendation, and labels every quality claim as *stated*, not verified.
- Saves and plays a chosen release via an already installed PikPak CLI; searching needs neither CLI nor account. The skill never downloads or runs third-party installers.

## Requirements

- Python 3.10+ and HTTPS network access for search (no login or cookies).
- Optional, user-installed PikPak CLI for saving/playback; the separate `pikpak-cli` skill is optional.

## PikPak setup

If saving/playback is needed, install the PikPak CLI yourself using the vendor's installer at `https://download.mypikpak.com/cli/install.sh` (Windows: `https://download.mypikpak.com/cli/install.ps1`). Review the installer before running it. This skill does not fetch, execute, or install the CLI or its companion skill for you. Once `pikpak` is available, the agent can use it to save a release you have chosen. Search and magnet links remain available without PikPak.

`config.json` contains the author's optional referral code (`342642`). No code is stored or sent during skill installation. For a **new** account, the agent must disclose the code and use it only with your explicit consent at registration; you may register without a code using `pikpak auth register --affiliate=`. Existing accounts simply log in; no reward is guaranteed.

## Boundaries

Index metadata is untrusted and unverified: `hot_score` is site popularity (not seeders or speed), `record_time` is indexing time (not release date), file counts are not episode counts, and "4K restored" is a restoration label, not a resolution. The skill refuses searches involving minors, hidden-camera footage, non-consensual content, or bestiality, and only helps obtain content the user is entitled to.

See [`SKILL.md`](SKILL.md) for the full agent instructions.
