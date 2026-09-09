#!/usr/bin/env python3
"""
Schoology Sync
==============

Downloads Schoology course file attachments while preserving the Schoology
course/folder hierarchy as closely as possible.

Authentication:
- Uses a persistent Playwright Chromium profile per student/profile.
- If Schoology asks for QR login, the browser remains open so you can scan it.
- The script never reads, stores, decodes, or automates the QR credential itself.

Examples:
    python schoology_sync.py hanna
    python schoology_sync.py matthew --output D:\\Schoology
    python schoology_sync.py hanna --list-courses
    python schoology_sync.py hanna --dry-run
    python schoology_sync.py hanna --refresh

Requires:
    pip install playwright beautifulsoup4 requests
    playwright install chromium
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


APP_NAME = "schoology-sync"
DEFAULT_BASE_URL = "https://app.schoology.com"
QR_PATH = "/login/qr"

# Be gentle with Schoology. An older Schoology downloader userscript also
# deliberately rate-limited requests. 0.65 sec is ~1.5 material pages/sec.
REQUEST_DELAY_SECONDS = 0.65

FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".rtf", ".csv",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".mp3", ".m4a", ".wav", ".aac",
    ".mp4", ".m4v", ".mov", ".avi", ".wmv",
    ".zip", ".7z", ".rar",
    ".epub",
}


@dataclass
class Course:
    course_id: str
    name: str
    url: str


@dataclass
class MaterialPage:
    url: str
    title: str
    folders: list[str]


@dataclass
class Attachment:
    source_page: str
    href: str
    display_name: str
    folders: list[str]
    material_title: str


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def sanitize_component(name: str, fallback: str = "Untitled") -> str:
    """Make a safe Windows/macOS/Linux path component."""
    name = (name or "").replace("\u00a0", " ").strip()
    name = re.sub(r"\s+", " ", name)
    # Windows forbidden characters + control characters.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    if not name:
        name = fallback
    # Keep paths manageable on Windows.
    if len(name) > 120:
        stem, dot, suffix = name.rpartition(".")
        if dot and len(suffix) <= 10:
            name = stem[:100].rstrip() + "." + suffix
        else:
            name = name[:110].rstrip()
    return name


def canonical_url(url: str) -> str:
    """Remove query/fragment so expiring signed query strings do not create duplicates."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def looks_like_file_name(name: str) -> bool:
    p = Path(name.strip())
    return p.suffix.lower() in FILE_EXTENSIONS


