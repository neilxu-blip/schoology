#!/usr/bin/env python3
"""
Schoology Sync
==============

Downloads Schoology course file attachments while preserving the Schoology
course/folder hierarchy as closely as possible.

Authentication:
- Uses a persistent Playwright Chromium profile per student/profile.
- If a local QR image is configured, Chromium presents it through a file-backed
  synthetic QR video stream so Schoology can scan it automatically.
- Otherwise the browser remains open for normal manual QR scanning.
- QR images remain local under .schoology_sync/ and should never be committed.

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
import base64
import hashlib
import json
import os
import random
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

# Conservative defaults. Schoology is not a bulk-download API, so keep requests
# human-paced. All can be overridden from the command line.
DEFAULT_CLICK_DELAY_SECONDS = 2.0
DEFAULT_REQUEST_DELAY_SECONDS = 2.5
DEFAULT_DOWNLOAD_DELAY_SECONDS = 1.0
DEFAULT_JITTER_SECONDS = 0.4

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



def sleep_with_jitter(base_seconds: float, jitter_seconds: float = 0.0) -> None:
    """Sleep for a gentle base interval plus a small positive random jitter."""
    delay = max(0.0, base_seconds)
    if jitter_seconds > 0:
        delay += random.uniform(0.0, jitter_seconds)
    time.sleep(delay)


def resolve_qr_image(
    explicit_path: Path | None,
    script_dir: Path,
    profile_name: str,
    manual_qr: bool,
) -> Path | None:
    """
    Resolve an optional QR image.

    If --qr-image is not supplied, auto-detect from the project-local folder:
      qr/<profile>.png|jpg|jpeg|webp

    Example:
      /Users/lingyunxu/Documents/git_projects/schoology/qr/hanna.png
      /Users/lingyunxu/Documents/git_projects/schoology/qr/matthew.png
    """
    if manual_qr:
        return None

    if explicit_path is not None:
        p = explicit_path.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"QR image not found: {p}")
        return p

    qr_dir = script_dir / "qr"
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = qr_dir / f"{profile_name}{ext}"
        if p.is_file():
            return p.resolve()
    return None


def build_qr_fake_camera_y4m(qr_image: Path, destination: Path) -> Path:
    """
    Turn a QR screenshot/photo into a one-frame Y4M video for Chromium's
    file-backed synthetic QR video stream. Chromium loops the file continuously.

    Pillow is imported only when this feature is used so existing manual-login
    installs keep working until the user enables automatic QR login.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError(
            "Automatic QR login needs Pillow. Install it with: uv pip install pillow\n"
            "or: pip install pillow"
        ) from exc

    frame_w, frame_h = 1280, 720

    with Image.open(qr_image) as im:
        # Flatten transparency onto white. QR scanners strongly prefer clean white
        # quiet zones around the code.
        im = im.convert("RGBA")
        white = Image.new("RGBA", im.size, "white")
        white.alpha_composite(im)
        im = white.convert("RGB")

        # Keep the QR/screenshot large but leave a generous white border.
        fitted = ImageOps.contain(
            im,
            (int(frame_w * 0.78), int(frame_h * 0.86)),
            method=Image.Resampling.LANCZOS,
        )
        canvas = Image.new("RGB", (frame_w, frame_h), "white")
        x = (frame_w - fitted.width) // 2
        y = (frame_h - fitted.height) // 2
        canvas.paste(fitted, (x, y))

    ycbcr = canvas.convert("YCbCr")
    y_plane, cb_plane, cr_plane = ycbcr.split()
    cb_plane = cb_plane.resize((frame_w // 2, frame_h // 2), Image.Resampling.BOX)
    cr_plane = cr_plane.resize((frame_w // 2, frame_h // 2), Image.Resampling.BOX)

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(
            f"YUV4MPEG2 W{frame_w} H{frame_h} F5:1 Ip A1:1 C420jpeg\n".encode("ascii")
        )
        f.write(b"FRAME\n")
        f.write(y_plane.tobytes())
        f.write(cb_plane.tobytes())
        f.write(cr_plane.tobytes())
    tmp.replace(destination)
    return destination.resolve()


def build_synthetic_qr_camera_script(qr_image: Path) -> str:
    """
    Minimal automatic QR camera shim.

    Only navigator.mediaDevices.getUserMedia() is replaced. We intentionally do
    not spoof Permissions API, enumerateDevices(), or any other browser API,
    because those broader overrides can interfere with Schoology's QR page.
    """
    suffix = qr_image.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix)
    if mime is None:
        raise ValueError(f"Unsupported QR image type: {qr_image}")

    encoded = base64.b64encode(qr_image.read_bytes()).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"
    data_url_js = json.dumps(data_url)

    return f"""
(() => {{
  const QR_DATA_URL = {data_url_js};

  async function makeQrStream() {{
    const canvas = document.createElement('canvas');
    canvas.width = 1280;
    canvas.height = 720;

    const ctx = canvas.getContext('2d', {{alpha: false}});
    ctx.fillStyle = 'white';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.imageSmoothingEnabled = false;

    const img = new Image();
    await new Promise((resolve, reject) => {{
      img.onload = resolve;
      img.onerror = () => reject(new Error('Could not load saved QR image'));
      img.src = QR_DATA_URL;
    }});

    // Center the QR with a generous white quiet zone around it.
    const maxW = Math.floor(canvas.width * 0.66);
    const maxH = Math.floor(canvas.height * 0.84);
    const scale = Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight);
    const w = Math.max(1, Math.round(img.naturalWidth * scale));
    const h = Math.max(1, Math.round(img.naturalHeight * scale));
    const x = Math.round((canvas.width - w) / 2);
    const y = Math.round((canvas.height - h) / 2);
    ctx.drawImage(img, x, y, w, h);

    const stream = canvas.captureStream(10);

    // Keep frames flowing even though the QR itself is static.
    let tick = false;
    setInterval(() => {{
      tick = !tick;
      ctx.fillStyle = tick ? 'rgb(255,255,255)' : 'rgb(254,254,254)';
      ctx.fillRect(0, 0, 3, 3);
    }}, 100);

    return stream;
  }}

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
    return;
  }}

  const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);

  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {{
    configurable: true,
    value: async (constraints) => {{
      // Only substitute requests that ask for video.
      if (constraints && constraints.video === false) {{
        return original(constraints);
      }}
      return makeQrStream();
    }}
  }});
}})();
"""


def install_synthetic_qr_camera(page: Page, qr_image: Path) -> None:
    """
    Install the minimal QR stream shim before the QR login page loads.
    """
    page.add_init_script(script=build_synthetic_qr_camera_script(qr_image))


def save_qr_login_debug(page: Page, debug_dir: Path, profile_name: str) -> None:
    """Save screenshot/HTML/URL when automatic QR login fails."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    base = debug_dir / f"{profile_name}_qr_login"

    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass

    try:
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass

    try:
        base.with_suffix(".txt").write_text(
            json.dumps({"url": page.url, "title": page.title()}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


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



def force_camera_permission(context: BrowserContext, page: Page | None = None, origin: str | None = None) -> None:
    """
    Aggressively grant camera permission for the automation context.

    Why both Playwright permissions and CDP?
    - Playwright's camera permission is the normal supported route.
    - Chromium's CDP 'videoCapture' permission is an additional fallback that
      helps with persistent profiles that previously remembered a blocked site.
    """
    try:
        # Remove Playwright permission overrides left from an earlier run.
        context.clear_permissions()
    except Exception:
        pass

    # Grant camera to all origins in this automation context. Schoology's QR
    # scanner may initialize through embedded content, so origin-only permission
    # is unnecessarily fragile here.
    try:
        context.grant_permissions(["camera"])
    except Exception as exc:
        eprint(f"WARNING: Playwright could not globally grant camera permission: {exc}")

    if page is None:
        return

    try:
        cdp = context.new_cdp_session(page)

        # Grant video capture globally in Chromium.
        try:
            cdp.send(
                "Browser.setPermission",
                {
                    "permission": {"name": "videoCapture"},
                    "setting": "granted",
                },
            )
        except Exception:
            pass

        # Also grant the exact QR-page origin when known.
        if origin:
            try:
                cdp.send(
                    "Browser.setPermission",
                    {
                        "permission": {"name": "videoCapture"},
                        "setting": "granted",
                        "origin": origin,
                    },
                )
            except Exception:
                pass

        try:
            cdp.detach()
        except Exception:
            pass
    except Exception:
        # The launch flags still provide an independent auto-accept path.
        pass


def camera_self_test(page: Page) -> dict:
    """
    Ask Chromium for one video frame/stream before Schoology's own scanner starts.
    With the synthetic-stream flags this should use the saved QR Y4M file, not the
    physical webcam.
    """
    js = r"""
    async () => {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        return {ok: false, name: "MediaDevicesUnavailable",
                message: "navigator.mediaDevices.getUserMedia is unavailable"};
      }

      let permission = "unknown";
      try {
        if (navigator.permissions && navigator.permissions.query) {
          const p = await navigator.permissions.query({name: "camera"});
          permission = p.state || "unknown";
        }
      } catch (_) {}

      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: true,
          audio: false
        });

        const tracks = stream.getVideoTracks();
        const label = tracks.length ? tracks[0].label : "";
        for (const t of stream.getTracks()) t.stop();

        return {ok: true, permission, label};
      } catch (e) {
        return {
          ok: false,
          permission,
          name: e && e.name ? e.name : "UnknownError",
          message: e && e.message ? e.message : String(e)
        };
      }
    }
    """
    try:
        return page.evaluate(js)
    except Exception as exc:
        return {
            "ok": False,
            "name": type(exc).__name__,
            "message": str(exc),
            "permission": "unknown",
        }


def prepare_automatic_qr_camera(page: Page, qr_url: str) -> None:
    """
    Make sure the synthetic QR video stream is usable before allowing Schoology's scanner
    to initialize. If the QR page initially rendered its "Camera is blocked"
    state, reload it after permission has been forced.
    """
    context = page.context
    p = urlsplit(qr_url)
    qr_origin = f"{p.scheme}://{p.netloc}"

    force_camera_permission(context, page, qr_origin)

    # Reload after permission changes so Schoology does not keep an error state
    # from an earlier getUserMedia() call made during initial page load.
    try:
        page.reload(wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(800)
    except Exception:
        pass

    result = camera_self_test(page)

    if result.get("ok"):
        label = result.get("label") or "fake video device"
        print(f"✓ Fake camera is available to Chromium ({label}).")

        # Our self-test stopped its stream. Reload once more so Schoology's QR
        # scanner starts fresh and owns the synthetic-stream stream.
        try:
            page.reload(wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(800)
        except Exception:
            pass
        return

    raise RuntimeError(
        "Chromium still cannot access the camera stream. "
        f"Permission={result.get('permission', 'unknown')}; "
        f"{result.get('name', 'CameraError')}: {result.get('message', '')}. "
        "On macOS, also check System Settings > Privacy & Security > Camera. "
        "If Chromium/Google Chrome is listed there, allow camera access and rerun."
    )



def _filtered_replay_headers(headers: dict) -> dict:
    """
    Keep only headers that are useful to replay the application request.
    Cookies/session headers are deliberately omitted because the current
    Playwright browser context supplies its own cookies.
    """
    allowed = {
        "accept",
        "content-type",
        "x-requested-with",
        "origin",
        "referer",
    }
    out = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in allowed:
            out[lk] = v
    return out


def _qr_request_score(method: str, url: str, post_data: str | None) -> int:
    """
    Rank same-origin requests that are likely to be the QR authentication call.
    We save several high-scoring candidates because Schoology may change the
    endpoint name without notice.
    """
    lower = url.lower()
    score = 0

    if method.upper() in {"POST", "PUT", "PATCH"}:
        score += 6
    if post_data:
        score += 4
    if "/login/qr" in lower:
        score += 8
    elif "qr" in lower:
        score += 5
    if "login" in lower:
        score += 4
    if "auth" in lower:
        score += 3
    if "session" in lower:
        score += 2

    # Penalize obvious static resources.
    if re.search(r"\.(js|css|png|jpe?g|gif|svg|woff2?|ico)(?:\?|$)", lower):
        score -= 20

    return score


def _save_learned_qr_requests(
    path: Path,
    captured: list[dict],
    *,
    login_completed_at: float,
    base_url: str,
) -> int:
    """
    Save the most plausible requests immediately preceding a successful manual
    QR login. The saved body can contain the QR credential/hash, so this file
    must stay private under .schoology_sync/.
    """
    base_host = urlsplit(base_url).netloc.lower()

    candidates = []
    for item in captured:
        age = login_completed_at - float(item.get("time", 0))
        if age < -1 or age > 20:
            continue

        try:
            host = urlsplit(item["url"]).netloc.lower()
        except Exception:
            continue
        if host != base_host:
            continue

        score = _qr_request_score(
            item.get("method", ""),
            item.get("url", ""),
            item.get("post_data"),
        )
        if score <= 0:
            continue

        cleaned = {
            "method": item.get("method", "GET").upper(),
            "url": item.get("url"),
            "headers": _filtered_replay_headers(item.get("headers", {})),
            "post_data": item.get("post_data"),
            "score": score,
        }
        candidates.append(cleaned)

    # Highest score first; de-duplicate same method+URL+body.
    candidates.sort(key=lambda x: x["score"], reverse=True)
    unique = []
    seen = set()
    for c in candidates:
        key = (c["method"], c["url"], c.get("post_data"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    # Keep a small set so replay does not generate unnecessary requests.
    unique = unique[:8]

    if not unique:
        return 0

    payload = {
        "version": 1,
        "warning": "Contains Schoology QR login credential/request data. Keep private.",
        "base_url": base_url,
        "learned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "requests": unique,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)

    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

    return len(unique)


def _load_learned_qr_requests(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        reqs = data.get("requests", [])
        return reqs if isinstance(reqs, list) else []
    except Exception:
        return []


def _is_authenticated_context(context: BrowserContext, base_url: str) -> bool:
    candidate = find_authenticated_page(context)
    if candidate is not None:
        return True
    authenticated, _ = browser_session_authenticated(context, base_url)
    return authenticated


def _replay_one_qr_request(page: Page, item: dict) -> dict:
    """
    Replay a learned request inside the Schoology page so current anonymous
    cookies, CSRF cookies, redirects, and browser networking behavior are used.
    """
    payload = {
        "url": item.get("url"),
        "method": item.get("method", "GET"),
        "headers": item.get("headers") or {},
        "body": item.get("post_data"),
    }

    return page.evaluate(
        r"""async (p) => {
            try {
                const opts = {
                    method: p.method || "GET",
                    credentials: "include",
                    redirect: "follow",
                    headers: p.headers || {}
                };

                if (p.body !== null && p.body !== undefined &&
                    !["GET", "HEAD"].includes((p.method || "GET").toUpperCase())) {
                    opts.body = p.body;
                }

                const r = await fetch(p.url, opts);
                return {
                    ok: r.ok,
                    status: r.status,
                    finalUrl: r.url
                };
            } catch (e) {
                return {
                    ok: false,
                    status: 0,
                    error: e && e.message ? e.message : String(e)
                };
            }
        }""",
        payload,
    )


def try_learned_qr_login(
    page: Page,
    base_url: str,
    auth_path: Path,
) -> bool:
    """
    Try saved QR-login request candidates. Return True only when the Schoology
    browser context becomes authenticated.
    """
    requests_to_try = _load_learned_qr_requests(auth_path)
    if not requests_to_try:
        return False

    print(f"Trying saved QR login ({len(requests_to_try)} request candidate(s))...")

    for index, item in enumerate(requests_to_try, start=1):
        try:
            result = _replay_one_qr_request(page, item)
        except Exception as exc:
            eprint(f"  saved QR request {index} could not be replayed: {exc}")
            continue

        # Give Schoology a moment to set session cookies / finish redirects.
        page.wait_for_timeout(1200)

        if _is_authenticated_context(page.context, base_url):
            print("✓ Saved QR login succeeded.")
            return True

        # Some endpoints respond with a redirect URL that the fetch follows but
        # do not navigate the page. Authentication is determined from cookies,
        # so there is nothing else to do here.

    print("Saved QR login did not authenticate; falling back to one manual scan.")
    return False


def manual_qr_login_and_learn(
    page: Page,
    base_url: str,
    auth_path: Path,
) -> str | None:
    """
    Open Schoology's normal QR scanner, let the user scan once with the physical
    QR code, and learn the network request that performed authentication.
    """
    context = page.context
    captured: list[dict] = []

    def on_request(request):
        try:
            url = request.url
            if urlsplit(url).netloc.lower() != urlsplit(base_url).netloc.lower():
                return

            captured.append({
                "time": time.time(),
                "method": request.method,
                "url": url,
                "headers": request.headers,
                "post_data": request.post_data,
            })

            # Prevent unbounded growth if the page emits lots of requests.
            if len(captured) > 300:
                del captured[:-300]
        except Exception:
            pass

    page.on("request", on_request)

    qr_url = base_url.rstrip("/") + QR_PATH
    page.goto(qr_url, wait_until="domcontentloaded", timeout=45_000)

    print()
    print("=" * 70)
    print("ONE-TIME QR LEARNING LOGIN")
    print("Scan the QR code normally with the physical camera.")
    print("After login succeeds, the program will save the QR-auth request locally")
    print("so future runs can try to log in automatically.")
    print("=" * 70)

    detected = False
    final_url = None

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

    try:
        page.remove_listener("request", on_request)
    except Exception:
        pass

    if not detected:
        raise RuntimeError(
            "I could not detect the completed Schoology QR login after 3 minutes."
        )

    learned = _save_learned_qr_requests(
        auth_path,
        captured,
        login_completed_at=time.time(),
        base_url=base_url,
    )

    if learned:
        print(f"✓ QR login detected. Learned {learned} authentication request candidate(s).")
        print(f"  Saved privately to: {auth_path}")
    else:
        print("✓ QR login detected.")
        print("WARNING: Login succeeded, but no replayable QR request was identified.")
        print("Future runs may still require a manual scan.")

    return final_url


def ensure_login(
    page: Page,
    base_url: str,
    *,
    qr_auth_path: Path,
    force_manual_qr: bool = False,
) -> str:
    """
    Ensure the persistent browser context is authenticated.

    Login order:
      1. Existing persistent browser session.
      2. Previously learned Schoology QR-auth request.
      3. One manual QR scan, while learning the successful request for later.
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
        print("Schoology requires login.")

        # Load the normal QR page first. This establishes any anonymous cookies
        # Schoology expects before the learned login request is replayed.
        qr_url = base_url.rstrip("/") + QR_PATH
        try:
            page.goto(qr_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(700)
        except PlaywrightTimeoutError:
            pass

        replayed = False
        if not force_manual_qr and qr_auth_path.exists():
            replayed = try_learned_qr_login(page, base_url, qr_auth_path)

        if replayed:
            authenticated, final_url = browser_session_authenticated(context, base_url)
        else:
            final_url = manual_qr_login_and_learn(page, base_url, qr_auth_path)

        if not _is_authenticated_context(context, base_url):
            raise RuntimeError("Schoology QR authentication did not create a valid session.")

    # Normalize onto authenticated Schoology home.
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
        page.goto(base_url.rstrip("/") + "/home", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(500)

    if logged_out(page):
        raise RuntimeError(
            "Schoology authentication appeared to succeed, but /home returned to login."
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


def expand_all_folders(
    page: Page,
    max_rounds: int = 80,
    *,
    click_delay: float = DEFAULT_CLICK_DELAY_SECONDS,
    jitter: float = DEFAULT_JITTER_SECONDS,
) -> int:
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
                sleep_with_jitter(click_delay, jitter)
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
            eprint("    Schoology returned HTTP 429; backing off for 30 seconds...")
            time.sleep(30)
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
    course_name: str,
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
        relative_name = " / ".join(attachment.folders + [attachment.display_name])
        print(f"      WOULD DOWNLOAD: {course_name} → {relative_name}")
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
                eprint("      Schoology returned HTTP 429; backing off for 30 seconds...")
                time.sleep(30)
                return save_attachment(
                    session, attachment, course_dir, state,
                    course_name=course_name,
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

            relative_name = " / ".join(attachment.folders + [dest.name])
            print(f"      ↓ NEW: {course_name} → {relative_name} ({size / 1024:.1f} KB)")
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
    click_delay: float,
    request_delay: float,
    download_delay: float,
    jitter: float,
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

    clicked = expand_all_folders(page, click_delay=click_delay, jitter=jitter)
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
        sleep_with_jitter(request_delay, jitter)

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
            course_name=course_name,
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

        # Existing items are skipped without network I/O, so don't slow those down.
        # Pace only operations that actually contacted a download URL.
        if result in {"downloaded", "error", "not-file"}:
            sleep_with_jitter(download_delay, jitter)

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
    parser.add_argument(
        "--qr-image",
        type=Path,
        default=None,
        help=(
            "Deprecated compatibility option. v1.9 learns the QR authentication "
            "request from one normal manual scan instead of emulating a camera."
        ),
    )
    parser.add_argument(
        "--manual-qr",
        action="store_true",
        help="Force a real-camera QR scan and refresh the locally learned QR login request.",
    )
    parser.add_argument(
        "--click-delay",
        type=float,
        default=DEFAULT_CLICK_DELAY_SECONDS,
        help=f"Seconds between Schoology folder clicks. Default: {DEFAULT_CLICK_DELAY_SECONDS}",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help=f"Seconds between material-page requests. Default: {DEFAULT_REQUEST_DELAY_SECONDS}",
    )
    parser.add_argument(
        "--download-delay",
        type=float,
        default=DEFAULT_DOWNLOAD_DELAY_SECONDS,
        help=f"Seconds between actual file download attempts. Default: {DEFAULT_DOWNLOAD_DELAY_SECONDS}",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=DEFAULT_JITTER_SECONDS,
        help=f"Extra random delay added to pacing intervals. Default: 0-{DEFAULT_JITTER_SECONDS}s",
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

    for name, value in (
        ("--click-delay", args.click_delay),
        ("--request-delay", args.request_delay),
        ("--download-delay", args.download_delay),
        ("--jitter", args.jitter),
    ):
        if value < 0:
            raise ValueError(f"{name} cannot be negative")

    qr_auth_path = work_dir / "qr_auth" / f"{profile_name}.json"

    output_root = args.output.expanduser().resolve() / sanitize_component(args.profile.title())
    output_root.mkdir(parents=True, exist_ok=True)

    print("Schoology Sync")
    print(f"Profile : {args.profile}")
    print(f"Output  : {output_root}")
    print(f"Browser : persistent Chromium profile at {profile_dir}")
    if qr_auth_path.exists() and not args.manual_qr:
        print(f"QR login: learned automatic request ({qr_auth_path})")
    else:
        print("QR login: one-time manual learning scan")
    print(
        f"Pacing  : clicks {args.click_delay:.1f}s, material requests {args.request_delay:.1f}s, "
        f"downloads {args.download_delay:.1f}s (+ up to {args.jitter:.1f}s jitter)"
    )

    state = State(state_path)

    try:
        with sync_playwright() as p:
            chromium_args = [
                "--disable-blink-features=AutomationControlled",
            ]

            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                accept_downloads=True,
                slow_mo=args.slow_mo,
                viewport={"width": 1440, "height": 1000},
                args=chromium_args,
            )

            page = context.pages[0] if context.pages else context.new_page()

            origin = ensure_login(
                page,
                args.base_url,
                qr_auth_path=qr_auth_path,
                force_manual_qr=args.manual_qr,
            )

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
                        click_delay=args.click_delay,
                        request_delay=args.request_delay,
                        download_delay=args.download_delay,
                        jitter=args.jitter,
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
