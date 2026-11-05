#!/usr/bin/env python3
"""
sync_jobs.py — TWB Legal Search job cycling automation
=======================================================
Run by a scheduled Claude task every ~26 days.

What it does
------------
1. Reads the Airtable Searches table to find:
   - Records where Search Status = Closed  AND  Live on Site = true
     → Archive each to /jobs/archive/<slug>.html and clear Live on Site
   - Records where Search Status ≠ Closed  AND  Website JD is filled
                                            AND  Live on Site is false/empty
     → Publish each to the JOBS array in index.html + jobs.html
       and set Live on Site = true

2. Anonymises every JD before publishing:
   - Attorney counts: replace exact numbers >1000 with rounded +1000 increments
     (e.g. "2,200 attorneys" → "2,000+ attorneys")
   - Office/location counts: replace exact numbers with vague language
     (e.g. "18 offices" → "several offices")

3. Regenerates the JOBS constant in index.html and jobs.html from the
   full set of currently-live records (old ones that weren't closed, plus
   newly added ones).

4. Creates a static archive page at /jobs/archive/<slug>.html for each
   closed search (Google-indexable, minimal markup).

5. Commits everything and pushes to main → Cloudflare deploys automatically.

Airtable field IDs (do not change)
-----------------------------------
Base:                 appKIPy8507uOMM0z
Table:                tblEsIoiOG8WkHhKB
Search Status:        fldpL4Lwm9J8kJuu3
  Closed choice:      sel9VpEC84xNQ7TgF
Website JD:           fldkmldrPT9Om4IVU   (multilineText — publishable JD copy)
Live on Site:         fldvID3il0mbCbC2N   (checkbox)

Env vars required
-----------------
AIRTABLE_API_KEY   — personal access token with records:read + records:write
GITHUB_TOKEN       — fine-grained token with Contents: write on twblegal/twb-legal-search-site
"""

import os, re, sys, json, textwrap, subprocess, datetime, unicodedata
from pathlib import Path
import urllib.request, urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
AIRTABLE_BASE   = "appKIPy8507uOMM0z"
AIRTABLE_TABLE  = "tblEsIoiOG8WkHhKB"
STATUS_FIELD    = "fldpL4Lwm9J8kJuu3"
CLOSED_CHOICE   = "sel9VpEC84xNQ7TgF"
WEBSITE_JD_FIELD = "fldkmldrPT9Om4IVU"
LIVE_FIELD      = "fldvID3il0mbCbC2N"

REPO_URL_TMPL   = "https://x-access-token:{token}@github.com/twblegal/twb-legal-search-site.git"
REPO_DIR        = Path("/tmp/twb-site-sync")
ARCHIVE_DIR     = "jobs/archive"

SESSION_URL     = "https://claude.ai/code/session_01SxPsmU63PNBgXoEVyo4PUe"

# ── Helpers ───────────────────────────────────────────────────────────────────

def airtable_request(method, path, body=None):
    api_key = os.environ["AIRTABLE_API_KEY"]
    url = f"https://api.airtable.com/v0/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def fetch_all_searches():
    """Return all records from the Searches table."""
    records = []
    offset = None
    while True:
        url = f"{AIRTABLE_BASE}/{AIRTABLE_TABLE}?pageSize=100"
        if offset:
            url += f"&offset={offset}"
        resp = airtable_request("GET", url)
        records.extend(resp.get("records", []))
        offset = resp.get("offset")
        if not offset:
            break
    return records


def set_live_on_site(record_id, value: bool):
    airtable_request(
        "PATCH",
        f"{AIRTABLE_BASE}/{AIRTABLE_TABLE}/{record_id}",
        {"fields": {LIVE_FIELD: value}},
    )


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text).strip("-")
    return text


