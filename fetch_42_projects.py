#!/usr/bin/env python3
"""
fetch_42_projects.py
--------------------
Fetches all projects from cursus 21 (42cursus / Python common core)
and saves them to projects.json for use with dashboard.html.

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
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import HTTPError

UA        = "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
API_BASE  = "https://api.intra.42.fr"
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


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_token(client_id: str, client_secret: str) -> str:
    client_id     = client_id.strip()
    client_secret = client_secret.strip()

    payload = urlencode({
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()

    req = Request(
        f"{API_BASE}/oauth/token",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent":   UA,
        },
    )

    try:
        with urlopen(req) as resp:
            return json.loads(resp.read())["access_token"]
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        print(f"\n  ✗ Authentication failed — HTTP {e.code} {e.reason}", flush=True)
        if body:
            print(f"  API response: {body}", flush=True)
        print("\n  Checklist:", flush=True)
        print("  1. https://profile.intra.42.fr/oauth/applications — grant type: Client Credentials", flush=True)
        print(f"  2. FT_CLIENT_ID     = {client_id!r}", flush=True)
        print(f"  3. FT_CLIENT_SECRET = {'*' * len(client_secret)} ({len(client_secret)} chars)", flush=True)
        sys.exit(1)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api_get(path: str, token: str, params: dict = None, retries: int = 5):
    qs = ("?" + urlencode(params)) if params else ""
    url = f"{API_BASE}{path}{qs}"
    req = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent":    UA,
    })
    for attempt in range(retries):
        try:
            with urlopen(req) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt
                print(f"  [rate limit] waiting {wait}s…", flush=True)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Failed after {retries} retries: {url}")


def get_all_pages(path: str, token: str) -> list:
    results = []
    page = 1
    per_page = 100
    while True:
        data = api_get(path, token, {"page[number]": page, "page[size]": per_page})
        if not data:
            break
        results.extend(data)
        print(f"  page {page}: +{len(data)} (total: {len(results)})", flush=True)
        if len(data) < per_page:
            break
        page += 1
        time.sleep(0.3)
    return results



# ── Project list cache ────────────────────────────────────────────────────────

def load_cache(cache_file, max_age_hours):
    if not cache_file.exists():
        return None
    age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        print(f"  [cache] expired ({age_hours:.1f}h old, limit {max_age_hours}h)", flush=True)
        return None
    with open(cache_file, encoding="utf-8") as f:
        data = json.load(f)
    print(f"  [cache] hit — {len(data)} projects ({age_hours:.1f}h old)", flush=True)
    return data


def save_cache(summaries, cache_file):
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False)
    print(f"  [cache] saved {len(summaries)} projects → {cache_file}", flush=True)

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
    parser.add_argument("--cache-file", default=os.getenv("FT_CACHE_FILE", ".cache_summaries.json"),
                        help="Cache file path (default: .cache_summaries.json)")
    parser.add_argument("--cache-ttl",  type=float, default=float(os.getenv("FT_CACHE_TTL_HOURS", "24")),
                        help="Cache TTL in hours (default: 24). Set 0 to always refresh.")
    parser.add_argument("--no-cache",   action="store_true",
                        help="Ignore cache and force a fresh fetch.")
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print(
            "\nError: credentials not found.\n"
            "  Set FT_CLIENT_ID and FT_CLIENT_SECRET in your .env file.\n"
        )
        sys.exit(1)

    cache_file = Path(args.cache_file)

    # 1. Try loading from cache first
    summaries = None
    if not args.no_cache and args.cache_ttl > 0:
        print(f"\n→ Checking cache ({cache_file}) …", flush=True)
        summaries = load_cache(cache_file, args.cache_ttl)

    # 2. Auth (always needed — for detail fetches)
    print("\n→ Authenticating…", flush=True)
    token = get_token(args.client_id, args.client_secret)
    print("  ✓ Token obtained", flush=True)

    # 3. Fetch summaries from API if cache missed
    if summaries is None:
        print(f"\n→ Fetching projects from GET /v2/cursus/{CURSUS_ID}/projects …", flush=True)
        summaries = get_all_pages(f"/v2/cursus/{CURSUS_ID}/projects", token)
        print(f"  ✓ {len(summaries)} project(s) in cursus {CURSUS_ID}", flush=True)
        save_cache(summaries, cache_file)
    else:
        print(f"  ✓ Using cached project list ({len(summaries)} projects)", flush=True)

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
            detail = api_get(f"/v2/projects/{p['id']}", token)
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

    # 4. Sort by most recently updated
    projects.sort(key=lambda p: p.get("updated_at") or "", reverse=True)

    # 5. Write output
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
