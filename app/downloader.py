from __future__ import annotations
import aiohttp
import asyncio
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse, urljoin   
from telegram import Bot
from telegram.constants import FileDownloadOutOfRange
from .utils import card_progress
from .config import DOWNLOAD_DIR, DL_CHUNK
import html as _html  # for unescaping &amp; etc
from aiohttp import ClientPayloadError, ClientConnectorError, ClientResponseError

_FILE_RE = re.compile(r'filename\*?=([^;]+)', re.I)

_HTML_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)

def _sanitize_candidate(u: str) -> str:
    # trim junk some pages append outside quotes, e.g. css url(...) ending ')'
    return u.strip().rstrip(')]>;,.')

def _extract_direct_link_from_html(base_url: str, html_text: str) -> Optional[str]:
    """
    Pull a real file/redirect URL out of an HTML landing page.
    Handles:
      - <meta http-equiv=refresh ... content="...;url=...">
      - JS redirects: window.location[.href]=..., location.replace(...), setTimeout(...)
      - CSS/JS url('...') constructs
      - <a href="..."> with download-ish text or file extensions
      - Generic candidates that look like direct endpoints
    """
    def U(s: str) -> str:
        return _html.unescape(s.strip())

    # 1) META REFRESH (attribute order varies on Blogspot)
    meta_pat = re.compile(
        r'<meta[^>]*?(?:http-equiv\s*=\s*["\']?refresh["\']?[^>]*?content\s*=\s*["\']([^"\']+)["\']'
        r'|content\s*=\s*["\']([^"\']+)["\'][^>]*?http-equiv\s*=\s*["\']?refresh["\']?)[^>]*?>',
        re.I,
    )
    m = meta_pat.search(html_text)
    if m:
        content = (m.group(1) or m.group(2) or "")
        m2 = re.search(r'url\s*=\s*([^;,\s]+)', content, re.I)
        if m2:
            return urljoin(base_url, _sanitize_candidate(U(m2.group(1))))

    # 2) JS redirects
    for pat in [
        r'window\.location(?:\.href)?\s*=\s*[\'"]([^\'"]+)[\'"]',
        r'location\.replace\(\s*[\'"]([^\'"]+)[\'"]\s*\)',
        r'setTimeout\([^)]*?window\.location(?:\.href)?\s*=\s*[\'"]([^\'"]+)[\'"]',
    ]:
        m = re.search(pat, html_text, re.I)
        if m:
            return urljoin(base_url, _sanitize_candidate(U(m.group(1))))

    # 3) CSS url(...) patterns (avoid grabbing the trailing ')')
    css_pat = re.compile(r'url\(\s*([\'"]?)(https?://[^)\'"]+)\1\s*\)', re.I)
    m = css_pat.search(html_text)
    if m:
        return urljoin(base_url, _sanitize_candidate(U(m.group(2))))

    # 4) <a href="..."> anchors — prefer obvious downloads
    a_pat = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
    for href, text in a_pat.findall(html_text):
        txt = re.sub(r'\s+', ' ', text).strip().lower()
        href_u = _sanitize_candidate(U(href))
        if any(w in txt for w in ["download", "click here", "continue", "get file"]):
            return urljoin(base_url, href_u)
        if re.search(r'\.(mp4|mkv|webm|mov|mp3|flac|wav|zip|rar|7z|pdf|srt|ass)(\?|#|$)', href_u, re.I):
            return urljoin(base_url, href_u)

    # 5) Generic candidates seen on page (avoid random googleusercontent *images*)
    candidates = []
    for u in _HTML_URL_RE.findall(html_text):
        u = _sanitize_candidate(U(u))
        # skip obvious theme/image assets
        if re.search(r'(blogger|themes)\.googleusercontent\.com', u):
            continue
        # prefer endpoints that smell like file downloads
        if any(x in u for x in [
            "googlevideo.com", "uc?export=download",
            "/download", "/get", "/api/dl", "/file/", "/dl?", "/d/"
        ]):
            candidates.append(urljoin(base_url, u))
    if candidates:
        return candidates[0]

    return None

def sanitize_filename(name: str) -> str:
    name = unquote(name)
    name = name.strip().replace("\n", " ").replace("\r", " ")
    name = re.sub(r'[\\/*?:"<>|]+', "_", name)
    return name[:240] or "file"

def pick_name_from_headers(url: str, headers: dict) -> str:
    cd = headers.get("Content-Disposition") or headers.get("content-disposition")
    if cd:
        m = _FILE_RE.search(cd)
        if m:
            v = m.group(1).strip().strip('"').strip("'")
            v = v.split("UTF-8''")[-1]
            return sanitize_filename(v)
    # Fallback to URL path
    path = urlparse(url).path
    name = os.path.basename(path) or "file"
    return sanitize_filename(name)

