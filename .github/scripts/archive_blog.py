#!/usr/bin/env python3
"""Keep proof of when each blog post was published.

For every post in _posts/ (skipped if its front matter has `archive: false`):
  1. OpenTimestamps -- stamp the post file once (Bitcoin-anchored proof that the
     exact file existed at that time); later runs upgrade pending proofs.
  2. Wayback Machine -- save one snapshot of the live post page.
  3. Zenodo -- deposit a PDF plus the Markdown source and publish it, which mints
     a DOI. Only when ZENODO_TOKEN is set, and only once the post has been left
     unedited for ZENODO_DELAY_HOURS (default 24), because a published Zenodo
     record cannot be deleted. FORCE_ZENODO=true skips the wait.

Results go to _data/archive.yml (shown under each post); proofs go to _proofs/.
To verify a proof:  git show <source_commit>:<file> > post.md
                    ots verify -f post.md _proofs/<file>.ots
"""
import datetime as dt
import hashlib
import html
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import markdown
import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
SITE = "https://tianyizheng.com"
POSTS = ROOT / "_posts"
PROOFS = ROOT / "_proofs"
MANIFEST = ROOT / "_data" / "archive.yml"
AUTHOR = {"name": "Zheng, Tianyi", "orcid": "0000-0002-8250-4071"}
LICENSE = "cc-by-4.0"
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)\.(md|markdown)$")
UA = {"User-Agent": "tianyizheng.com post archiver (+https://tianyizheng.com)"}
JST = dt.timezone(dt.timedelta(hours=9))
ERRORS = []