def anonymise(text: str) -> str:
    """Apply client-anonymisation rules to JD text."""
    if not text:
        return text

    # Attorney counts: "X,XXX attorneys" / "X,XXX+ attorneys" / "X,XXX attorney"
    def anon_attorneys(m):
        raw = m.group(1).replace(",", "").replace("+", "")
        try:
            n = int(raw)
        except ValueError:
            return m.group(0)
        rounded = (n // 1000) * 1000
        return f"{rounded:,}+ {m.group(2)}"

    text = re.sub(
        r"([\d,]+\+?)\s*(attorney(?:s)?)",
        anon_attorneys,
        text,
        flags=re.IGNORECASE,
    )

    # Users across N offices / N locations
    def anon_users_offices(m):
        raw = m.group(1).replace(",", "").replace("+", "")
        try:
            n = int(raw)
        except ValueError:
            return m.group(0)
        rounded = (n // 1000) * 1000
        return f"{rounded:,}+ {m.group(2)} across {m.group(3)}"

    text = re.sub(
        r"([\d,]+\+?)\s*(users?)\s+(?:across|in)\s+([\d,]+\+?\s+(?:offices?|locations?))",
        lambda m: anon_users_offices(m) if re.search(r"\d", m.group(3)) else m.group(0),
        text,
        flags=re.IGNORECASE,
    )
    # Replace remaining "X offices" / "X locations" with vague language
    def anon_offices(m):
        return f"several {m.group(1)}"

    text = re.sub(
        r"\b\d+\+?\s+(offices?|locations?)\b",
        anon_offices,
        text,
        flags=re.IGNORECASE,
    )

    return text


def jd_to_index_description(jd_text: str) -> str:
    """
    Convert a plain-text Website JD into the description format used in
    index.html JOBS array: section headers become <b>Header</b>, rest is prose.
    """
    return anonymise(jd_text.strip())


def jd_to_jobs_fields(record) -> dict | None:
    """
    Parse a Website JD multiline text into the structured fields jobs.html uses.
    Returns None if the JD is too thin to publish.

    Expected section markers in the JD (case-insensitive):
      The Role / What You'll Own / What We're Looking For / Compensation
    """
    fields = record.get("fields", {})
    jd_raw = fields.get(WEBSITE_JD_FIELD, "").strip()
    if not jd_raw:
        return None

    jd = anonymise(jd_raw)
    title = fields.get("Name") or fields.get("Search Name") or ""
    if not title:
        # Try to find any text field that looks like a title
        for v in fields.values():
            if isinstance(v, str) and len(v) < 100 and "\n" not in v:
                title = v
                break
    if not title:
        return None

    # Extract comp from first "Compensation:" line or "Comp:" line
    comp = None
    comp_match = re.search(r"(?:compensation|comp)[:\s]+(.+)", jd, re.IGNORECASE)
    if comp_match:
        comp = comp_match.group(1).split("\n")[0].strip()

    # Extract sections
    def extract_section(text, header_pattern):
        m = re.search(rf"{header_pattern}\s*\n+([\s\S]+?)(?=\n\n[A-Z]|\Z)", text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    summary = extract_section(jd, r"(?:the role|overview)")
    owns_raw = extract_section(jd, r"what you(?:'ll|'ll| will) own")
    reqs_raw = extract_section(jd, r"what we(?:'re|'re| are) looking for")

    # If structured extraction failed, use whole JD as summary
    if not summary:
        summary = jd[:800]

    def bullets(raw):
        lines = [l.strip().lstrip("—-•*").strip() for l in raw.splitlines() if l.strip()]
        return [l for l in lines if l and not re.match(r"what (you|we)", l, re.IGNORECASE)]

    return {
        "title": title.strip(),
        "summary": summary,
        "comp": comp,
        "owns": bullets(owns_raw),
        "requirements": bullets(reqs_raw),
        "editorial": "",   # optional — left blank, editors can fill in Airtable if desired
        "jd_full": jd,     # used for index.html plain-text description
    }


def infer_category(title: str, owns: list) -> str:
    text = (title + " " + " ".join(owns)).lower()
    if any(w in text for w in ["director", "manager", "cio", "chief", "vp ", "head of"]):
        return "leadership"
    if any(w in text for w in ["cloud", "network", "infrastructure", "engineer", "architect", "server", "azure", "vmware"]):
        return "infrastructure"
    return "support"


def infer_tags(title: str, category: str, jd: str) -> list:
    tags = [category.capitalize()]
    jd_low = jd.lower()
    if "remote" in jd_low:
        tags.append("Remote")
    elif "hybrid" in jd_low:
        tags.append("Hybrid")
    elif "on-site" in jd_low or "on site" in jd_low:
        tags.append("On-Site")
    return tags


# ── Archive page builder ──────────────────────────────────────────────────────

ARCHIVE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — TWB Legal Search (Archived)</title>
  <meta name="description" content="{description}" />
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 48px auto; padding: 0 24px; color: #15181d; line-height: 1.7; }}
    h1 {{ font-size: 1.8rem; margin-bottom: 8px; }}
    .meta {{ color: #666; font-size: 0.9rem; margin-bottom: 32px; }}
    .archived-notice {{ background: #fff8f0; border: 1px solid #e8d8c0; color: #8a6830; padding: 14px 18px; border-radius: 6px; margin-bottom: 32px; font-size: 0.9rem; }}
    .jd {{ white-space: pre-line; font-size: 0.95rem; }}
    .back {{ display: inline-block; margin-top: 40px; color: #2b4c3f; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">TWB Legal Search · Archived {archived_date}</div>
  <div class="archived-notice">
    This search has been filled or closed. We keep it posted for reference.
    <a href="https://twbls.com/#jobs">See current open roles →</a>
  </div>
  <div class="jd">{jd_html}</div>
  <a class="back" href="https://twbls.com">← TWB Legal Search</a>
</body>
</html>
"""


def build_archive_page(slug, title, jd_text):
    description = (jd_text[:160] + "…") if len(jd_text) > 160 else jd_text
    jd_html = jd_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ARCHIVE_TEMPLATE.format(
        title=title,
        description=description,
        archived_date=datetime.date.today().strftime("%B %Y"),
        jd_html=jd_html,
    )


# ── index.html JOBS array writer ──────────────────────────────────────────────

def escape_backtick(s):
    return s.replace("`", "\\`").replace("${", "\\${")


def build_index_job_entry(job_id, title, location, job_type, description):
    desc_escaped = escape_backtick(description)
    return (
        f"      {{\n"
        f"        id: {json.dumps(job_id)},\n"
        f"        title: {json.dumps(title)},\n"
        f"        location: {json.dumps(location)},\n"
        f"        type: {json.dumps(job_type)},\n"
        f"        description: `{desc_escaped}`\n"
        f"      }}"
    )


INDEX_JOBS_RE = re.compile(
    r"(const JOBS = \[)([\s\S]*?)(\];[\s\S]*?/\* ═+)",
    re.MULTILINE,
)


def update_index_jobs(html: str, job_entries: list[str]) -> str:
    body = ",\n".join(job_entries)
    replacement = r"\g<1>\n" + body + "\n    " + r"\g<3>"
    return INDEX_JOBS_RE.sub(replacement, html)


# ── jobs.html JOBS array writer ───────────────────────────────────────────────

def build_jobs_html_entry(job_id, title, category, location, comp, tags, status,
                           editorial, summary, owns, requirements):
    owns_js  = json.dumps(owns,  ensure_ascii=False, indent=4)
    reqs_js  = json.dumps(requirements, ensure_ascii=False, indent=4)
    tags_js  = json.dumps(tags,  ensure_ascii=False)
    return (
        f"  {{\n"
        f"    id: {json.dumps(job_id)},\n"
        f"    title: {json.dumps(title)},\n"
        f"    category: {json.dumps(category)},\n"
        f"    location: {json.dumps(location)},\n"
        f"    comp: {json.dumps(comp)},\n"
        f"    tags: {tags_js},\n"
        f"    status: \"open\",\n"
        f"    filledDate: null,\n"
        f"    editorial: {json.dumps(editorial)},\n"
        f"    summary: {json.dumps(summary)},\n"
        f"    owns: {owns_js},\n"
        f"    requirements: {reqs_js}\n"
        f"  }}"
    )


JOBS_HTML_JOBS_RE = re.compile(
    r"(// ── JOB DATA ──[\s\S]*?const JOBS = \[)([\s\S]*?)(\];\n\n// ── RENDER ──)",
    re.MULTILINE,
)


def update_jobs_html_jobs(html: str, job_entries: list[str]) -> str:
    body = ",\n".join(job_entries)
    replacement = r"\g<1>\n" + body + "\n" + r"\g<3>"
    return JOBS_HTML_JOBS_RE.sub(replacement, html)


# ── Git helpers ───────────────────────────────────────────────────────────────

def run(cmd, cwd=None, check=True):
    return subprocess.run(cmd, shell=True, cwd=cwd, check=check,
                          capture_output=True, text=True)


def git_setup(repo_dir: Path):
    run('git config user.email "noreply@anthropic.com"', cwd=repo_dir)
    run('git config user.name "Claude Sonnet 4.6"', cwd=repo_dir)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    airtable_key = os.environ.get("AIRTABLE_API_KEY")
    github_token = os.environ.get("GITHUB_TOKEN")

    if not airtable_key:
        print("ERROR: AIRTABLE_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if not github_token:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    # ── 1. Clone repo ──────────────────────────────────────────────────────────
    if REPO_DIR.exists():
        run("git pull --ff-only", cwd=REPO_DIR)
    else:
        clone_url = REPO_URL_TMPL.format(token=github_token)
        run(f"git clone {clone_url} {REPO_DIR}")

    git_setup(REPO_DIR)

    # ── 2. Fetch Airtable records ──────────────────────────────────────────────
    print("Fetching Airtable Searches…")
    all_records = fetch_all_searches()
    print(f"  {len(all_records)} records total")

    to_archive  = []   # live + closed
    to_publish  = []   # active + JD filled + not live
    still_live  = []   # live + not closed (unchanged)

    for rec in all_records:
        fields  = rec.get("fields", {})
        status  = fields.get(STATUS_FIELD)        # dict with id/name, or None
        jd      = fields.get(WEBSITE_JD_FIELD, "").strip()
        is_live = bool(fields.get(LIVE_FIELD, False))

        status_id = status.get("id") if isinstance(status, dict) else None
        is_closed = (status_id == CLOSED_CHOICE)

        if is_closed and is_live:
            to_archive.append(rec)
        elif not is_closed and jd and not is_live:
            to_publish.append(rec)
        elif not is_closed and is_live:
            still_live.append(rec)

    print(f"  To archive: {len(to_archive)}")
    print(f"  To publish: {len(to_publish)}")
    print(f"  Still live: {len(still_live)}")

    if not to_archive and not to_publish:
        print("Nothing to do — site is already up to date.")
        return

    # ── 3. Build archive pages ─────────────────────────────────────────────────
    archive_path = REPO_DIR / ARCHIVE_DIR
    archive_path.mkdir(parents=True, exist_ok=True)

    archived_slugs = []
    for rec in to_archive:
        fields  = rec.get("fields", {})
        title   = fields.get("Name") or fields.get("Search Name") or "Archived Role"
        jd_text = anonymise(fields.get(WEBSITE_JD_FIELD, ""))
        slug    = slugify(title)
        page    = build_archive_page(slug, title, jd_text)
        (archive_path / f"{slug}.html").write_text(page, encoding="utf-8")
        archived_slugs.append(slug)
        print(f"  Archived: {title} → {ARCHIVE_DIR}/{slug}.html")

    # ── 4. Build JOBS arrays ───────────────────────────────────────────────────
    # All live records after this run = still_live + to_publish
    live_records = still_live + to_publish

    index_entries = []
    jobs_entries  = []

    for rec in live_records:
        fields  = rec.get("fields", {})
        jd_raw  = fields.get(WEBSITE_JD_FIELD, "").strip()
        if not jd_raw:
            # still_live records might predate the Website JD field — skip gracefully
            continue

        parsed = jd_to_jobs_fields(rec)
        if not parsed:
            continue

        job_id   = slugify(parsed["title"])
        title    = parsed["title"]
        location = fields.get("Location") or fields.get("Office") or "Confidential"
        comp     = parsed["comp"]
        summary  = parsed["summary"]
        owns     = parsed["owns"]
        reqs     = parsed["requirements"]
        editorial = parsed["editorial"]
        jd_full  = parsed["jd_full"]
        category = infer_category(title, owns)
        tags     = infer_tags(title, category, jd_full)
        job_type = tags[1] if len(tags) > 1 else "Full-Time"

        # index.html uses plain-text description with <b> for section headers
        description = jd_full  # already anonymised

        index_entries.append(
            build_index_job_entry(job_id, title, location, f"Full-Time · {job_type}", description)
        )
        jobs_entries.append(
            build_jobs_html_entry(
                job_id, title, category, location, comp, tags,
                "open", editorial, summary, owns, reqs,
            )
        )

    # ── 5. Rewrite HTML files ──────────────────────────────────────────────────
    index_html_path = REPO_DIR / "index.html"
    jobs_html_path  = REPO_DIR / "jobs.html"

    index_html = index_html_path.read_text(encoding="utf-8")
    jobs_html  = jobs_html_path.read_text(encoding="utf-8")

    new_index_html = update_index_jobs(index_html, index_entries)
    new_jobs_html  = update_jobs_html_jobs(jobs_html, jobs_entries)

    if new_index_html == index_html and len(to_archive) == 0:
        print("No HTML changes detected.")
    else:
        index_html_path.write_text(new_index_html, encoding="utf-8")
        print("  Updated index.html")

    if new_jobs_html != jobs_html:
        jobs_html_path.write_text(new_jobs_html, encoding="utf-8")
        print("  Updated jobs.html")

    # ── 6. Commit and push ─────────────────────────────────────────────────────
    added_titles   = [rec.get("fields", {}).get("Name") or "?" for rec in to_publish]
    removed_titles = [rec.get("fields", {}).get("Name") or "?" for rec in to_archive]

    lines = []
    if added_titles:
        lines.append("Add: " + ", ".join(added_titles))
    if removed_titles:
        lines.append("Archive: " + ", ".join(removed_titles))

    commit_msg = "chore: job cycle — " + "; ".join(lines) if lines else "chore: job cycle (no changes)"
    commit_msg += f"""

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
Claude-Session: {SESSION_URL}"""

    run("git add -A", cwd=REPO_DIR)
    result = run(
        f'git commit -m "$(cat <<\'EOF\'\n{commit_msg}\nEOF\n)"',
        cwd=REPO_DIR, check=False
    )
    if result.returncode != 0:
        # Fallback: write to temp file
        msg_file = REPO_DIR / ".commit_msg.txt"
        msg_file.write_text(commit_msg)
        run(f'git commit -F .commit_msg.txt', cwd=REPO_DIR)
        msg_file.unlink(missing_ok=True)

    run("git push origin main", cwd=REPO_DIR)
    print("Pushed to GitHub — Cloudflare will deploy shortly.")

    # ── 7. Update Airtable Live on Site flags ──────────────────────────────────
    for rec in to_archive:
        set_live_on_site(rec["id"], False)
        print(f"  Cleared Live on Site: {rec.get('fields',{}).get('Name','?')}")

    for rec in to_publish:
        set_live_on_site(rec["id"], True)
        print(f"  Set Live on Site: {rec.get('fields',{}).get('Name','?')}")

    print("\nDone.")


if __name__ == "__main__":
    main()
