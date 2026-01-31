from __future__ import annotations
import aiohttp
import asyncio
import mimetypes
import os
import re
import time
import socket
import logging
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse, urljoin   
from telegram import Bot
from .utils import card_progress
from .config import DOWNLOAD_DIR, DL_CHUNK
import html as _html

# Set up explicit logging for this module
log = logging.getLogger(__name__)

_FILE_RE = re.compile(r'filename\*?=([^;]+)', re.I)
_HTML_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)

def _sanitize_candidate(u: str) -> str:
    return u.strip().rstrip(')]>;,.')

def _extract_direct_link_from_html(base_url: str, html_text: str) -> Optional[str]:
    log.info(f"[DEBUG] Scanning HTML for direct links in: {base_url}")
    def U(s: str) -> str:
        return _html.unescape(s.strip())

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
            found = urljoin(base_url, _sanitize_candidate(U(m2.group(1))))
            log.info(f"[DEBUG] Found Meta Refresh link: {found}")
            return found

    # JS Redirects
    for pat in [
        r'window\.location(?:\.href)?\s*=\s*[\'"]([^\'"]+)[\'"]',
        r'location\.replace\(\s*[\'"]([^\'"]+)[\'"]\s*\)',
    ]:
        m = re.search(pat, html_text, re.I)
        if m:
            found = urljoin(base_url, _sanitize_candidate(U(m.group(1))))
            log.info(f"[DEBUG] Found JS Redirect link: {found}")
            return found

    # Generic candidates
    candidates = []
    for u in _HTML_URL_RE.findall(html_text):
        u = _sanitize_candidate(U(u))
        if any(x in u for x in ["googlevideo.com", "/download", "/get", "/file/", "/dl?", "/d/"]):
            candidates.append(urljoin(base_url, u))
    
    if candidates:
        log.info(f"[DEBUG] Found generic candidate link: {candidates[0]}")
        return candidates[0]
    
    log.info("[DEBUG] No direct link found in HTML.")
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
            if "UTF-8''" in v:
                v = v.split("UTF-8''")[-1]
            return sanitize_filename(v)
    path = urlparse(url).path
    name = os.path.basename(path)
    if not name or "." not in name:
        name = "download_file"
    return sanitize_filename(name)

