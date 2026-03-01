#!/usr/bin/env python3
"""
fetch_42.py
-----------
Combined fetcher for 42 project metadata and subject PDFs.

Phase 1 — Project metadata (via IntraAPI):
  Fetches all projects from cursus 21, saves to projects.json.
  Tracks project metadata changes (updated_at) in history.json.

Phase 2 — Subject PDFs (via IntraScrape):
  Scrapes projects.intra.42.fr for subject attachments.
  Downloads PDFs, extracts text, tracks word-level diffs in history.json.
  (Skipped automatically if SESSION_COOKIE is not set.)

Dependencies:
    pip install -r requirements.txt

Setup:
    cp .env.example .env
    # Fill in FT_CLIENT_ID, FT_CLIENT_SECRET for API access
    # Fill in SESSION_COOKIE, USER_ID_COOKIE, CF_CLEARANCE_COOKIE for scraping

Usage:
    python fetch_42.py                          # full run (projects + subjects)
    python fetch_42.py --skip-subjects          # project metadata only
    python fetch_42.py --keywords "python,dslr" # filter by keyword
    python fetch_42.py --dry-run                # list projects, no downloads
    python fetch_42.py --max 20                 # cap for testing
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from intra42 import IntraAPI, IntraScrape

CURSUS_ID = 21  # 42cursus


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


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """
    Extract plain text from PDF bytes.
    Requires: pip install pypdf
    Falls back gracefully if not installed.
    """
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)
    except ImportError:
        return "[pypdf not installed — run: pip install pypdf]"
    except Exception as e:
        return f"[PDF extraction failed: {e}]"


# ── Word-level diff ───────────────────────────────────────────────────────────

def word_diff(old_text: str, new_text: str) -> list[dict]:
    """
    Compute a word-level diff between two texts.
    Returns a list of chunks: {"type": "equal"|"insert"|"delete", "text": str}
    """
    old_words = re.findall(r'\S+|\s+', old_text)
    new_words = re.findall(r'\S+|\s+', new_text)

    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    chunks = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            chunks.append({"type": "equal",  "text": "".join(old_words[i1:i2])})
        elif op == "insert":
            chunks.append({"type": "insert", "text": "".join(new_words[j1:j2])})
        elif op == "delete":
            chunks.append({"type": "delete", "text": "".join(old_words[i1:i2])})
        elif op == "replace":
            chunks.append({"type": "delete", "text": "".join(old_words[i1:i2])})
            chunks.append({"type": "insert", "text": "".join(new_words[j1:j2])})
    return chunks


def diff_summary(chunks: list[dict]) -> dict:
    added   = sum(len(c["text"].split()) for c in chunks if c["type"] == "insert")
    removed = sum(len(c["text"].split()) for c in chunks if c["type"] == "delete")
    return {"words_added": added, "words_removed": removed}


# ── Keyword filter ────────────────────────────────────────────────────────────

def parse_keywords(raw: str) -> list[str]:
    if not raw:
        return []
    return [kw.strip().lower() for kw in raw.split(",") if kw.strip()]


def matches_keywords(project: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = (project.get("name") or "").lower() + " " + (project.get("slug") or "").lower()
    return any(kw in haystack for kw in keywords)


def matches_keywords_scrape(project: dict, keywords: list[str]) -> bool:
    """Keyword match for IntraScrape projects (have 'url' instead of 'slug')."""
    if not keywords:
        return True
    haystack = (project.get("name") or "").lower() + " " + (project.get("url") or "").lower()
    return any(kw in haystack for kw in keywords)


# ── History management ────────────────────────────────────────────────────────

def load_history(history_file: Path) -> dict:
    if history_file.exists():
        with open(history_file, encoding="utf-8") as f:
            return json.load(f)
    return {"projects": {}}


def save_history(history: dict, history_file: Path) -> None:
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ── Project metadata hash ────────────────────────────────────────────────────

def project_metadata_hash(detail: dict) -> str:
    """Hash the essential project metadata fields to detect changes."""
    fields = {
        "name":        detail.get("name"),
        "slug":        detail.get("slug"),
        "description": detail.get("description") or "",
        "exam":        detail.get("exam"),
        "difficulty":  detail.get("difficulty"),
        "duration":    detail.get("duration"),
        "updated_at":  detail.get("updated_at"),
    }
    raw = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch 42 project metadata & subject PDFs — combined tracker"
    )
    # API credentials
    parser.add_argument("--client-id",     default=os.getenv("FT_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.getenv("FT_CLIENT_SECRET"))
    # Scraping cookies (optional — subjects phase skipped if missing)
    parser.add_argument("--session-cookie",      default=os.getenv("SESSION_COOKIE"))
    parser.add_argument("--user-id-cookie",      default=os.getenv("USER_ID_COOKIE"))
    parser.add_argument("--cf-clearance-cookie",  default=os.getenv("CF_CLEARANCE_COOKIE"))
    # Output
    parser.add_argument("--output",        default=os.getenv("OUTPUT_FILE", "projects.json"))
    parser.add_argument("--history-file",  default=os.getenv("FT_HISTORY_FILE", "history.json"))
    parser.add_argument("--subjects-dir",  default=os.getenv("FT_SUBJECTS_DIR", "subjects"))
    # Filters
    parser.add_argument("--keywords",      default=os.getenv("FT_KEYWORDS", ""),
                        help="Comma-separated keywords to filter by name/slug.")
    parser.add_argument("--max",           type=int, default=None,
                        help="Cap number of projects (for testing)")
    # Flags
    parser.add_argument("--skip-subjects", action="store_true",
                        help="Skip subject PDF fetching (metadata only)")
    parser.add_argument("--dry-run",       action="store_true",
                        help="List projects without downloading anything")
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print("\nError: set FT_CLIENT_ID and FT_CLIENT_SECRET in your .env file.\n")
        sys.exit(1)

    subjects_dir = Path(args.subjects_dir)
    history_file = Path(args.history_file)
    subjects_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).isoformat()

    # ── Phase 1: API — project metadata ──────────────────────────────────────

    print("\n═══ Phase 1: Project Metadata (API) ═══\n", flush=True)

    print("→ Authenticating…", flush=True)
    try:
        api = IntraAPI(args.client_id, args.client_secret)
    except Exception as e:
        print(f"  ✗ Authentication failed: {e}", flush=True)
        print("\n  Checklist:", flush=True)
        print("  1. https://profile.intra.42.fr/oauth/applications — grant type: Client Credentials", flush=True)
        print(f"  2. FT_CLIENT_ID     = {args.client_id!r}", flush=True)
        print(f"  3. FT_CLIENT_SECRET = {'*' * len(args.client_secret)} ({len(args.client_secret)} chars)", flush=True)
        sys.exit(1)
    print("  ✓ Token obtained", flush=True)

    print(f"\n→ Fetching projects from cursus {CURSUS_ID} …", flush=True)
    summaries = api.get_all_pages(f"/v2/cursus/{CURSUS_ID}/projects")
    print(f"  ✓ {len(summaries)} project(s)", flush=True)

    # Keyword filter
    keywords = parse_keywords(args.keywords)
    if keywords:
        before = len(summaries)
        summaries = [p for p in summaries if matches_keywords(p, keywords)]
        print(f"\n→ Keyword filter {keywords}: {before} → {len(summaries)} match", flush=True)
        if not summaries:
            print("  ⚠ No projects matched.", flush=True)
            sys.exit(0)

    targets = summaries[:args.max] if args.max else summaries
    if args.max:
        print(f"  (capped at {args.max})", flush=True)

    if args.dry_run:
        print("\n[dry-run] Projects:")
        for p in targets:
            print(f"  {p.get('slug', p.get('name', '?'))}")
        return

    # Fetch full project details
    print(f"\n→ Fetching details for {len(targets)} project(s) …", flush=True)
    projects = []
    details_by_slug = {}
    for i, p in enumerate(targets, 1):
        try:
            detail = api.get(f"/v2/projects/{p['id']}")
        except Exception as exc:
            print(f"  [warn] {p.get('slug', p['id'])} failed: {exc} — using summary", flush=True)
            detail = p

        slug = detail.get("slug") or str(detail.get("id"))
        details_by_slug[slug] = detail
        projects.append({
            "id":          detail.get("id"),
            "name":        detail.get("name"),
            "slug":        slug,
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

    # Sort by most recently updated
    projects.sort(key=lambda p: p.get("updated_at") or "", reverse=True)

    # ── Track project metadata changes in history ────────────────────────────

    print(f"\n→ Tracking project metadata changes …", flush=True)
    history = load_history(history_file)
    meta_changes = 0

    for proj in projects:
        slug = proj["slug"]
        detail = details_by_slug.get(slug, proj)

        proj_history = history["projects"].setdefault(slug, {
            "name":     proj["name"],
            "slug":     slug,
            "versions": [],
        })

        # Update name in case it changed
        proj_history["name"] = proj["name"]

        # Compute hash of current metadata
        meta_hash = project_metadata_hash(detail)

        # Initialize project_changes list if not present
        if "project_changes" not in proj_history:
            proj_history["project_changes"] = []

        # Check if metadata changed since last recorded state
        last_meta_hash = (
            proj_history["project_changes"][-1]["hash"]
            if proj_history["project_changes"]
            else None
        )

        if meta_hash != last_meta_hash:
            proj_history["project_changes"].append({
                "timestamp":  run_ts,
                "hash":       meta_hash,
                "updated_at": detail.get("updated_at"),
            })
            proj_history["project_updated_at"] = detail.get("updated_at")
            meta_changes += 1

    print(f"  ✓ {meta_changes} project(s) with metadata changes detected", flush=True)
    save_history(history, history_file)

    # Write projects.json
    output = {
        "fetched_at": run_ts,
        "cursus_id":  CURSUS_ID,
        "total":      len(projects),
        "projects":   projects,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  ✓ Saved {len(projects)} projects → {args.output}", flush=True)

    # ── Phase 2: Subject PDFs (scraping) ─────────────────────────────────────

    if args.skip_subjects:
        print("\n  [skip] Subject fetching skipped (--skip-subjects)", flush=True)
    elif not args.session_cookie:
        print("\n  [skip] Subject fetching skipped — no SESSION_COOKIE set.", flush=True)
        print("         Set SESSION_COOKIE in .env to enable subject tracking.", flush=True)
    else:
        print("\n═══ Phase 2: Subject PDFs (Scraping) ═══\n", flush=True)

        cookies = {'_intra_42_session_production': args.session_cookie}
        if args.user_id_cookie:
            cookies['user.id'] = args.user_id_cookie
        if args.cf_clearance_cookie:
            cookies['cf_clearance'] = args.cf_clearance_cookie

        print("→ Initializing IntraScrape …", flush=True)
        scraper = IntraScrape(cookies)

        print("→ Fetching project list from projects.intra.42.fr …", flush=True)
        scrape_projects = scraper.get_all_projects()
        print(f"  ✓ {len(scrape_projects)} projects found", flush=True)

        # Keyword filter for scrape projects
        if keywords:
            scrape_projects = [
                p for p in scrape_projects
                if matches_keywords_scrape(p, keywords)
            ]
            print(f"  Filtered to {len(scrape_projects)} projects", flush=True)

        if not scrape_projects:
            print("  ⚠ No projects matched for subject fetching.", flush=True)
        else:
            print(f"\n→ Processing {len(scrape_projects)} project(s) …\n", flush=True)
            changed = 0
            skipped = 0
            errors  = 0

            for i, project in enumerate(scrape_projects, 1):
                name = project["name"]
                slug = project["url"].strip("/").split("/")[-1]
                print(f"  [{i}/{len(scrape_projects)}] {name}", flush=True)

                try:
                    attachments = scraper.get_project_attachments(project["url"])
                except Exception as e:
                    print(f"    [warn] attachment fetch failed: {e}", flush=True)
                    errors += 1
                    time.sleep(0.5)
                    continue

                pdf_urls = [url for url in attachments if url.lower().endswith(".pdf")]
                if not pdf_urls:
                    print(f"    [skip] no PDF attachments", flush=True)
                    skipped += 1
                    time.sleep(0.25)
                    continue

                pdf_url = pdf_urls[0]
                print(f"    PDF: {pdf_url}", flush=True)

                try:
                    resp = scraper.session.get(pdf_url, timeout=30)
                    if resp.status_code != 200:
                        raise Exception(f"HTTP {resp.status_code}")
                    pdf_bytes = resp.content
                except Exception as e:
                    print(f"    [warn] download failed: {e}", flush=True)
                    errors += 1
                    time.sleep(0.25)
                    continue

                pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
                proj_history = history["projects"].setdefault(slug, {
                    "name":     name,
                    "slug":     slug,
                    "versions": [],
                })
                if "project_changes" not in proj_history:
                    proj_history["project_changes"] = []

                last_hash = proj_history["versions"][-1]["hash"] if proj_history["versions"] else None
                if pdf_hash == last_hash:
                    print(f"    [=] unchanged", flush=True)
                    time.sleep(0.25)
                    continue

                new_text = extract_text_from_pdf(pdf_bytes)

                if proj_history["versions"]:
                    prev_txt_file = subjects_dir / slug / proj_history["versions"][-1]["text_file"]
                    try:
                        old_text = prev_txt_file.read_text(encoding="utf-8")
                    except Exception:
                        old_text = ""
                    chunks  = word_diff(old_text, new_text)
                    summary_ = diff_summary(chunks)
                    print(f"    [+] changed — +{summary_['words_added']} / -{summary_['words_removed']} words", flush=True)
                else:
                    chunks   = [{"type": "equal", "text": new_text}]
                    summary_ = {"words_added": len(new_text.split()), "words_removed": 0}
                    print(f"    [new] first version — {summary_['words_added']} words", flush=True)

                proj_dir = subjects_dir / slug
                proj_dir.mkdir(parents=True, exist_ok=True)
                ts_safe  = run_ts.replace(":", "-").replace("+", "")
                pdf_file  = f"{ts_safe}.pdf"
                txt_file  = f"{ts_safe}.txt"
                diff_file = f"{ts_safe}.diff.json"

                (proj_dir / pdf_file).write_bytes(pdf_bytes)
                (proj_dir / txt_file).write_text(new_text, encoding="utf-8")
                (proj_dir / diff_file).write_text(
                    json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8"
                )

                proj_history["versions"].append({
                    "timestamp":     run_ts,
                    "hash":          pdf_hash,
                    "pdf_file":      pdf_file,
                    "text_file":     txt_file,
                    "diff_file":     diff_file,
                    "words_added":   summary_["words_added"],
                    "words_removed": summary_["words_removed"],
                })

                changed += 1
                save_history(history, history_file)
                time.sleep(0.5)

            print(f"\n  ✓ Subjects — {changed} changed, {skipped} no PDF, {errors} errors", flush=True)

    # Final save
    save_history(history, history_file)

    print(f"\n{'═' * 50}")
    print(f"✓ All done")
    print(f"  Projects  → {args.output}")
    print(f"  History   → {history_file}")
    print(f"  Subjects  → {subjects_dir}/")
    print(f"  Dashboard → open dashboard.html")


if __name__ == "__main__":
    main()