def log(msg):
    print(msg, flush=True)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def read_post(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return {}, text
    return yaml.safe_load(m.group(1)) or {}, m.group(2)


# ---------------------------------------------------------------- OpenTimestamps
def ots_stamp(path, entry):
    PROOFS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / path.name
        shutil.copyfile(path, tmp)
        subprocess.run(["ots", "stamp", str(tmp)], check=True, capture_output=True)
        dest = PROOFS / (path.name + ".ots")
        shutil.move(str(tmp) + ".ots", dest)
    rel = str(path.relative_to(ROOT))
    entry["ots"] = str(dest.relative_to(ROOT))
    entry["ots_status"] = "pending"
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    entry["source_commit"] = git("log", "-1", "--format=%H", "--", rel) or "uncommitted"
    entry["stamped_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ots_upgrade(entry):
    proof = ROOT / entry["ots"]
    subprocess.run(["ots", "upgrade", str(proof)], capture_output=True)
    for bak in proof.parent.glob(proof.name + ".bak*"):
        bak.unlink()
    info = subprocess.run(["ots", "info", str(proof)], capture_output=True, text=True).stdout
    if "BitcoinBlockHeaderAttestation" in info:
        entry["ots_status"] = "complete"


# ---------------------------------------------------------------- Wayback Machine
def is_live(url):
    try:
        return requests.get(url, headers=UA, timeout=30).status_code == 200
    except requests.RequestException:
        return False


def wait_live(url):
    deadline = time.time() + int(os.environ.get("WAIT_LIVE_SECONDS", "0"))
    while True:
        if is_live(url):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(15)


def wayback_latest(url, since):
    q = ("https://web.archive.org/cdx/search/cdx?output=json&fl=timestamp"
         f"&filter=statuscode:200&from={since[:8]}&url={urllib.parse.quote(url, safe='')}")
    rows = requests.get(q, headers=UA, timeout=60).json()
    stamps = [r[0] for r in rows[1:] if r[0] >= since]
    return f"https://web.archive.org/web/{max(stamps)}/{url}" if stamps else None


def wayback_save(url):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).strftime("%Y%m%d%H%M%S")
    try:
        r = requests.get(f"https://web.archive.org/save/{url}", headers=UA, timeout=300)
        m = re.search(r"/web/(\d{14})/", r.url)
        if m:
            return f"https://web.archive.org/web/{m.group(1)}/{url}"
    except requests.RequestException as e:
        log(f"    save request: {e}")
    for _ in range(15):  # the capture index can lag a few minutes behind
        time.sleep(20)
        try:
            snap = wayback_latest(url, since)
            if snap:
                return snap
        except (requests.RequestException, ValueError):
            pass
    return None


# ---------------------------------------------------------------- Zenodo
PDF_TEMPLATE = string.Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>$title</title>
<style>
  @page { size: A4; margin: 22mm 22mm 24mm; }
  body { font-family: Georgia, "Times New Roman", "Noto Serif CJK JP", "Hiragino Mincho ProN", serif;
         font-size: 11pt; line-height: 1.6; color: #222; }
  h1 { font-size: 20pt; line-height: 1.25; margin: 0 0 6pt; }
  .meta { color: #555; font-size: 9.5pt; margin: 0 0 18pt; padding-bottom: 10pt; border-bottom: 0.5pt solid #bbb; }
  a { color: inherit; }
  blockquote { margin-left: 0; padding-left: 12pt; border-left: 2pt solid #ccc; color: #444; }
  .footer { margin-top: 24pt; padding-top: 8pt; border-top: 0.5pt solid #bbb; color: #666; font-size: 8.5pt; }
</style></head><body>
<h1>$title</h1>
<p class="meta">Tianyi Zheng (ORCID <a href="https://orcid.org/0000-0002-8250-4071">0000-0002-8250-4071</a>)<br>
Published $date_long at <a href="$url">$url</a></p>
$body
<p class="footer">&copy; $year Tianyi Zheng. Licensed under CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/).</p>
</body></html>
""")


def build_pdf(title, body_html, url, date, out_pdf):
    page = PDF_TEMPLATE.substitute(
        title=html.escape(title), body=body_html, url=url, year=date[:4],
        date_long=dt.date.fromisoformat(date).strftime("%B %-d, %Y"))
    src = out_pdf.with_suffix(".html")
    src.write_text(page, encoding="utf-8")
    chrome = os.environ.get("CHROME", "google-chrome")
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox",
                    "--no-pdf-header-footer", "--print-to-pdf-no-header",
                    f"--print-to-pdf={out_pdf}", src.as_uri()],
                   check=True, capture_output=True, timeout=120)
    if not out_pdf.exists() or out_pdf.stat().st_size == 0:
        raise RuntimeError("Chrome did not produce a PDF")


def zenodo_publish(path, title, body_md, url, date):
    api = os.environ.get("ZENODO_API", "https://zenodo.org/api").rstrip("/")
    body_html = markdown.markdown(body_md, extensions=["extra", "smarty"])
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / (path.stem + ".pdf")
        build_pdf(title, body_html, url, date, pdf)  # build first so a failure leaves no draft
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {os.environ['ZENODO_TOKEN']}"
        r = s.post(f"{api}/deposit/depositions", json={}, timeout=60)
        r.raise_for_status()
        dep = r.json()
        for f in (pdf, path):
            with open(f, "rb") as fh:
                s.put(f"{dep['links']['bucket']}/{f.name}", data=fh, timeout=300).raise_for_status()
    metadata = {
        "upload_type": "publication",
        "publication_type": "other",
        "title": title,
        "creators": [AUTHOR],
        "description": body_html + f'<p>Originally published at <a href="{url}">{url}</a>.</p>',
        "publication_date": date,
        "access_right": "open",
        "license": LICENSE,
        "related_identifiers": [
            {"identifier": url, "relation": "isIdenticalTo", "resource_type": "publication-other"}],
    }
    s.put(f"{api}/deposit/depositions/{dep['id']}", json={"metadata": metadata},
          timeout=60).raise_for_status()
    r = s.post(f"{api}/deposit/depositions/{dep['id']}/actions/publish", timeout=120)
    r.raise_for_status()
    return r.json()["doi"]


# ---------------------------------------------------------------- main
def main():
    manifest = (yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else None) or {}
    token = os.environ.get("ZENODO_TOKEN", "")
    force = os.environ.get("FORCE_ZENODO", "").lower() == "true"
    delay = float(os.environ.get("ZENODO_DELAY_HOURS", "24")) * 3600
    today = dt.datetime.now(JST).date().isoformat()
    changed = False

    for path in sorted(POSTS.iterdir()):
        m = NAME_RE.match(path.name)
        if not m:
            continue
        date, slug = m.group(1), m.group(2)
        fm, body = read_post(path)
        if fm.get("archive") is False or fm.get("published") is False or date > today:
            continue
        url = f"{SITE}/blog/{slug}/"
        entry = manifest.setdefault(slug, {"file": str(path.relative_to(ROOT))})
        before = dict(entry)
        log(f"- {slug}")
        try:
            if "ots" not in entry:
                ots_stamp(path, entry)
                log("    OpenTimestamps: stamped (pending Bitcoin confirmation)")
            elif entry.get("ots_status") != "complete":
                ots_upgrade(entry)
                log(f"    OpenTimestamps: {entry['ots_status']}")
        except Exception as e:  # noqa: BLE001
            ERRORS.append(f"{slug}: OpenTimestamps: {e}")

        if "wayback" not in entry:
            if wait_live(url):
                snap = wayback_save(url)
                if snap:
                    entry["wayback"] = snap
                    log(f"    Wayback: {snap}")
                else:
                    ERRORS.append(f"{slug}: Wayback snapshot not confirmed")
            else:
                log("    Wayback: page not live yet, will retry next run")

        if "zenodo_doi" not in entry and token:
            age = time.time() - int(git("log", "-1", "--format=%ct", "--", str(path.relative_to(ROOT))) or time.time())
            if force or age >= delay:
                try:
                    entry["zenodo_doi"] = zenodo_publish(path, fm.get("title", slug), body, url, date)
                    log(f"    Zenodo: https://doi.org/{entry['zenodo_doi']}")
                except Exception as e:  # noqa: BLE001
                    ERRORS.append(f"{slug}: Zenodo: {e}")
            else:
                log(f"    Zenodo: waiting until 24 h after the last edit ({age / 3600:.1f} h so far)")

        changed |= entry != before

    if changed:
        MANIFEST.write_text(
            "# Written by .github/scripts/archive_blog.py -- proof of publication time for each blog post.\n"
            + yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
        log("Updated _data/archive.yml")
    for e in ERRORS:
        log(f"ERROR {e}")
    sys.exit(1 if ERRORS else 0)


if __name__ == "__main__":
    main()
