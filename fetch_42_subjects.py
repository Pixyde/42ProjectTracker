#!/usr/bin/env python3
"""
fetch_42_subjects.py
--------------------
Downloads subject PDFs for 42 projects, extracts their text, and keeps a
versioned history. On each run it compares the new text against the last
saved version and records a word-level diff in history.json.

PDF discovery order:
  1. API fields (pdf, attachments, project_sessions > uploads / subject)
  2. CDN probe — tries common filenames at /pdf/pdf/{project_id}/
  3. projects.intra.42.fr scraping (needs --session-cookie or bearer token)

Dependencies (install once):
    pip install pypdf requests

OAuth flow used: Authorization Code
  — requires a real 42 user account, not just API keys.
  — the script prints a URL, you paste it in your browser, log in,
    then paste the redirect URL back into the terminal.

Setup:
    cp .env.example .env
    # Fill in FT_CLIENT_ID, FT_CLIENT_SECRET, FT_REDIRECT_URI
    # Create your OAuth app at https://profile.intra.42.fr/oauth/applications
    # Set the redirect URI to http://localhost (or any URI you control)

Usage:
    python fetch_42_subjects.py
    python fetch_42_subjects.py --keywords "python,dslr"
    python fetch_42_subjects.py --dry-run    # auth + list projects, no downloads
    python fetch_42_subjects.py --scan-previous          # discover older PDF versions
    python fetch_42_subjects.py --scan-previous --scan-range 500
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import HTTPError

UA            = "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
API_BASE      = "https://api.intra.42.fr"
PROJECTS_BASE = "https://projects.intra.42.fr"
CDN_PDF_BASE  = "https://cdn.intra.42.fr/pdf/pdf"
CDN_FILENAMES = ("en.subject.pdf", "fr.subject.pdf", "subject.pdf")
CURSUS_ID     = 21


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


# ── OAuth Authorization Code flow ────────────────────────────────────────────

def oauth_authorize_url(client_id: str, redirect_uri: str, scope: str = "public projects") -> str:
    params = urlencode({
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         scope,
    })
    return f"{API_BASE}/oauth/authorize?{params}"


def exchange_code_for_token(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    payload = urlencode({
        "grant_type":    "authorization_code",
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
        "redirect_uri":  redirect_uri,
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
    with urlopen(req) as resp:
        return json.loads(resp.read())


def get_user_token(client_id: str, client_secret: str, redirect_uri: str) -> str:
    """
    Prints the authorization URL, waits for the user to paste the redirect URL,
    extracts the code, and exchanges it for an access token.
    """
    url = oauth_authorize_url(client_id, redirect_uri)

    print("\n── OAuth Authorization ──────────────────────────────────────────────")
    print("  Open this URL in your browser and log in with your 42 account:")
    print()
    print(f"  {url}")
    print()
    print("  After logging in you'll be redirected to your redirect URI.")
    print("  Paste the full redirect URL below (even if it shows an error).")
    print("─────────────────────────────────────────────────────────────────────\n")

    redirect_url = input("  Paste redirect URL: ").strip()

    # Extract code from the redirect URL
    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)
    if "code" not in params:
        print(f"\n  ✗ No 'code' found in URL: {redirect_url}")
        print("    Make sure you pasted the full redirect URL including ?code=...")
        sys.exit(1)

    code = params["code"][0]
    print(f"\n  ✓ Authorization code received ({code[:8]}…)", flush=True)

    print("  Exchanging code for access token…", flush=True)
    try:
        token_data = exchange_code_for_token(code, client_id, client_secret, redirect_uri)
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"\n  ✗ Token exchange failed — HTTP {e.code}: {body}")
        sys.exit(1)

    token = token_data.get("access_token")
    if not token:
        print(f"\n  ✗ No access_token in response: {token_data}")
        sys.exit(1)

    print("  ✓ Access token obtained", flush=True)
    return token


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api_get(path: str, token: str, params: dict = None, retries: int = 5):
    qs = ("?" + urlencode(params)) if params else ""
    url = f"{API_BASE}{path}{qs}"
    req = Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
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
    while True:
        data = api_get(path, token, {"page[number]": page, "page[size]": 100})
        if not data:
            break
        results.extend(data)
        print(f"  page {page}: +{len(data)} (total: {len(results)})", flush=True)
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.3)
    return results


def download_bytes(url: str, token: str) -> bytes:
    """Download a URL. Tries with Bearer auth first, then without (for public CDN URLs)."""
    headers = {"User-Agent": UA}
    for auth in (f"Bearer {token}", None):
        hdrs = {**headers, "Authorization": auth} if auth else headers
        req = Request(url, headers=hdrs)
        try:
            with urlopen(req) as resp:
                return resp.read()
        except HTTPError as e:
            # On auth failure, retry without auth (CDN URLs are public)
            if auth and e.code in (401, 403):
                continue
            raise
    raise RuntimeError(f"download failed (auth and no-auth both failed): {url}")


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


# ── Subject URL extraction ────────────────────────────────────────────────────

def find_subject_url(project_detail: dict) -> str | None:
    """
    Look for a PDF subject URL in the project detail JSON.
    Checks: pdf field, attachments, project_sessions > uploads,
    and project_sessions > subject.
    """
    # Direct pdf field
    if project_detail.get("pdf"):
        return project_detail["pdf"]

    # Attachments array
    for att in (project_detail.get("attachments") or []):
        url = att.get("url") or att.get("file_url") or ""
        if url.lower().endswith(".pdf"):
            return url

    # project_sessions may contain uploads or a subject url
    for session in (project_detail.get("project_sessions") or []):
        # Check uploads array (where 42 API stores subject PDFs)
        for upload in (session.get("uploads") or []):
            u = upload.get("url") or upload.get("file_url") or upload.get("link") or ""
            if u:
                return u

        # Fallback: check the subject field
        url = session.get("subject", {})
        if isinstance(url, dict):
            u = url.get("url") or url.get("file_url") or ""
            if u:
                return u
        elif isinstance(url, str) and url:
            return url

    return None


def scrape_subject_pdf_url(slug: str, token: str, session_cookie: str = None) -> str | None:
    """
    Discover the real CDN PDF URL by scraping the project page on
    projects.intra.42.fr.  Tries bearer-token auth first, then falls
    back to a session cookie if one was supplied.

    Used as a last resort when the API fields are empty and CDN probing
    with common filenames also fails.
    """
    for page_url in (
        f"{PROJECTS_BASE}/projects/{slug}",
        f"{PROJECTS_BASE}/{slug}",
    ):
        # Try bearer token first, then session cookie
        auth_variants = []
        auth_variants.append({
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
        })
        if session_cookie:
            auth_variants.append({
                "User-Agent": UA,
                "Cookie": f"_intra_42_session_production={session_cookie}",
            })

        for headers in auth_variants:
            req = Request(page_url, headers=headers)
            try:
                with urlopen(req) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
            except Exception:
                continue

            # Look for CDN PDF URLs in the HTML
            m = re.search(
                r'https://cdn\.intra\.42\.fr/pdf/pdf/\d+/[\w.]+\.pdf',
                html,
            )
            if m:
                return m.group(0)

    return None


# ── CDN probing ───────────────────────────────────────────────────────────────

def _cdn_head_ok(url: str, token: str) -> bool:
    """Return True if a HEAD (or GET) request to *url* succeeds (HTTP 200/206)."""
    for auth in (f"Bearer {token}", None):
        headers = {"User-Agent": UA}
        if auth:
            headers["Authorization"] = auth
        for method in ("HEAD", "GET"):
            hdrs = dict(headers)
            if method == "GET":
                hdrs["Range"] = "bytes=0-0"   # avoid downloading the full file
            req = Request(url, method=method, headers=hdrs)
            try:
                with urlopen(req) as resp:
                    return True               # 200 or 206
            except HTTPError as e:
                if method == "HEAD" and e.code == 405:
                    continue                  # HEAD not allowed, try GET
                if auth and e.code in (401, 403):
                    break                     # try without auth
                return False                  # 404 or other → file absent
            except Exception:
                break
    return False


def probe_cdn_pdf(project_id: int, token: str) -> str | None:
    """
    Probe the 42 CDN for a subject PDF at ``/pdf/pdf/{project_id}/``.

    The directory may exist (HTTP 403 on the bare path) while the
    commonly-assumed filename ``en.subject.pdf`` yields 404.  This
    function tries every name in CDN_FILENAMES with and without auth.
    """
    for filename in CDN_FILENAMES:
        url = f"{CDN_PDF_BASE}/{project_id}/{filename}"
        if _cdn_head_ok(url, token):
            return url
    return None


def extract_cdn_id(url: str) -> int | None:
    """Extract the numeric upload / project ID from a cdn.intra.42.fr URL."""
    m = re.search(r'cdn\.intra\.42\.fr/pdf/pdf/(\d+)/', url)
    return int(m.group(1)) if m else None


def discover_previous_versions(
    current_cdn_id: int,
    known_filename: str,
    project_slug: str,
    token: str,
    scan_range: int = 200,
    max_versions: int = 10,
) -> list[dict]:
    """
    Scan the CDN **backward** from *current_cdn_id* looking for earlier
    uploads whose first-page text matches *project_slug*.

    Returns ``[{"cdn_id": int, "url": str, "pdf_bytes": bytes,
                "text": str}, ...]`` ordered **oldest-first**.
    """
    slug_normalised = project_slug.lower().replace("-", " ")
    slug_words = [w for w in slug_normalised.split() if len(w) > 1]

    versions: list[dict] = []
    consecutive_misses = 0

    for probe_id in range(current_cdn_id - 1,
                          max(0, current_cdn_id - scan_range - 1), -1):
        # Try the known filename first, then the other common names
        filenames = [known_filename] + [f for f in CDN_FILENAMES
                                         if f != known_filename]
        pdf_url = None
        for fn in filenames:
            candidate = f"{CDN_PDF_BASE}/{probe_id}/{fn}"
            if _cdn_head_ok(candidate, token):
                pdf_url = candidate
                break

        if not pdf_url:
            consecutive_misses += 1
            if consecutive_misses >= 30:
                break
            continue

        consecutive_misses = 0

        # Download the PDF and check if it belongs to the same project
        try:
            pdf_bytes = download_bytes(pdf_url, token)
        except Exception:
            continue

        text = extract_text_from_pdf(pdf_bytes)
        first_page = text[:1000].lower()

        # Match: full normalised slug in first page, or ≥⅔ of the words
        if slug_normalised in first_page:
            match = True
        else:
            hits = sum(1 for w in slug_words if w in first_page)
            match = hits >= max(2, len(slug_words) * 2 // 3)

        if match:
            versions.append({
                "cdn_id":    probe_id,
                "url":       pdf_url,
                "pdf_bytes": pdf_bytes,
                "text":      text,
            })
            if len(versions) >= max_versions:
                break

        time.sleep(0.3)

    versions.reverse()   # oldest first
    return versions


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
    parser.add_argument("--redirect-uri",  default=os.getenv("FT_REDIRECT_URI", "http://localhost"))
    parser.add_argument("--keywords",      default=os.getenv("FT_KEYWORDS", ""),
                        help="Comma-separated keywords to filter projects. Empty = all.")
    parser.add_argument("--subjects-dir",  default=os.getenv("FT_SUBJECTS_DIR", "subjects"),
                        help="Directory to store PDFs and extracted text (default: subjects/)")
    parser.add_argument("--history-file",  default=os.getenv("FT_HISTORY_FILE", "history.json"),
                        help="Path to the history JSON file (default: history.json)")
    parser.add_argument("--cache-file",    default=os.getenv("FT_CACHE_FILE", ".cache_summaries.json"),
                        help="Shared project list cache (default: .cache_summaries.json)")
    parser.add_argument("--cache-ttl",     type=float, default=float(os.getenv("FT_CACHE_TTL_HOURS", "24")),
                        help="Cache TTL in hours (default: 24). Set 0 to always refresh.")
    parser.add_argument("--no-cache",      action="store_true",
                        help="Ignore cache and force a fresh fetch.")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Authenticate and list projects but don't download anything")
    parser.add_argument("--session-cookie", default=os.getenv("FT_SESSION_COOKIE"),
                        help="Value of _intra_42_session_production cookie for "
                             "projects.intra.42.fr scraping (optional). "
                             "Needed when the API does not expose PDF URLs.")
    parser.add_argument("--scan-previous",  action="store_true",
                        help="Scan the CDN backward from each project's CDN ID "
                             "to discover older versions of its subject PDF.")
    parser.add_argument("--scan-range",     type=int,
                        default=int(os.getenv("FT_SCAN_RANGE", "200")),
                        help="How many CDN IDs to probe backward (default: 200).")
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print("\nError: set FT_CLIENT_ID and FT_CLIENT_SECRET in your .env file.\n")
        sys.exit(1)

    subjects_dir = Path(args.subjects_dir)
    history_file = Path(args.history_file)
    subjects_dir.mkdir(parents=True, exist_ok=True)

    # 1. OAuth — user token
    token = get_user_token(args.client_id, args.client_secret, args.redirect_uri)

    # 2. Project list — try cache first
    cache_file = Path(args.cache_file)
    summaries = None
    if not args.no_cache and args.cache_ttl > 0:
        print(f"\n→ Checking project list cache ({cache_file}) …", flush=True)
        summaries = load_cache(cache_file, args.cache_ttl)
    if summaries is None:
        print(f"\n→ Fetching projects from cursus {CURSUS_ID} …", flush=True)
        summaries = get_all_pages(f"/v2/cursus/{CURSUS_ID}/projects", token)
        print(f"  ✓ {len(summaries)} projects", flush=True)
        save_cache(summaries, cache_file)
    else:
        print(f"  ✓ Using cached project list ({len(summaries)} projects)", flush=True)

    # 3. Keyword filter
    keywords = parse_keywords(args.keywords)
    if keywords:
        before    = len(summaries)
        summaries = [p for p in summaries if matches_keywords(p, keywords)]
        print(f"\n→ Keyword filter {keywords}: {before} → {len(summaries)} match", flush=True)
    if not summaries:
        print("  ⚠ No projects matched.", flush=True)
        sys.exit(0)

    if args.dry_run:
        print("\n[dry-run] Projects that would be processed:")
        for p in summaries:
            print(f"  {p['slug']}")
        return

    # 4. Load existing history
    history = load_history(history_file)

    # 5. For each project: fetch detail, find PDF, download, diff, save
    print(f"\n→ Processing {len(summaries)} project(s) …\n", flush=True)
    run_ts = datetime.now(timezone.utc).isoformat()
    changed = 0
    skipped = 0
    errors  = 0

    for i, summary in enumerate(summaries, 1):
        slug = summary.get("slug") or str(summary["id"])
        name = summary.get("name") or slug
        print(f"  [{i}/{len(summaries)}] {slug}", flush=True)

        # Fetch full project detail
        try:
            detail = api_get(f"/v2/projects/{summary['id']}", token)
        except Exception as e:
            print(f"    [warn] detail fetch failed: {e}", flush=True)
            errors += 1
            time.sleep(0.25)
            continue

        # Find subject PDF URL — try API fields → CDN probe → web scrape
        pdf_url = find_subject_url(detail)
        if not pdf_url:
            pdf_url = probe_cdn_pdf(summary["id"], token)
        if not pdf_url:
            pdf_url = scrape_subject_pdf_url(slug, token, args.session_cookie)

        if not pdf_url:
            print(f"    [skip] no subject PDF found", flush=True)
            skipped += 1
            time.sleep(0.25)
            continue

        print(f"    PDF: {pdf_url}", flush=True)

        # Download PDF
        try:
            pdf_bytes = download_bytes(pdf_url, token)
        except HTTPError as e:
            if e.code == 404:
                print(f"    [skip] PDF not found (404)", flush=True)
                skipped += 1
            else:
                print(f"    [warn] download failed: HTTP {e.code}", flush=True)
                errors += 1
            time.sleep(0.25)
            continue
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

        # ── Scan for previous versions on the CDN ─────────────────────────
        if args.scan_previous and pdf_url:
            cdn_id = extract_cdn_id(pdf_url)
            known_fn = pdf_url.rsplit("/", 1)[-1]
            if cdn_id:
                print(f"    ↻ scanning CDN backward from {cdn_id} "
                      f"(range {args.scan_range}) …", flush=True)
                known_hashes = {v["hash"] for v in proj_history["versions"]}
                prev = discover_previous_versions(
                    cdn_id, known_fn, slug, token, args.scan_range,
                )
                new_prev = 0
                for pv in prev:
                    pv_hash = hashlib.sha256(pv["pdf_bytes"]).hexdigest()
                    if pv_hash in known_hashes:
                        continue
                    known_hashes.add(pv_hash)

                    pv_prefix = f"cdn-{pv['cdn_id']}"
                    pv_pdf  = f"{pv_prefix}.pdf"
                    pv_txt  = f"{pv_prefix}.txt"
                    pv_diff = f"{pv_prefix}.diff.json"

                    (proj_dir / pv_pdf).write_bytes(pv["pdf_bytes"])
                    (proj_dir / pv_txt).write_text(pv["text"], encoding="utf-8")
                    pv_chunks = [{"type": "equal", "text": pv["text"]}]
                    (proj_dir / pv_diff).write_text(
                        json.dumps(pv_chunks, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

                    proj_history["versions"].insert(0, {
                        "discovered_at": run_ts,
                        "hash":          pv_hash,
                        "pdf_file":      pv_pdf,
                        "text_file":     pv_txt,
                        "diff_file":     pv_diff,
                        "words_added":   len(pv["text"].split()),
                        "words_removed": 0,
                        "cdn_id":        pv["cdn_id"],
                        "source":        "cdn_scan",
                    })
                    new_prev += 1

                if new_prev:
                    print(f"    ✓ {new_prev} previous version(s) discovered",
                          flush=True)
                    save_history(history, history_file)
                else:
                    print(f"    (no previous versions found)", flush=True)

        time.sleep(0.5)

    print(f"\n✓ Done — {changed} changed, {skipped} no PDF, {errors} errors")
    print(f"  History saved to {history_file}")
    print(f"  PDFs saved under {subjects_dir}/")
    print(f"  Open dashboard.html to explore the history.")

    if skipped and not args.session_cookie:
        print()
        print("  ╭─ Tip ──────────────────────────────────────────────────────────╮")
        print("  │ Some projects don't expose PDF URLs through the API.           │")
        print("  │ To fix this, supply your 42 intra session cookie so the tool   │")
        print("  │ can scrape the real CDN URLs from projects.intra.42.fr:        │")
        print("  │                                                                │")
        print("  │   1. Log into projects.intra.42.fr in your browser             │")
        print("  │   2. Open DevTools → Application → Cookies                     │")
        print("  │   3. Copy the value of _intra_42_session_production            │")
        print("  │   4. Set FT_SESSION_COOKIE in .env or pass --session-cookie    │")
        print("  ╰────────────────────────────────────────────────────────────────╯")


if __name__ == "__main__":
    main()
