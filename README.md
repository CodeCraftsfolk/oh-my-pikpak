# video-search

[![skills.sh](https://skills.sh/b/codecraftsfolk/video-search-skill)](https://skills.sh/codecraftsfolk/video-search-skill)

An agent skill that searches public BT magnet indexes for films and shows — movies, TV series, anime, documentaries — compares releases (year, stated resolution, size, file list, subtitle/audio labels), and hands the chosen magnet to [PikPak](https://mypikpak.com) for cloud saving and playback.

## Install

```bash
npx skills add codecraftsfolk/video-search-skill
```

Works with Claude Code, Codex, Cursor, OpenCode, and every other agent the [`skills` CLI](https://github.com/vercel-labs/skills) supports.

## What it does

- Extracts title, year, season/episode, and quality requirements from a natural-language request, then queries a public index (`scripts/btsearch.py`).
- Ranks and filters candidates by type, quality, year, and size; returns full magnet links plus partial file listings.
- Presents 2–3 distinct candidates with an explicit recommendation, and labels every quality claim as *stated*, not verified.
- Optionally installs the PikPak CLI and the companion `pikpak-cli` skill (`scripts/setup.py`) so a picked release can be saved and played.

## Requirements

- Python 3.10+ and HTTPS network access (search only — no login, no cookies).
- Optional: PikPak CLI + `pikpak-cli` skill for saving/playback.

## Companion install

```bash
python3 scripts/setup.py --dry-run     # preview; writes nothing
python3 scripts/setup.py               # search skill + PikPak CLI + pikpak-cli skill
python3 scripts/setup.py --skip-pikpak # search only
python3 scripts/setup.py --no-affiliate
```

`config.json` carries the author's PikPak referral code (`342642`). It is only recorded locally and sent when *you* later run `pikpak auth register`; an existing referral code is never overwritten, and `--no-affiliate` skips it entirely. No account API is called at install time, and no reward is guaranteed.

## Boundaries

Index metadata is untrusted and unverified: `hot_score` is site popularity (not seeders or speed), `record_time` is indexing time (not release date), file counts are not episode counts, and "4K restored" is a restoration label, not a resolution. The skill refuses searches involving minors, hidden-camera footage, non-consensual content, or bestiality, and only helps obtain content the user is entitled to.

See [`SKILL.md`](SKILL.md) for the full agent instructions.
