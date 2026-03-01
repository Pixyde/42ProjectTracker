#!/usr/bin/env python3
"""
fetch_42_subjects.py
--------------------
Downloads subject PDFs for 42 projects, extracts their text, and keeps a
versioned history. On each run it compares the new text against the last
saved version and records a word-level diff in history.json.

Uses the intra42 module (from timotif/intra_42) for subject fetching:
  - IntraScrape: web-scrapes projects.intra.42.fr for subject attachments
  - IntraAPI:    OAuth2 client for the 42 API (project metadata)

Dependencies (install once):
    pip install -r requirements.txt

Setup:
    cp .env.example .env
    # Fill in FT_CLIENT_ID, FT_CLIENT_SECRET for API access
    # Fill in SESSION_COOKIE, USER_ID_COOKIE, CF_CLEARANCE_COOKIE for scraping
    # Create your OAuth app at https://profile.intra.42.fr/oauth/applications
    # Extract cookies from your browser after logging into projects.intra.42.fr

Usage:
    python fetch_42_subjects.py
    python fetch_42_subjects.py --keywords "python,dslr"
    python fetch_42_subjects.py --dry-run    # list projects, no downloads
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
    haystack = (project.get("name") or "").lower() + " " + (project.get("url") or "").lower()
    return any(kw in haystack for kw in keywords)


# ── .env loader ───────────────────────────────────────────────────────────────

def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
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


# ── History management ────────────────────────────────────────────────────────

def load_history(history_file: Path) -> dict:
    if history_file.exists():
        with open(history_file, encoding="utf-8") as f:
            return json.load(f)
    return {"projects": {}}


def save_history(history: dict, history_file: Path) -> None:
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download & track 42 project subject PDFs")
    parser.add_argument("--client-id",     default=os.getenv("FT_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.getenv("FT_CLIENT_SECRET"))
    parser.add_argument("--session-cookie",     default=os.getenv("SESSION_COOKIE"))
    parser.add_argument("--user-id-cookie",     default=os.getenv("USER_ID_COOKIE"))
    parser.add_argument("--cf-clearance-cookie", default=os.getenv("CF_CLEARANCE_COOKIE"))
    parser.add_argument("--keywords",      default=os.getenv("FT_KEYWORDS", ""),
                        help="Comma-separated keywords to filter projects. Empty = all.")
    parser.add_argument("--subjects-dir",  default=os.getenv("FT_SUBJECTS_DIR", "subjects"),
                        help="Directory to store PDFs and extracted text (default: subjects/)")
    parser.add_argument("--history-file",  default=os.getenv("FT_HISTORY_FILE", "history.json"),
                        help="Path to the history JSON file (default: history.json)")
    parser.add_argument("--dry-run",       action="store_true",
                        help="List projects but don't download anything")
    args = parser.parse_args()

    if not args.session_cookie:
        print("\nError: set SESSION_COOKIE in your .env file (or pass --session-cookie).")
        print("  Extract it from browser DevTools after logging into projects.intra.42.fr")
        print("  Application → Cookies → _intra_42_session_production\n")
        sys.exit(1)

    subjects_dir = Path(args.subjects_dir)
    history_file = Path(args.history_file)
    subjects_dir.mkdir(parents=True, exist_ok=True)

    # 1. Set up IntraScrape for subject fetching (web scraping approach)
    cookies = {
        '_intra_42_session_production': args.session_cookie,
    }
    if args.user_id_cookie:
        cookies['user.id'] = args.user_id_cookie
    if args.cf_clearance_cookie:
        cookies['cf_clearance'] = args.cf_clearance_cookie

    print("\n→ Initializing IntraScrape …", flush=True)
    scraper = IntraScrape(cookies)

    # 2. Fetch project list from projects.intra.42.fr (with parallel pagination)
    print("\n→ Fetching project list from projects.intra.42.fr …", flush=True)
    projects = scraper.get_all_projects()
    print(f"  ✓ {len(projects)} projects found", flush=True)

    # 3. Keyword filter
    keywords = parse_keywords(args.keywords)
    if keywords:
        before   = len(projects)
        projects = [p for p in projects if matches_keywords(p, keywords)]
        print(f"\n→ Keyword filter {keywords}: {before} → {len(projects)} match", flush=True)
    if not projects:
        print("  ⚠ No projects matched.", flush=True)
        sys.exit(0)

    if args.dry_run:
        print("\n[dry-run] Projects that would be processed:")
        for p in projects:
            print(f"  {p['name']} → {p['url']}")
        return

    # 4. Load existing history
    history = load_history(history_file)

    # 5. For each project: find attachments via scraping, download PDFs, diff, save
    print(f"\n→ Processing {len(projects)} project(s) …\n", flush=True)
    run_ts = datetime.now(timezone.utc).isoformat()
    changed = 0
    skipped = 0
    errors  = 0

    for i, project in enumerate(projects, 1):
        name = project["name"]
        slug = project["url"].strip("/").split("/")[-1]
        print(f"  [{i}/{len(projects)}] {name}", flush=True)

        # Find subject attachments via IntraScrape
        try:
            attachments = scraper.get_project_attachments(project["url"])
        except Exception as e:
            print(f"    [warn] attachment fetch failed: {e}", flush=True)
            errors += 1
            time.sleep(0.5)
            continue

        # Filter for PDF attachments
        pdf_urls = [url for url in attachments if url.lower().endswith(".pdf")]
        if not pdf_urls:
            print(f"    [skip] no PDF attachments found", flush=True)
            skipped += 1
            time.sleep(0.25)
            continue

        pdf_url = pdf_urls[0]
        print(f"    PDF: {pdf_url}", flush=True)

        # Download PDF via IntraScrape session
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

        # Check if content changed via hash
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

        # Extract text
        new_text = extract_text_from_pdf(pdf_bytes)

        # Word-level diff against previous version
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

        # Save PDF and text
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

        # Record in history
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
        save_history(history, history_file)  # save after each project in case of interruption
        time.sleep(0.5)

    print(f"\n✓ Done — {changed} changed, {skipped} no PDF, {errors} errors")
    print(f"  History saved to {history_file}")
    print(f"  PDFs saved under {subjects_dir}/")
    print(f"  Open dashboard.html to explore the history.")


if __name__ == "__main__":
    main()
