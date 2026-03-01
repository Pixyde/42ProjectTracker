#!/usr/bin/env python3
"""
fetch_42_projects.py
--------------------
Fetches all projects from cursus 21 (42cursus / Python common core)
and saves them to projects.json for use with dashboard.html.

Uses the intra42 module (from timotif/intra_42) for API access:
  - IntraAPI: OAuth2 client for the 42 API

Setup:
    cp .env.example .env
    # Fill in FT_CLIENT_ID and FT_CLIENT_SECRET

Usage:
    python fetch_42_projects.py
    python fetch_42_projects.py --output my_data.json
    python fetch_42_projects.py --max 20   # cap for testing
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from intra42 import IntraAPI

CURSUS_ID = 21  # 42cursus — new Python common core


# ── .env loader ───────────────────────────────────────────────────────────────

def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        print(f"  [.env] no .env found at {env_path.resolve()} — skipping", flush=True)
        return
    try:
        from dotenv import load_dotenv as _load
        _load(dotenv_path=env_path, override=False)
        print(f"  [.env] loaded via python-dotenv ({env_path})", flush=True)
        return
    except ImportError:
        pass
    loaded = 0
    with open(env_path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
                loaded += 1
    print(f"  [.env] loaded {loaded} variable(s) from {env_path} (built-in parser)", flush=True)


load_dotenv()


# ── Keyword filter ───────────────────────────────────────────────────────────

def parse_keywords(raw: str) -> list[str]:
    """Split a comma-separated keyword string into a lowercase list."""
    if not raw:
        return []
    return [kw.strip().lower() for kw in raw.split(",") if kw.strip()]


def matches_keywords(project: dict, keywords: list[str]) -> bool:
    """True if any keyword appears in the project name or slug. Empty = match all."""
    if not keywords:
        return True
    haystack = (project.get("name") or "").lower() + " " + (project.get("slug") or "").lower()
    return any(kw in haystack for kw in keywords)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch cursus 21 projects → projects.json")
    parser.add_argument("--client-id",     default=os.getenv("FT_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.getenv("FT_CLIENT_SECRET"))
    parser.add_argument("--output",        default=os.getenv("OUTPUT_FILE", "projects.json"))
    parser.add_argument("--max",           type=int, default=None,
                        help="Cap number of projects to detail-fetch (for testing)")
    parser.add_argument("--keywords",      default=os.getenv("FT_KEYWORDS", ""),
                        help="Comma-separated keywords to filter by name/slug. "
                             "Empty = no filter (fetch all). E.g. 'python,dslr'")
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print(
            "\nError: credentials not found.\n"
            "  Set FT_CLIENT_ID and FT_CLIENT_SECRET in your .env file.\n"
        )
        sys.exit(1)

    # 1. Auth via IntraAPI (automatic OAuth2 client credentials)
    print("\n→ Authenticating…", flush=True)
    try:
        api = IntraAPI(args.client_id, args.client_secret)
    except Exception as e:
        print(f"\n  ✗ Authentication failed: {e}", flush=True)
        print("\n  Checklist:", flush=True)
        print("  1. https://profile.intra.42.fr/oauth/applications — grant type: Client Credentials", flush=True)
        print(f"  2. FT_CLIENT_ID     = {args.client_id!r}", flush=True)
        print(f"  3. FT_CLIENT_SECRET = {'*' * len(args.client_secret)} ({len(args.client_secret)} chars)", flush=True)
        sys.exit(1)
    print("  ✓ Token obtained", flush=True)

    # 2. Fetch project summaries from API
    print(f"\n→ Fetching projects from GET /v2/cursus/{CURSUS_ID}/projects …", flush=True)
    summaries = api.get_all_pages(f"/v2/cursus/{CURSUS_ID}/projects")
    print(f"  ✓ {len(summaries)} project(s) in cursus {CURSUS_ID}", flush=True)

    # 3. Keyword filter on summary data (name + slug) — no extra API calls
    keywords = parse_keywords(args.keywords)
    print(f"\n→ Raw keywords value: {args.keywords!r}", flush=True)
    print(f"  Parsed keywords:    {keywords}", flush=True)
    if keywords:
        before   = len(summaries)
        summaries = [p for p in summaries if matches_keywords(p, keywords)]
        print(f"\n→ Keyword filter {keywords}: {before} → {len(summaries)} project(s) match", flush=True)
        if not summaries:
            print("  ⚠ No projects matched. Try broader keywords or remove --keywords.", flush=True)
            sys.exit(0)
    else:
        print("  (no keyword filter)", flush=True)

    targets = summaries[:args.max] if args.max else summaries
    if args.max:
        print(f"  (capped at {args.max})", flush=True)

    # 4. Fetch full details for each project
    print(f"\n→ Fetching details for {len(targets)} project(s) via GET /v2/projects/:id …", flush=True)
    projects = []
    for i, p in enumerate(targets, 1):
        try:
            detail = api.get(f"/v2/projects/{p['id']}")
        except Exception as exc:
            print(f"  [warn] {p.get('slug', p['id'])} failed: {exc} — using summary", flush=True)
            detail = p

        projects.append({
            "id":          detail.get("id"),
            "name":        detail.get("name"),
            "slug":        detail.get("slug"),
            "description": detail.get("description") or "",
            "updated_at":  detail.get("updated_at"),
            "created_at":  detail.get("created_at"),
            "exam":        bool(detail.get("exam")),
            "difficulty":  detail.get("difficulty"),
            "duration":    detail.get("duration"),
            "project_sessions": [
                {
                    "id":     s.get("id"),
                    "campus": s.get("campus", {}).get("name") if s.get("campus") else None,
                }
                for s in (detail.get("project_sessions") or [])
            ],
        })

        if i % 10 == 0 or i == len(targets):
            print(f"  [{int(i/len(targets)*100):3d}%] {i}/{len(targets)} — {detail.get('name', '?')}", flush=True)

        time.sleep(0.25)

    # 5. Sort by most recently updated
    projects.sort(key=lambda p: p.get("updated_at") or "", reverse=True)

    # 6. Write output
    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "cursus_id":  CURSUS_ID,
        "total":      len(projects),
        "projects":   projects,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Done — saved {len(projects)} projects to '{args.output}'")
    print(f"  Open dashboard.html in your browser and load this file.")


if __name__ == "__main__":
    main()