def content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None

    # RFC 5987 filename*=UTF-8''...
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", value, flags=re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))

    m = re.search(r'filename\s*=\s*"([^"]+)"', value, flags=re.I)
    if m:
        return m.group(1).strip()

    m = re.search(r"filename\s*=\s*([^;]+)", value, flags=re.I)
    if m:
        return m.group(1).strip().strip('"')

    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def ensure_unique_path(path: Path, expected_hash: str | None = None) -> Path:
    if not path.exists():
        return path

    if expected_hash:
        try:
            if sha256_file(path) == expected_hash:
                return path
        except OSError:
            pass

    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
        i += 1


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"version": 1, "files": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                self.data.setdefault("files", {})
            except Exception as exc:
                eprint(f"WARNING: could not read state file {path}: {exc}")

    def get(self, key: str) -> dict | None:
        return self.data["files"].get(key)

    def set(self, key: str, value: dict) -> None:
        self.data["files"][key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


def build_requests_session(context: BrowserContext, page: Page) -> requests.Session:
    session = requests.Session()
    try:
        ua = page.evaluate("navigator.userAgent")
        if ua:
            session.headers["User-Agent"] = ua
    except Exception:
        pass

    # Copy Playwright cookies into requests.
    for c in context.cookies():
        kwargs = {}
        if c.get("domain"):
            kwargs["domain"] = c["domain"]
        if c.get("path"):
            kwargs["path"] = c["path"]
        session.cookies.set(c["name"], c["value"], **kwargs)

    session.headers.update({
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": page.url,
    })
    return session


def logged_out(page: Page) -> bool:
    u = page.url.lower()
    if "/login" in u or "/register" in u:
        return True

    try:
        if page.locator('input[type="password"]').count():
            return True
    except Exception:
        pass

    return False


def browser_session_authenticated(context: BrowserContext, base_url: str) -> tuple[bool, str | None]:
    """
    Check authentication without disturbing the QR-camera page.

    BrowserContext.request shares the browser context's cookie jar, so this also
    detects logins that complete in a popup/new tab instead of the original page.
    """
    try:
        response = context.request.get(
            base_url.rstrip("/") + "/home",
            timeout=10_000,
            max_redirects=10,
        )
        final_url = response.url
        lower = final_url.lower()
        logged_in = (
            response.status < 400
            and "/login" not in lower
            and "/register" not in lower
        )
        return logged_in, final_url
    except Exception:
        return False, None


def find_authenticated_page(context: BrowserContext) -> Page | None:
    """Return any already-open page that looks authenticated."""
    for candidate in reversed(context.pages):
        try:
            if not candidate.is_closed() and not logged_out(candidate):
                u = candidate.url.lower()
                if u.startswith("http") and "schoology" in u:
                    return candidate
        except Exception:
            continue
    return None


def ensure_login(page: Page, base_url: str) -> str:
    """
    Ensure the persistent browser context is authenticated and return its origin.
    """
    context = page.context
    print("\nChecking Schoology login...")
    try:
        page.goto(base_url.rstrip("/") + "/home", wait_until="domcontentloaded", timeout=45_000)
    except PlaywrightTimeoutError:
        pass

    authenticated, final_url = browser_session_authenticated(context, base_url)

    if not logged_out(page) or authenticated:
        print("✓ Existing Schoology session is still valid.")
    else:
        qr_url = base_url.rstrip("/") + QR_PATH
        print("Schoology requires login.")
        print("Opening QR login in the browser...")
        page.goto(qr_url, wait_until="domcontentloaded", timeout=45_000)

        print()
        print("=" * 70)
        print("SCAN THE SCHOOL QR CODE IN THE OPEN BROWSER WINDOW.")
        print("If Chromium asks for camera permission, allow it.")
        print("The terminal will continue automatically after login succeeds.")
        print("=" * 70)

        detected = False
        final_url = None

        # Check BOTH open browser pages and the shared browser cookie session.
        # This handles Schoology/SSO flows that finish in a popup or new tab.
        for _ in range(180):
            candidate = find_authenticated_page(context)
            if candidate is not None:
                detected = True
                final_url = candidate.url
                break

            authenticated, final_url = browser_session_authenticated(context, base_url)
            if authenticated:
                detected = True
                break

            time.sleep(1)

        if not detected:
            raise RuntimeError(
                "I could not detect the completed Schoology login after 3 minutes. "
                "If the browser is visibly logged in, rerun this command; the persistent "
                "profile should keep the authenticated session."
            )

        print("✓ QR login detected.")

    # Normalize the automation page onto the authenticated Schoology home page.
    # This is important when authentication completed in a popup/new tab.
    target_origin = base_url.rstrip("/")
    if final_url:
        parsed = urlsplit(final_url)
        if parsed.scheme and parsed.netloc:
            target_origin = f"{parsed.scheme}://{parsed.netloc}"

    try:
        page.goto(target_origin + "/home", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(700)
    except PlaywrightTimeoutError:
        pass

    if logged_out(page):
        # Custom Schoology domains sometimes authenticate against app.schoology.com.
        # Try the original base URL once more before failing.
        page.goto(base_url.rstrip("/") + "/home", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(500)

    if logged_out(page):
        raise RuntimeError(
            "Schoology authentication cookies were detected, but /home still returned "
            "to a login page. Please rerun the command once; the persistent session "
            "has already been saved."
        )

    parsed = urlsplit(page.url)
    return f"{parsed.scheme}://{parsed.netloc}"


def course_candidates_from_page(page: Page, origin: str) -> dict[str, str]:
    js = r"""
    () => {
      const result = [];
      for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href || '';
        const m = href.match(/\/course\/(\d+)(?:\/|$|\?)/);
        if (!m) continue;

        let text = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
        result.push({id: m[1], href, text});
      }
      return result;
    }
    """
    rows = page.evaluate(js)
    found: dict[str, str] = {}

    bad_names = {
        "", "materials", "grades", "mastery", "updates", "members",
        "analytics", "badges", "attendance", "course options"
    }

    for row in rows:
        cid = row["id"]
        text = (row.get("text") or "").strip()
        if text.lower() in bad_names:
            text = ""
        # Prefer a meaningful/longer text label.
        if cid not in found or (text and len(text) > len(found[cid])):
            found[cid] = text

    return found


def discover_courses(page: Page, origin: str) -> list[Course]:
    print("\nDiscovering courses...")
    found: dict[str, str] = {}

    def merge(rows: dict[str, str]):
        for cid, name in rows.items():
            if cid not in found or (name and len(name) > len(found[cid])):
                found[cid] = name

    # Home page first.
    try:
        page.goto(origin + "/home", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(1500)
        merge(course_candidates_from_page(page, origin))
    except Exception as exc:
        eprint(f"WARNING: home-page course discovery issue: {exc}")

    # Open the Courses menu: many Schoology deployments render current courses here.
    selectors = [
        'a:has-text("Courses")',
        'button:has-text("Courses")',
        '[aria-label*="Courses" i]',
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count():
                loc.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                merge(course_candidates_from_page(page, origin))
                break
        except Exception:
            continue

    # Some deployments have a direct courses page/dashboard.
    for path in ("/courses", "/home/course-dashboard"):
        try:
            response = page.goto(origin + path, wait_until="domcontentloaded", timeout=20_000)
            if response is not None and response.status < 400 and not logged_out(page):
                page.wait_for_timeout(1000)
                merge(course_candidates_from_page(page, origin))
        except Exception:
            pass

    courses: list[Course] = []
    for cid, name in found.items():
        courses.append(
            Course(
                course_id=cid,
                name=name or f"Course {cid}",
                url=f"{origin}/course/{cid}/materials",
            )
        )

    # Get better names from each Materials page if needed.
    refined: list[Course] = []
    for c in courses:
        try:
            page.goto(c.url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(700)
            better = get_course_name(page, c.name)
            refined.append(Course(c.course_id, better, c.url))
        except Exception:
            refined.append(c)

    # Stable alphabetical output.
    refined.sort(key=lambda x: x.name.lower())
    print(f"✓ Found {len(refined)} course(s).")
    return refined


def get_course_name(page: Page, fallback: str) -> str:
    selectors = [
        "h1",
        ".course-title",
        ".page-title",
        ".profile-header .name",
        ".course-profile .name",
        "[data-testid*='course-title']",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count():
                text = loc.first.inner_text(timeout=1500).strip()
                text = re.sub(r"\s+", " ", text)
                if text and len(text) < 180 and text.lower() not in {"materials", "schoology"}:
                    return text
        except Exception:
            pass

    title = page.title().strip()
    for sep in (" | Schoology", " - Schoology"):
        if title.endswith(sep):
            title = title[:-len(sep)].strip()
    if title and title.lower() != "schoology":
        return title
    return fallback


def folder_has_loaded_subtree(expander) -> bool:
    try:
        return bool(expander.evaluate(
            """el => {
                const td = el.closest('td');
                if (!td) return false;
                return Array.from(td.children).some(x => x.classList && x.classList.contains('folder-subtree'));
            }"""
        ))
    except Exception:
        return False


def expand_all_folders(page: Page, max_rounds: int = 80) -> int:
    """
    Schoology's classic Materials UI loads child folders via AJAX when a
    .folder-expander is clicked. Expand recursively until no unopened folder
    expander remains.
    """
    total_clicked = 0

    for _round in range(max_rounds):
        try:
            expanders = page.locator(".folder-contents-cell .folder-expander")
            count = expanders.count()
        except Exception:
            return total_clicked

        clicked_this_round = 0

        # Use index loop. DOM can change after clicks, so re-check count carefully.
        for i in range(count):
            try:
                expander = page.locator(".folder-contents-cell .folder-expander").nth(i)
                if not expander.is_visible():
                    continue
                if folder_has_loaded_subtree(expander):
                    continue

                expander.click(timeout=4000)
                clicked_this_round += 1
                total_clicked += 1
                page.wait_for_timeout(700)
            except Exception:
                continue

        if clicked_this_round == 0:
            break

    return total_clicked


EXTRACT_JS = r"""
() => {
  const root = document.querySelector('#folder-contents-table') ||
               document.querySelector('[class*="materials"]') ||
               document.body;

  function clean(s) {
    return (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function ownFolderTitle(td, subtree) {
    if (!td) return '';
    const titles = Array.from(td.querySelectorAll('.folder-title'));
    // The title belonging to this folder is outside the subtree containing us.
    const own = titles.find(t => !subtree.contains(t));
    return own ? clean(own.innerText || own.textContent) : '';
  }

  function folderPath(el) {
    const path = [];
    let cur = el;

    for (let guard = 0; guard < 30; guard++) {
      const subtree = cur.closest ? cur.closest('.folder-subtree') : null;
      if (!subtree) break;

      const td = subtree.parentElement;
      if (!td) break;

      const title = ownFolderTitle(td, subtree);
      if (title && !path.includes(title)) path.unshift(title);

      cur = td.parentElement || td;
    }
    return path;
  }

  function bestTitle(el) {
    const selectors = [
      '.item-title',
      '.materials-item-title',
      '.folder-title',
      '.title',
      'h3', 'h4'
    ];
    for (const s of selectors) {
      const t = el.querySelector && el.querySelector(s);
      if (t) {
        const v = clean(t.innerText || t.textContent);
        if (v) return v;
      }
    }
    return '';
  }

  const standaloneDocs = [];
  for (const row of root.querySelectorAll('.type-document')) {
    const a = row.querySelector('.attachments-file-name a[href]') ||
              row.querySelector('.attachments-file a[href]');
    if (!a) continue;
    standaloneDocs.push({
      url: a.href,
      title: clean(a.innerText || a.textContent) || bestTitle(row),
      folders: folderPath(row)
    });
  }

  // Collect likely material pages so we can inspect assignments/pages/etc. for
  // attached files. We intentionally only collect links located inside a
  // material-ish row, not global navigation.
  const materialPages = [];
  const seen = new Set();

  const rowCandidates = root.querySelectorAll(
    'tr, .material-row, .materials-item, .type-assignment, .type-page, ' +
    '.type-discussion, .type-assessment, .type-quiz, .type-test, .type-document'
  );

  for (const row of rowCandidates) {
    const anchors = Array.from(row.querySelectorAll('a[href]'));
    for (const a of anchors) {
      const href = a.href || '';
      if (!href || href.startsWith('javascript:')) continue;

      let u;
      try { u = new URL(href, location.href); } catch { continue; }

      if (u.origin !== location.origin) continue;

      const p = u.pathname;
      const likely =
        /\/course\/\d+\/materials\//.test(p) ||
        /\/assignment\//.test(p) ||
        /\/page\//.test(p) ||
        /\/discussion\//.test(p) ||
        /\/assessment\//.test(p) ||
        /\/test\//.test(p) ||
        /\/quiz\//.test(p);

      if (!likely) continue;

      const key = u.origin + u.pathname + u.search;
      if (seen.has(key)) continue;
      seen.add(key);

      const txt = clean(a.innerText || a.textContent) || bestTitle(row);
      materialPages.push({
        url: key,
        title: txt || 'Material',
        folders: folderPath(row)
      });
    }
  }

  // Fallback for newer Schoology markup: collect obvious file-looking links
  // inside the materials region.
  const directFileLinks = [];
  for (const a of root.querySelectorAll('a[href]')) {
    const href = a.href || '';
    const txt = clean(a.innerText || a.textContent);
    let p = '';
    try { p = new URL(href, location.href).pathname.toLowerCase(); } catch {}
    if (/\.(pdf|docx?|xlsx?|pptx?|txt|rtf|csv|jpg|jpeg|png|gif|webp|mp3|m4a|wav|mp4|mov|zip|7z|rar|epub)$/.test(p)) {
      directFileLinks.push({
        url: href,
        title: txt || p.split('/').pop() || 'file',
        folders: folderPath(a)
      });
    }
  }

  return {standaloneDocs, materialPages, directFileLinks};
}
"""


def extract_material_inventory(page: Page):
    return page.evaluate(EXTRACT_JS)


def parse_attachments_from_html(
    html: str,
    source_page: str,
    folders: list[str],
    material_title: str,
) -> list[Attachment]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Attachment] = []
    seen: set[str] = set()

    selectors = [
        ".attachments-file a[href]",
        ".attachments-file-name a[href]",
        "[class*='attachment'] a[href]",
    ]

    for selector in selectors:
        for a in soup.select(selector):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "#")):
                continue

            absolute = urljoin(source_page, href)
            key = canonical_url(absolute)
            if key in seen:
                continue

            # Avoid external web links unless the href itself clearly looks like a file.
            path = urlsplit(absolute).path.lower()
            link_text = " ".join(a.stripped_strings).strip()

            # Schoology's attachment areas can include link attachments as well.
            # File attachment URLs often do not carry a normal extension, so we
            # accept Schoology-hosted links within attachment containers.
            source_host = urlsplit(source_page).netloc
            target_host = urlsplit(absolute).netloc
            same_host = source_host == target_host
            fileish = Path(path).suffix.lower() in FILE_EXTENSIONS

            if not same_host and not fileish:
                continue

            display = link_text or Path(unquote(path)).name or "attachment"
            seen.add(key)
            out.append(
                Attachment(
                    source_page=source_page,
                    href=absolute,
                    display_name=display,
                    folders=list(folders),
                    material_title=material_title,
                )
            )

    return out


def request_html(session: requests.Session, url: str) -> str | None:
    try:
        r = session.get(url, timeout=45, allow_redirects=True)
        if r.status_code == 429:
            time.sleep(10)
            r = session.get(url, timeout=45, allow_redirects=True)
        r.raise_for_status()

        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "<html" not in r.text[:1000].lower():
            return None
        return r.text
    except Exception as exc:
        eprint(f"    WARNING: could not inspect material page: {url}\n             {exc}")
        return None


def derive_filename(
    attachment: Attachment,
    response: requests.Response | None = None,
) -> str:
    # Best source: Content-Disposition.
    if response is not None:
        cd = content_disposition_filename(response.headers.get("Content-Disposition"))
        if cd:
            return sanitize_component(cd, "attachment")

    # Link text often contains the real filename.
    display = attachment.display_name.strip()
    # Remove Schoology tooltip/status text after line breaks where possible.
    display = display.splitlines()[0].strip()
    if looks_like_file_name(display):
        return sanitize_component(display)

    # URL basename.
    url_name = Path(unquote(urlsplit(attachment.href).path)).name
    if looks_like_file_name(url_name):
        return sanitize_component(url_name)

    # Use display name even without extension; content type may add one below.
    return sanitize_component(display or url_name or "attachment")


def extension_for_content_type(content_type: str) -> str:
    c = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
        "application/zip": ".zip",
        "application/epub+zip": ".epub",
    }
    return mapping.get(c, "")


def state_key(attachment: Attachment) -> str:
    return "|".join([
        canonical_url(attachment.source_page),
        canonical_url(attachment.href),
        attachment.display_name.strip(),
    ])


def save_attachment(
    session: requests.Session,
    attachment: Attachment,
    course_dir: Path,
    state: State,
    *,
    refresh: bool,
    dry_run: bool,
) -> str:
    key = state_key(attachment)
    old = state.get(key)

    if old and not refresh:
        old_path = Path(old.get("path", ""))
        if old_path.exists():
            print(f"      ✓ {old_path.name}")
            return "skipped"

    folder_dir = course_dir
    for folder in attachment.folders:
        folder_dir = folder_dir / sanitize_component(folder)
    folder_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        print(f"      WOULD DOWNLOAD: {' / '.join(attachment.folders + [attachment.display_name])}")
        return "dry-run"

    try:
        with session.get(
            attachment.href,
            stream=True,
            timeout=(20, 120),
            allow_redirects=True,
            headers={"Referer": attachment.source_page},
        ) as r:
            if r.status_code == 429:
                time.sleep(10)
                return save_attachment(
                    session, attachment, course_dir, state,
                    refresh=refresh, dry_run=dry_run
                )

            r.raise_for_status()
            ctype = (r.headers.get("Content-Type") or "").lower()

            filename = derive_filename(attachment, r)
            if not Path(filename).suffix:
                ext = extension_for_content_type(ctype)
                if ext:
                    filename += ext

            # If a supposed file resolves to an HTML page, don't save it as a fake file.
            if "text/html" in ctype and Path(filename).suffix.lower() not in {".html", ".htm"}:
                eprint(f"      SKIP (resolved to HTML): {attachment.display_name}")
                return "not-file"

            dest = folder_dir / sanitize_component(filename)
            if old and old.get("sha256") and Path(old.get("path", "")).exists():
                # On --refresh, write temp then compare.
                tmp = dest.with_name(dest.name + ".part")
            else:
                dest = ensure_unique_path(dest)
                tmp = dest.with_name(dest.name + ".part")

            h = hashlib.sha256()
            size = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)

            digest = h.hexdigest()

            if old and refresh and old.get("sha256") == digest and Path(old.get("path", "")).exists():
                tmp.unlink(missing_ok=True)
                print(f"      ✓ unchanged: {Path(old['path']).name}")
                old["last_checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
                state.set(key, old)
                return "skipped"

            # If another file already occupies the chosen name, avoid overwriting it
            # unless it is the exact path previously used for this attachment.
            old_path = Path(old["path"]) if old and old.get("path") else None
            if dest.exists() and (old_path is None or dest.resolve() != old_path.resolve()):
                dest = ensure_unique_path(dest)

            if dest.exists() and old_path and dest.resolve() == old_path.resolve():
                dest.unlink()

            tmp.replace(dest)

            state.set(key, {
                "path": str(dest.resolve()),
                "sha256": digest,
                "size": size,
                "source_page": attachment.source_page,
                "attachment_url": canonical_url(attachment.href),
                "material_title": attachment.material_title,
                "folders": attachment.folders,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

            print(f"      ↓ {dest.name} ({size / 1024:.1f} KB)")
            return "downloaded"

    except Exception as exc:
        eprint(f"      ERROR downloading {attachment.display_name}: {exc}")
        return "error"


def dedupe_material_pages(items: Iterable[MaterialPage]) -> list[MaterialPage]:
    out = []
    seen = set()
    for item in items:
        k = canonical_url(item.url)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def material_pages_from_inventory(inv: dict) -> list[MaterialPage]:
    pages = []
    for key in ("standaloneDocs", "materialPages"):
        for x in inv.get(key, []):
            url = x.get("url")
            if not url:
                continue
            pages.append(
                MaterialPage(
                    url=url,
                    title=x.get("title") or "Material",
                    folders=x.get("folders") or [],
                )
            )
    return dedupe_material_pages(pages)


def direct_attachments_from_inventory(inv: dict, source_page: str) -> list[Attachment]:
    out = []
    seen = set()
    for x in inv.get("directFileLinks", []):
        url = x.get("url")
        if not url:
            continue
        k = canonical_url(url)
        if k in seen:
            continue
        seen.add(k)
        out.append(
            Attachment(
                source_page=source_page,
                href=url,
                display_name=x.get("title") or Path(urlsplit(url).path).name or "file",
                folders=x.get("folders") or [],
                material_title=x.get("title") or "Direct file",
            )
        )
    return out


def save_debug(page: Page, debug_dir: Path, course_name: str):
    debug_dir.mkdir(parents=True, exist_ok=True)
    name = sanitize_component(course_name)
    try:
        (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=True)
    except Exception:
        pass


def sync_course(
    page: Page,
    context: BrowserContext,
    course: Course,
    output_root: Path,
    state: State,
    *,
    refresh: bool,
    dry_run: bool,
    debug_dir: Path,
) -> dict[str, int]:
    print()
    print("=" * 72)
    print(course.name)
    print("=" * 72)

    page.goto(course.url, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(1000)

    course_name = get_course_name(page, course.name)
    course_dir = output_root / sanitize_component(course_name)
    course_dir.mkdir(parents=True, exist_ok=True)

    clicked = expand_all_folders(page)
    if clicked:
        print(f"  Expanded {clicked} Schoology folder(s).")

    page.wait_for_timeout(800)

    try:
        inv = extract_material_inventory(page)
    except Exception as exc:
        eprint(f"  ERROR: could not inspect Materials page: {exc}")
        save_debug(page, debug_dir, course_name)
        return {"downloaded": 0, "skipped": 0, "errors": 1, "not_file": 0}

    # Save diagnostics automatically only when classic Schoology selectors found
    # nothing useful; this makes future selector fixes straightforward.
    if not any(inv.get(k) for k in ("standaloneDocs", "materialPages", "directFileLinks")):
        eprint("  WARNING: no downloadable/material links detected on this course.")
        eprint(f"  Debug snapshot saved under: {debug_dir}")
        save_debug(page, debug_dir, course_name)

    print(
        f"  Found {len(inv.get('standaloneDocs', []))} file material(s), "
        f"{len(inv.get('materialPages', []))} material page candidate(s)."
    )

    session = build_requests_session(context, page)

    # Collect attachments referenced by standalone document pages and by
    # assignments/pages/discussions/etc.
    attachments: list[Attachment] = []
    seen_attachment_keys: set[str] = set()

    pages = material_pages_from_inventory(inv)

    for idx, mat in enumerate(pages, start=1):
        print(f"  [{idx}/{len(pages)}] Inspecting: {mat.title[:70]}")
        html = request_html(session, mat.url)
        if html:
            for a in parse_attachments_from_html(html, mat.url, mat.folders, mat.title):
                k = state_key(a)
                if k not in seen_attachment_keys:
                    seen_attachment_keys.add(k)
                    attachments.append(a)
        time.sleep(REQUEST_DELAY_SECONDS)

    # Add fallback direct file links not already represented.
    for a in direct_attachments_from_inventory(inv, course.url):
        k = state_key(a)
        if k not in seen_attachment_keys:
            seen_attachment_keys.add(k)
            attachments.append(a)

    print(f"  Total unique downloadable attachment(s): {len(attachments)}")

    stats = {"downloaded": 0, "skipped": 0, "errors": 0, "not_file": 0}

    for a in attachments:
        result = save_attachment(
            session,
            a,
            course_dir,
            state,
            refresh=refresh,
            dry_run=dry_run,
        )
        if result == "downloaded":
            stats["downloaded"] += 1
        elif result == "skipped":
            stats["skipped"] += 1
        elif result == "error":
            stats["errors"] += 1
        elif result == "not-file":
            stats["not_file"] += 1

        # Persist state incrementally so Ctrl+C does not lose successful work.
        if result == "downloaded":
            state.save()

    return stats


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download Schoology files while preserving course/folder structure."
    )
    parser.add_argument(
        "profile",
        help="Profile/student name, e.g. hanna or matthew. Each gets its own browser cookies.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/Users/lingyunxu/Library/CloudStorage/GoogleDrive-neilxu@gmail.com/My Drive/Schoology Basis 2026-27"),
        help="Root download folder. Default: Google Drive / Schoology Basis 2026-27",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Schoology base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--list-courses",
        action="store_true",
        help="Log in, discover courses, print them, and exit.",
    )
    parser.add_argument(
        "--course-id",
        action="append",
        default=[],
        help="Only sync this numeric course ID. May be repeated.",
    )
    parser.add_argument(
        "--course-url",
        action="append",
        default=[],
        help="Sync a specific Schoology Materials/course URL. May be repeated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and inspect materials without writing downloaded files.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download known attachments and compare hashes; replace only if changed.",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Playwright slow motion in milliseconds, useful for debugging.",
    )
    return parser.parse_args()


def explicit_courses(args, origin: str) -> list[Course]:
    out: list[Course] = []

    for cid in args.course_id:
        if not re.fullmatch(r"\d+", cid):
            raise ValueError(f"Invalid --course-id: {cid}")
        out.append(Course(cid, f"Course {cid}", f"{origin}/course/{cid}/materials"))

    for raw in args.course_url:
        url = raw.strip()
        m = re.search(r"/course/(\d+)", url)
        cid = m.group(1) if m else hashlib.md5(url.encode()).hexdigest()[:10]
        if "/materials" not in url:
            url = url.rstrip("/") + "/materials"
        out.append(Course(cid, f"Course {cid}", url))

    # dedupe
    deduped = []
    seen = set()
    for c in out:
        if c.course_id in seen:
            continue
        seen.add(c.course_id)
        deduped.append(c)
    return deduped


def main():
    args = parse_args()

    profile_name = sanitize_component(args.profile.lower(), "profile")
    script_dir = Path(__file__).resolve().parent
    work_dir = script_dir / ".schoology_sync"
    profile_dir = work_dir / "profiles" / profile_name
    state_path = work_dir / "state" / f"{profile_name}.json"
    debug_dir = work_dir / "debug" / profile_name

    output_root = args.output.expanduser().resolve() / sanitize_component(args.profile.title())
    output_root.mkdir(parents=True, exist_ok=True)

    print("Schoology Sync")
    print(f"Profile : {args.profile}")
    print(f"Output  : {output_root}")
    print(f"Browser : persistent Chromium profile at {profile_dir}")

    state = State(state_path)

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                accept_downloads=True,
                slow_mo=args.slow_mo,
                viewport={"width": 1440, "height": 1000},
                args=[
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            # Let Schoology use the webcam for QR scanning without an extra
            # browser permission dialog where Chromium supports it.
            try:
                context.grant_permissions(
                    ["camera"],
                    origin=args.base_url.rstrip("/"),
                )
            except Exception:
                pass

            page = context.pages[0] if context.pages else context.new_page()

            origin = ensure_login(page, args.base_url)

            courses = explicit_courses(args, origin)
            if not courses:
                courses = discover_courses(page, origin)

            if args.course_id:
                wanted = set(args.course_id)
                courses = [c for c in courses if c.course_id in wanted]

            if args.list_courses:
                print()
                for c in courses:
                    print(f"{c.course_id:>12}  {c.name}")
                    print(f"{'':>12}  {c.url}")
                context.close()
                return 0

            if not courses:
                eprint("\nNo courses were discovered.")
                eprint("Try opening a course manually and copy its URL, then run:")
                eprint(f'  python {Path(__file__).name} {args.profile} --course-url "PASTE_URL_HERE"')
                context.close()
                return 2

            print("\nCourses to sync:")
            for c in courses:
                print(f"  • {c.name}  [{c.course_id}]")

            totals = {"downloaded": 0, "skipped": 0, "errors": 0, "not_file": 0}

            for course in courses:
                try:
                    stats = sync_course(
                        page,
                        context,
                        course,
                        output_root,
                        state,
                        refresh=args.refresh,
                        dry_run=args.dry_run,
                        debug_dir=debug_dir,
                    )
                    for k, v in stats.items():
                        totals[k] += v
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    totals["errors"] += 1
                    eprint(f"\nERROR syncing {course.name}: {exc}")
                    try:
                        save_debug(page, debug_dir, course.name)
                    except Exception:
                        pass

            state.save()

            print()
            print("=" * 72)
            print("SYNC SUMMARY")
            print("=" * 72)
            print(f"Downloaded : {totals['downloaded']}")
            print(f"Already had: {totals['skipped']}")
            print(f"Not files  : {totals['not_file']}")
            print(f"Errors     : {totals['errors']}")
            print(f"Output     : {output_root}")
            if totals["errors"]:
                print(f"Debug      : {debug_dir}")
            print()

            context.close()
            return 0 if totals["errors"] == 0 else 1

    except KeyboardInterrupt:
        print("\nStopped by user.")
        state.save()
        return 130
    except Exception as exc:
        eprint(f"\nFATAL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