async def download_http(
    url: str, dest_dir: Path, status_updater: Callable[[str], None]
) -> tuple[Path, Optional[str], int]:
    """
    Robust HTTP downloader with:
    - HTML landing page handling (extracts real file URL)
    - Resume via Range when connection drops or server lies about Content-Length
    Returns: (dest_path, mime, total_bytes)
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Use the original URL as Referer for all hops/requests
    base_referer = url
    # --- Follow up to 3 HTML landing pages to a direct file URL ---
    max_html_hops = 5
    cur_url = url
    mime_hint = None
    total_declared = 0
    name_hint = None

    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as sess:
        for _ in range(max_html_hops + 1):
            # HEAD first (optional hint)
            try:
                async with sess.head(cur_url, allow_redirects=True) as hr:
                    if hr.status // 100 == 2:
                        mime_hint = mime_hint or hr.headers.get("Content-Type")
                        total_declared = int(hr.headers.get("Content-Length") or 0)
                        if not name_hint:
                            name_hint = pick_name_from_headers(str(hr.url), hr.headers)
            except Exception:
                pass

            # GET once to see if it's HTML or a file
            async with sess.get(
                cur_url,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0", "Referer": base_referer},
            ) as r:
                ct = (r.headers.get("Content-Type") or "").lower()
                if ct.startswith("text/html") and (r.status // 100 == 2):
                    # This is an HTML page. Read it and try to extract the real file URL.
                    txt = await r.text(errors="ignore")
                    nxt = _extract_direct_link_from_html(str(r.url), txt)
                    if nxt and nxt != cur_url:
                        cur_url = nxt
                        continue  # try again (next hop)
                    # No direct link found; bail out with a friendly message.
                    raise RuntimeError(
                        "This URL opens a web page, not a direct file. "
                        "Open it in a browser and copy the final download link (it should end with the file)."
                    )
                else:
                    # We got a file response (or at least not HTML) — proceed to real download with this URL.
                    # Let the streaming/resume logic below handle it; we won't reuse this response.
                    break

    # Decide filename
    # Decide filename using real response headers
    if not name_hint:
        name = pick_name_from_headers(str(r.url), r.headers)
    else:
        name = name_hint
    
    dest = dest_dir / name

    part = dest.with_suffix(dest.suffix + ".part")

    # Progress state
    start_time = time.time()
    last_t = start_time
    last_done = 0
    done = part.stat().st_size if part.exists() else 0
    total = total_declared

    max_retries = 5
    retries_left = max_retries

    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as sess:
        with open(part, "ab") as f:
            while True:
                base_headers = {"User-Agent": "Mozilla/5.0", "Referer": base_referer}
                headers = dict(base_headers)
                if done > 0:
                    headers["Range"] = f"bytes={done}-"

                try:
                    async with sess.get(
                        cur_url,
                        allow_redirects=True,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=None),
                    ) as r:
                        r.raise_for_status()

                        # Determine total size
                        cr = r.headers.get("Content-Range")
                        if cr and "bytes" in cr and "/" in cr:
                            try:
                                total = int(cr.split("/")[-1])
                            except Exception:
                                pass
                        if total == 0:
                            try:
                                total = int(r.headers.get("Content-Length") or 0)
                            except Exception:
                                total = 0

                        # If server ignored Range and sent full 200, restart clean
                        if done > 0 and r.status == 200:
                            f.seek(0); f.truncate(0)
                            done = 0
                            last_done = 0
                            start_time = last_t = time.time()

                        if not mime_hint:
                            mime_hint = r.headers.get("Content-Type")

                        async for chunk in r.content.iter_chunked(DL_CHUNK):
                            f.write(chunk)
                            done += len(chunk)

                            now = time.time()
                            if now - last_t >= 1.0:
                                dt = max(0.001, now - last_t)
                                speed = (done - last_done) / dt
                                eta = (total - done) / speed if (speed > 0 and total) else -1
                                elapsed = now - start_time
                                status_updater(card_progress("Downloading File", done, total, speed, elapsed, eta))
                                last_t, last_done = now, done

                        # Finished this response
                        if total == 0 or done >= total:
                            break

                        # Server closed early — retry and fetch remainder
                        retries_left = max_retries

                except (ClientPayloadError, ClientConnectorError, asyncio.TimeoutError, ConnectionResetError) as e:
                    if retries_left > 0:
                        retries_left -= 1
                        await asyncio.sleep(1.0)
                        continue
                    raise

    os.replace(part, dest)
    return dest, mime_hint, total


async def download_telegram_file(bot: Bot, file_id: str, dest_dir: Path, status_updater: Callable[[str], None]) -> tuple[Path, Optional[str], int]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    tg_file = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{bot.token}/{tg_file.file_path}"
    base = os.path.basename(tg_file.file_path)
    name = sanitize_filename(base or "telegram_file")
    dest = dest_dir / name
    async with aiohttp.ClientSession() as sess:
        async with sess.get(file_url) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            start = last = time.time()
            done = 0
            last_done = 0
            with open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(DL_CHUNK):
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last >= 1.0:
                        dt = max(0.001, now - last)
                        speed = (done - last_done) / dt
                        eta = (total - done) / speed if (speed > 0 and total) else -1
                        elapsed = now - start
                        status_updater(card_progress("Downloading File", done, total, speed, elapsed, eta))
                        last, last_done = now, done
    mime, _ = mimetypes.guess_type(dest.name)
    return dest, mime, total
