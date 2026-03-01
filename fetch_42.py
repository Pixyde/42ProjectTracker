#!/usr/bin/env python3
"""
fetch_42.py
-----------
Fetcher for 42 project subject PDFs with change tracking.
Outputs a single data.json used directly by dashboard.html.

First-time setup (automatic):
  Uses the 42 API to fetch the initial project list from cursus 21.
  Stores project metadata (name, slug, updated_at) as a baseline.
  Only runs once — skipped automatically when data.json already exists.

Subject tracking (every run):
  Scrapes projects.intra.42.fr for subject PDF attachments.
  Downloads PDFs, extracts text, tracks word-level diffs.
  Subject version timestamps drive the dashboard activity tracker.

Dependencies:
    pip install -r requirements.txt

Setup:
    cp .env.example .env
    # Fill in FT_CLIENT_ID, FT_CLIENT_SECRET for first-time API setup
    # Fill in SESSION_COOKIE, USER_ID_COOKIE, CF_CLEARANCE_COOKIE for scraping

Usage:
    python fetch_42.py                          # normal run (subjects only)
    python fetch_42.py --force-setup            # re-run API setup
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


# ── Data file management ──────────────────────────────────────────────────────

def load_data(data_file: Path) -> dict:
    """Load the combined data file (projects + history)."""
    if data_file.exists():
        with open(data_file, encoding="utf-8") as f:
            data = json.load(f)
            # Ensure history structure exists
            data.setdefault("history", {"projects": {}})
            data["history"].setdefault("projects", {})
            return data
    return {"history": {"projects": {}}}


def save_data(data: dict, data_file: Path) -> None:
    """Save the combined data file (projects + history)."""
    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch 42 subject PDFs — track changes over time"
    )
    # API credentials (only needed for first-time setup)
    parser.add_argument("--client-id",     default=os.getenv("FT_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.getenv("FT_CLIENT_SECRET"))
    # Scraping cookies
    parser.add_argument("--session-cookie",      default=os.getenv("SESSION_COOKIE"))
    parser.add_argument("--user-id-cookie",      default=os.getenv("USER_ID_COOKIE"))
    parser.add_argument("--cf-clearance-cookie",  default=os.getenv("CF_CLEARANCE_COOKIE"))
    # Output
    parser.add_argument("--output",        default=os.getenv("OUTPUT_FILE", "data.json"),
                        help="Combined output file (default: data.json)")
    parser.add_argument("--subjects-dir",  default=os.getenv("FT_SUBJECTS_DIR", "subjects"))
    # Filters
    parser.add_argument("--keywords",      default=os.getenv("FT_KEYWORDS", ""),
                        help="Comma-separated keywords to filter by name/slug.")
    parser.add_argument("--max",           type=int, default=None,
                        help="Cap number of projects (for testing)")
    # Flags
    parser.add_argument("--force-setup",   action="store_true",
                        help="Force re-run of API setup even if data.json exists")
    parser.add_argument("--dry-run",       action="store_true",
                        help="List projects without downloading anything")
    args = parser.parse_args()

    subjects_dir = Path(args.subjects_dir)
    data_file = Path(args.output)
    subjects_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).isoformat()

    # Load existing combined data (for history continuity)
    existing = load_data(data_file)
    history = existing["history"]
    projects = existing.get("projects", [])

    keywords = parse_keywords(args.keywords)

    # ── Initial Setup: API (first run only) ──────────────────────────────────

    if not projects or args.force_setup:
        print("\n═══ Initial Setup: Project List (API) ═══\n", flush=True)

        if not args.client_id or not args.client_secret:
            print("Error: First-time setup requires FT_CLIENT_ID and FT_CLIENT_SECRET in .env\n")
            print("  After initial setup, API credentials are no longer needed.")
            sys.exit(1)

        print("→ Authenticating…", flush=True)
        try:
            api = IntraAPI(args.client_id, args.client_secret)
        except Exception as e:
            print(f"  ✗ Authentication failed: {e}", flush=True)
            sys.exit(1)
        print("  ✓ Token obtained", flush=True)

        print(f"\n→ Fetching projects from cursus {CURSUS_ID} …", flush=True)
        summaries = api.get_all_pages(f"/v2/cursus/{CURSUS_ID}/projects")
        print(f"  ✓ {len(summaries)} project(s)", flush=True)

        # Keyword filter
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

        # Build project list from summaries (fast — no per-project detail fetch)
        projects = []
        for p in targets:
            slug = p.get("slug") or str(p.get("id"))
            projects.append({
                "id":          p.get("id"),
                "name":        p.get("name"),
                "slug":        slug,
                "description": p.get("description") or "",
                "updated_at":  p.get("updated_at"),
                "created_at":  p.get("created_at"),
                "exam":        bool(p.get("exam")),
                "difficulty":  p.get("difficulty"),
                "duration":    p.get("duration"),
            })

            # Seed history entry with API updated_at as baseline
            history["projects"].setdefault(slug, {
                "name":     p.get("name"),
                "slug":     slug,
                "project_updated_at": p.get("updated_at"),
                "versions": [],
            })

        projects.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
        print(f"\n  ✓ {len(projects)} projects loaded (initial setup complete)", flush=True)
        print("  ℹ  API credentials are no longer needed for subsequent runs.", flush=True)
    else:
        print(f"\n═══ Projects already loaded ({len(projects)}) — skipping API setup ═══\n", flush=True)
        if args.dry_run:
            print("[dry-run] Projects:")
            for p in projects:
                print(f"  {p.get('slug', p.get('name', '?'))}")
            return

    # ── Subject PDFs (scraping — every run) ──────────────────────────────────

    if not args.session_cookie:
        print("  [skip] Subject fetching skipped — no SESSION_COOKIE set.", flush=True)
        print("         Set SESSION_COOKIE in .env to enable subject tracking.", flush=True)
    else:
        print("═══ Subject PDFs (Scraping) ═══\n", flush=True)

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
                # Incremental save after each change
                output = {
                    "fetched_at": run_ts,
                    "cursus_id":  CURSUS_ID,
                    "total":      len(projects),
                    "projects":   projects,
                    "history":    history,
                }
                save_data(output, data_file)
                time.sleep(0.5)

            print(f"\n  ✓ Subjects — {changed} changed, {skipped} no PDF, {errors} errors", flush=True)

    # Final save
    output = {
        "fetched_at": run_ts,
        "cursus_id":  CURSUS_ID,
        "total":      len(projects),
        "projects":   projects,
        "history":    history,
    }
    save_data(output, data_file)

    print(f"\n{'═' * 50}")
    print(f"✓ All done")
    print(f"  Data      → {data_file}")
    print(f"  Subjects  → {subjects_dir}/")
    print(f"  Dashboard → open dashboard.html")


if __name__ == "__main__":
    main()
