# oh-my-pikpak

Agent skills for finding videos and sharing files through [PikPak](https://mypikpak.com).

## Install

```bash
npx skills add CodeCraftsfolk/oh-my-pikpak --skill video-search
npx skills add CodeCraftsfolk/oh-my-pikpak --skill pikpak-drop
```

Works with Claude Code, Codex, Cursor, OpenCode, and every other agent the [`skills` CLI](https://github.com/vercel-labs/skills) supports.

## video-search

- Extracts title, year, season/episode, and quality requirements from a natural-language request, then queries a public index (`skills/video-search/scripts/btsearch.py`).
- Ranks and filters candidates by type, quality, year, and size; returns full magnet links plus partial file listings.
- Presents 2–3 distinct candidates with an explicit recommendation, and labels every quality claim as *stated*, not verified.
- Saves and plays a chosen release via an already installed PikPak CLI; searching needs neither CLI nor account. The skill never downloads or runs third-party installers.

### Requirements

- Python 3.10+ and HTTPS network access for search (no login or cookies).
- Optional, user-installed PikPak CLI for saving/playback; the separate `pikpak-cli` skill is optional.

## pikpak-drop

Upload local files or folders to PikPak and create a single expiring, password-protected share link. The skill checks for sensitive files before upload; Python 3.8+ and the PikPak CLI are required. See [`skills/pikpak-drop/SKILL.md`](skills/pikpak-drop/SKILL.md).

## PikPak setup

Install the PikPak CLI yourself using the vendor's installer at `https://download.mypikpak.com/cli/install.sh` (Windows: `https://download.mypikpak.com/cli/install.ps1`). Review the installer before running it. Video search works without the CLI or an account.

Each skill's `config.json` contains the author's optional referral code (`342642`). For a new account, the agent must disclose the code and use it only with your explicit consent at registration; you may register without a code using `pikpak auth register --affiliate=`. Existing accounts simply log in; no reward is guaranteed.

## Boundaries

Index metadata is untrusted and unverified: `hot_score` is site popularity (not seeders or speed), `record_time` is indexing time (not release date), file counts are not episode counts, and "4K restored" is a restoration label, not a resolution. The skill refuses searches involving minors, hidden-camera footage, non-consensual content, or bestiality, and only helps obtain content the user is entitled to.

See [`skills/video-search/SKILL.md`](skills/video-search/SKILL.md) for the full search instructions.
