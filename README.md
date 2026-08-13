# 42 Project Subject Tracker

A Python tool that watches 42 School project subject PDFs for changes and displays the history in a single self-contained dashboard.

On each run, it scrapes `projects.intra.42.fr` for the subject PDFs attached to your cursus projects, downloads them, extracts the text, and diffs each new version against the last one it saw. The result is written to `data.json`, which `dashboard.html` reads directly — no server or build step required.

## Features

- One-time bootstrap via the 42 API to build the initial project list (skipped automatically once `data.json` exists)
- Scrapes subject PDFs from `projects.intra.42.fr` and tracks them by update timestamp
- Word-level diffing between subject versions, so you can see exactly what changed
- Single static `dashboard.html` — open it in a browser, no backend needed
- Keyword filtering, dry-run mode, and a `--max` cap for quick testing

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy the environment template and fill it in:
   ```bash
   cp env.example .env
   ```
   - `FT_CLIENT_ID` / `FT_CLIENT_SECRET` — a 42 OAuth app (client credentials grant), only needed for the first-time project list bootstrap. Create one at [profile.intra.42.fr/oauth/applications](https://profile.intra.42.fr/oauth/applications).
   - `SESSION_COOKIE`, `USER_ID_COOKIE`, `CF_CLEARANCE_COOKIE` — session cookies for `projects.intra.42.fr`, required for the actual subject scraping. Grab these from your browser's DevTools (Application → Cookies) after logging in. They expire periodically and need refreshing.

## Usage

```bash
python fetch_42.py                          # normal run — fetch/track subjects
python fetch_42.py --force-setup            # re-run the initial API bootstrap
python fetch_42.py --keywords "python,dslr" # only track projects matching these keywords
python fetch_42.py --dry-run                # list matching projects without downloading
python fetch_42.py --max 20                 # cap how many projects are processed
```

Then open `dashboard.html` in a browser to view the tracked subjects and their change history.

## Project structure

```
fetch_42.py       # main fetcher — CLI entry point, orchestrates the run
intra42.py        # scraper for projects.intra.42.fr (project list + attachments)
dashboard.html    # static dashboard that reads data.json
env.example        # environment variable template
requirements.txt  # Python dependencies
```

## Notes

- Session cookies (not just API credentials) are required because subject PDFs are only accessible through the authenticated web session, not the public API.
- `data.json` is the single source of truth for the dashboard — delete it to force the bootstrap step to run again.