async def download_http(
    url: str, dest_dir: Path, status_updater: Callable[[str], None]
) -> tuple[Path, Optional[str], int]:
    
    log.info(f"[DEBUG] Starting download_http for: {url}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    base_referer = url
    cur_url = url
    mime_hint = None
    total_declared = 0
    name_hint = None

    # Step 1: Probe URL
    log.info("[DEBUG] Creating IPv4 Probe Session...")
    conn_probe = aiohttp.TCPConnector(family=socket.AF_INET)
    timeout = aiohttp.ClientTimeout(total=30) # 30s timeout for probe

    async with aiohttp.ClientSession(connector=conn_probe, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout) as sess:
        try:
            log.info(f"[DEBUG] sending HEAD request to {cur_url}")
            async with sess.head(cur_url, allow_redirects=True) as hr:
                log.info(f"[DEBUG] HEAD status: {hr.status}")
                if hr.status == 200:
                    mime_hint = hr.headers.get("Content-Type")
                    total_declared = int(hr.headers.get("Content-Length") or 0)
                    name_hint = pick_name_from_headers(str(hr.url), hr.headers)
                    log.info(f"[DEBUG] HEAD success. Mime: {mime_hint}, Size: {total_declared}")
        except Exception as e:
            log.error(f"[DEBUG] HEAD failed: {e}")

        try:
            log.info(f"[DEBUG] Sending GET request (probe) to {cur_url}")
            async with sess.get(cur_url, allow_redirects=True) as r:
                log.info(f"[DEBUG] GET status: {r.status}")
                ct = (r.headers.get("Content-Type") or "").lower()
                log.info(f"[DEBUG] Content-Type: {ct}")

                if "text/html" in ct and r.status == 200:
                    log.info("[DEBUG] Response is HTML, downloading text to parse...")
                    txt = await r.text(errors="ignore")
                    nxt = _extract_direct_link_from_html(str(r.url), txt)
                    if nxt:
                        log.info(f"[DEBUG] Switching to extracted URL: {nxt}")
                        cur_url = nxt
                    else:
                        log.warning("[DEBUG] Is HTML but no link found. Proceeding anyway.")
                
                if not name_hint:
                    name_hint = pick_name_from_headers(str(r.url), r.headers)
                    log.info(f"[DEBUG] Name hint found: {name_hint}")
        except Exception as e:
             log.error(f"[DEBUG] Probe GET failed: {e}")
             # Don't crash, try to download main URL anyway
             pass

    name = name_hint if name_hint and "." in name_hint else f"file_{int(time.time())}"
    dest = dest_dir / name
    part = dest.with_suffix(dest.suffix + ".part")
    log.info(f"[DEBUG] Final Dest: {dest}")

    # Step 2: Download
    log.info("[DEBUG] Starting Main Download Session (IPv4)...")
    conn_dl = aiohttp.TCPConnector(family=socket.AF_INET)
    # Disable total timeout for large files
    timeout_dl = aiohttp.ClientTimeout(total=None, connect=60, sock_read=60)

    async with aiohttp.ClientSession(connector=conn_dl, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout_dl) as sess:
        start_time = time.time()
        last_t = start_time
        last_done = 0
        done = 0
        if part.exists():
            done = part.stat().st_size
            log.info(f"[DEBUG] Resuming from {done} bytes")

        try:
            with open(part, "ab") as f:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": base_referer}
                if done > 0:
                    headers["Range"] = f"bytes={done}-"
                
                log.info(f"[DEBUG] Requesting stream from: {cur_url}")
                async with sess.get(cur_url, headers=headers) as r:
                    log.info(f"[DEBUG] Stream Status: {r.status}")
                    r.raise_for_status()

                    if total_declared == 0:
                        total_declared = int(r.headers.get("Content-Length") or 0)
                    
                    if not mime_hint:
                        mime_hint = r.headers.get("Content-Type")

                    log.info(f"[DEBUG] Starting chunk loop. Total: {total_declared}")
                    async for chunk in r.content.iter_chunked(DL_CHUNK):
                        f.write(chunk)
                        done += len(chunk)

                        now = time.time()
                        if now - last_t >= 1.0:
                            # Print progress to log every 5 seconds to debug
                            if int(now) % 5 == 0:
                                log.info(f"[DEBUG] Downloaded {done} bytes...")
                            
                            dt = max(0.001, now - last_t)
                            speed = (done - last_done) / dt
                            eta = (total_declared - done) / speed if (speed > 0 and total_declared) else -1
                            status_updater(card_progress("Downloading File", done, total_declared, speed, now - start_time, eta))
                            last_t, last_done = now, done
                    
                    log.info("[DEBUG] Download stream finished.")

        except Exception as e:
            log.exception(f"[DEBUG] CRASH inside download loop: {e}")
            raise e

    os.replace(part, dest)
    log.info(f"[DEBUG] Renamed part file to {dest}")
    
    # Final extension check
    if "." not in dest.name and mime_hint:
        ext = mimetypes.guess_extension(mime_hint)
        if ext:
            new_dest = dest.with_suffix(ext)
            os.rename(dest, new_dest)
            dest = new_dest
            log.info(f"[DEBUG] Added extension: {dest}")

    return dest, mime_hint, total_declared

async def download_telegram_file(bot: Bot, file_id: str, dest_dir: Path, status_updater: Callable[[str], None]) -> tuple[Path, Optional[str], int]:
    # ... (Keeping Telegram download mostly the same, focusing on HTTP first)
    log.info("[DEBUG] Starting Telegram File Download")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tg_file = await bot.get_file(file_id)
    file_url = tg_file.file_path
    
    base = os.path.basename(file_url)
    name = sanitize_filename(base or "telegram_file")
    dest = dest_dir / name
    
    conn_tg = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=conn_tg) as sess:
        async with sess.get(file_url) as r:
            total = int(r.headers.get("Content-Length") or 0)
            start = time.time()
            last = start
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
                        status_updater(card_progress("Downloading File", done, total, speed, now - start, eta))
                        last, last_done = now, done
    mime, _ = mimetypes.guess_type(dest.name)
    return dest, mime, total
