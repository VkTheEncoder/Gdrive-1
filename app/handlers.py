from __future__ import annotations

import asyncio
import html
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from telegram.error import TimedOut, RetryAfter, NetworkError, BadRequest
from telegram import Update, Message
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from .config import DOWNLOAD_DIR, EDIT_THROTTLE_SECS, GOOGLE_OAUTH_MODE
from .db import (
    delete_creds,
    get_folder,
    load_creds,
    save_creds,
    save_state,
    set_folder,
)
from .drive import (
    build_flow,
    creds_from_token_response,
    device_code_request,
    email_from_id_token,
    get_service_for_user,
    poll_device_token,
    upload_with_progress,
)
from .downloader import download_http, download_telegram_file
from .utils import Throttle, card_done, card_progress

log = logging.getLogger(__name__)

# ---------- constants ----------
MAX_TG_BOT_DOWNLOAD = 20 * 1024 * 1024  # ~20 MB Bot API download limit
_URL_RE = re.compile(r"(https?://\S+)", re.I)

# ---------- queue data structures ----------
@dataclass
class Job:
    update: Update
    context: ContextTypes.DEFAULT_TYPE
    src: str
    from_telegram: bool
    file_id: Optional[str]
    ticket_msg: Message  # the message we keep editing

_job_queue: asyncio.Queue[Job] = asyncio.Queue()
_worker_task: Optional[asyncio.Task] = None
_worker_busy: bool = False


# ---------- helpers ----------
async def safe_edit(
    msg: Message,
    text: str,
    *,
    parse_mode: ParseMode = ParseMode.HTML,
    disable_web_page_preview: bool = True,
    max_tries: int = 4,
):
    """Edit a message with retries on Telegram timeouts/rate limits."""
    last_err = None
    for attempt in range(max_tries):
        try:
            return await msg.edit_text(
                text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 1)) + 0.5)
        except (TimedOut, NetworkError):
            await asyncio.sleep(1.5 * (attempt + 1))
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return msg
            last_err = e
            await asyncio.sleep(0.5)
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.5)
    try:
        return await msg.chat.send_message(
            text, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview
        )
    except Exception:
        if last_err:
            raise last_err


def extract_urls(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text.strip())


async def _drain_pending(tasks: list[asyncio.Task]) -> None:
    """Cancel/await any in-flight progress edits so they can't overwrite final cards."""
    if not tasks:
        return
    for t in tasks:
        if not t.done():
            try:
                t.cancel()
            except Exception:
                pass
    await asyncio.gather(*tasks, return_exceptions=True)
    tasks.clear()


# ---------- user-facing commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "<b>📁 GDrive Uploader Bot</b>\n"
        "Fast, reliable uploads to your Google Drive with live progress cards.\n\n"
        "<b>What I can do</b>\n"
        "• Download from direct links (auto-follows most redirects)  \n"
        "• Upload to your Drive with resumable progress  \n"
        "• Queue multiple jobs (you’ll see your position)  \n"
        "• Optional target folder for uploads\n\n"
        "<b>Getting started</b>\n"
        "1) <code>/login</code> – connect your Google account  \n"
        "2) Send a file (≤ 20\u202fMB via Telegram) or paste a direct HTTP link\n\n"
        "<b>Commands</b>\n"
        "• <code>/login</code> – connect Google Drive  \n"
        "• <code>/logout</code> – disconnect & delete tokens  \n"
        "• <code>/me</code> – show connected account & folder  \n"
        "• <code>/setfolder &lt;folder_id&gt;</code> – set target Drive folder  \n"
        "• <code>/queue</code> – see pending jobs  \n"
        "• <code>/help</code> – show this help\n\n"
        "<b>Notes</b>\n"
        "• Telegram’s Bot API can only download files up to ~20\u202fMB. For larger files, send a direct link.  \n"
        "• I’ll send clear status cards for Downloading → Uploading → Upload complete with the file link.\n\n"
        "<b>Privacy</b>\n"
        "OAuth tokens are stored only to upload to <i>your</i> Drive. Use <code>/logout</code> anytime to remove them.\n\n"
        "<b>📩 Contact @THe_vK_3 if any problem or Query 😇</b>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start(update, context)


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = load_creds(uid)
    folder = get_folder(uid)

    if not data:
        await update.message.reply_text("Not connected. Use /login to connect your Google Drive.")
        return

    email, _ = data
    folder_txt = html.escape(folder) if folder else "Telegram Bot Uploads (auto)"
    text = f"Connected as <b>{html.escape(email)}</b>\nFolder: <code>{folder_txt}</code>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def setfolder_cmd(update, context):
    if not context.args:
        await update.message.reply_text(
            "Usage: /setfolder <drive_folder_id or share link> (or /setfolder none to reset)"
        )
        return

    raw = context.args[0].strip()
    if raw.lower() in ("none", "reset", "default"):
        set_folder(update.effective_user.id, None)
        await update.message.reply_text("Folder reset. I’ll use the default 'Telegram Bot Uploads'.")
        return

    # Accept link or raw id:
    folder_id = raw
    # Optional: extract id from a Drive URL
    # e.g. https://drive.google.com/drive/folders/<ID>?...
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", raw)
    if not m:
        # e.g. ...open?id=<ID>
        m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", raw)
    if m:
        folder_id = m.group(1)

    set_folder(update.effective_user.id, folder_id)
    await update.message.reply_text(
        f"Folder set to: <code>{html.escape(folder_id)}</code>", parse_mode=ParseMode.HTML
    )



# ---------- login/logout ----------
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if GOOGLE_OAUTH_MODE == "web":
        from uuid import uuid4
        state = uuid4().hex
        save_state(state, uid)
        flow = build_flow(state)
        auth_url, _ = flow.authorization_url(
            prompt="consent", access_type="offline", include_granted_scopes="true"
        )
        await update.message.reply_text("Tap to connect your Google Drive:\n" + auth_url)
        return

    # Device Flow (no domain needed)
    dc = await device_code_request()
    status = await update.message.reply_text(
        "🔐 Connect Google Drive\n\n"
        "1) Open: https://www.google.com/device\n"
        f"2) Enter code: `{dc.user_code}`\n\n"
        "I’ll wait while you approve…",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        tok = await poll_device_token(dc.device_code, dc.interval)
        creds_json = creds_from_token_response(tok)
        email = email_from_id_token(tok.get("id_token"))
        save_creds(uid, email, creds_json)
        # CHANGED: use safe_edit instead of direct edit_text
        await safe_edit(status, f"✅ Connected as {email}. You can now send files or links.")
    except Exception as e:
        # CHANGED: use safe_edit instead of direct edit_text
        await safe_edit(status, f"❌ Login failed: {html.escape(str(e))}")


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    delete_creds(update.effective_user.id)
    await update.message.reply_text("Disconnected from Google Drive.")


# ---------- queue internals ----------
def _start_worker(app: Application) -> None:
    """Start the background worker once."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = app.create_task(_queue_worker(app))


async def _queue_worker(app: Application) -> None:
    global _worker_busy
    while True:
        job = await _job_queue.get()
        _worker_busy = True
        try:
            uid = job.update.effective_user.id

            # If the user logged out while queued, don't show "Starting…"
            if not load_creds(uid):
                await safe_edit(job.ticket_msg, "Please /login first to connect your Google Drive.")
                continue

            await safe_edit(job.ticket_msg, "⏳ Starting…")

            await _process_and_upload(
                job.update, job.context, job.src, job.from_telegram, job.file_id,
                existing_status_msg=job.ticket_msg
            )
        except Exception as e:
            try:
                await job.ticket_msg.edit_text(f"❌ Failed: {html.escape(str(e))}", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        finally:
            _worker_busy = False
            _job_queue.task_done()


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = _job_queue.qsize()
    running = 1 if _worker_busy else 0
    total = pending + running

    if total == 0:
        await update.message.reply_text("✅ Queue is empty.")
        return

    msg = [
        "🕗 <b>Queue status</b>",
        f"• Running now: <b>{running}</b>",
        f"• Waiting: <b>{pending}</b>",
        f"• Total: <b>{total}</b>",
        "",
        "Tip: when you add a job I reply with your position. I’ll reuse that same message for progress and results.",
    ]
    await update.message.reply_text("\n".join(msg), parse_mode=ParseMode.HTML)


async def _enqueue_job(update: Update, context: ContextTypes.DEFAULT_TYPE, *, src: str, from_telegram: bool, file_id: Optional[str]):
    uid = update.effective_user.id

    # ⛔ if not connected, do not enqueue and do not create a ticket message
    if not load_creds(uid):
        await update.message.reply_text("Please /login first to connect your Google Drive.")
        return

    position = _job_queue.qsize() + (1 if _worker_busy else 0) + 1
    if position > 1:
        ticket = await update.message.reply_text(
            f"🕗 Queued • Position #{position}\n"
            f"I’ll update this message when your turn starts.",
            disable_web_page_preview=True,
        )
    else:
        ticket = await update.message.reply_text("Preparing…")

    await _job_queue.put(Job(update=update, context=context, src=src, from_telegram=from_telegram, file_id=file_id, ticket_msg=ticket))
    _start_worker(context.application)

# ---------- core flow ----------
async def _process_and_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    src: str,
    from_telegram: bool,
    file_id: Optional[str] = None,
    existing_status_msg: Optional[Message] = None,
):
    uid = update.effective_user.id
    data = load_creds(uid)
    if not data:
        await update.message.reply_text("Please /login first to connect your Google Drive.")
        return

    status_msg = existing_status_msg or await update.message.reply_text("Preparing…")
    throttle = Throttle(EDIT_THROTTLE_SECS)

    # keep track of every async progress edit task
    pending_edits: list[asyncio.Task] = []

    def updater(txt: str):
        if throttle.ready():
            t = asyncio.create_task(
                safe_edit(status_msg, txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            )
            pending_edits.append(t)

    # 1) Download
    try:
        dl_start = time.time()
        if from_telegram and file_id:
            dest, mime, total = await download_telegram_file(
                context.bot, file_id, Path(DOWNLOAD_DIR), updater
            )
        else:
            dest, mime, total = await download_http(src, Path(DOWNLOAD_DIR), updater)
        dl_elapsed = time.time() - dl_start

        size_bytes = dest.stat().st_size
        # drain any straggler progress edits before the final card
        await _drain_pending(pending_edits)
        await safe_edit(
            status_msg,
            card_done("Download complete", file_name=dest.name, size=size_bytes, dl_time=dl_elapsed),
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.exception("Download failed")
        await _drain_pending(pending_edits)
        await safe_edit(status_msg, f"❌ Download failed: {html.escape(str(e))}")
        return

    # 2) Upload
    try:
        ul_start = time.time()
        service, _ = get_service_for_user(uid)
        if not service:
            await _drain_pending(pending_edits)
            await safe_edit(status_msg, "Please /login first to connect your Google Drive.")
            return
        if not mime:
            mime, _ = mimetypes.guess_type(dest.name)

        # initial upload card
        updater(card_progress("Uploading File", 0, size_bytes, 0.0, 0.0, -1))

        link, info = upload_with_progress(service, uid, str(dest), dest.name, mime, updater)
        ul_elapsed = time.time() - ul_start

        size_final = int(info.get("size") or size_bytes)

        # drain pending edits so nothing overwrites the final card
        await _drain_pending(pending_edits)
        await safe_edit(
            status_msg,
            card_done(
                "Upload complete",
                file_name=dest.name,
                size=size_final,
                dl_time=dl_elapsed,
                ul_time=ul_elapsed,
                link=link,
            ),
            disable_web_page_preview=False,
        )
    except Exception as e:
        log.exception("Upload failed")
        await _drain_pending(pending_edits)
        await safe_edit(status_msg, f"❌ Upload failed: {html.escape(str(e))}")
        return


# ---------- update handlers ----------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document or update.message.video or update.message.animation
    if not doc:
        await update.message.reply_text("Send a document/video or a direct link.")
        return

    size = getattr(doc, "file_size", 0) or 0
    if size > MAX_TG_BOT_DOWNLOAD:
        await update.message.reply_text(
            "🚫 Telegram limits bot downloads to 20 MB.\n"
            "Please send a direct HTTP link for larger files."
        )
        return

    await _enqueue_job(update, context, src="telegram", from_telegram=True, file_id=doc.file_id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    urls = extract_urls(update.message.text)
    if not urls:
        await update.message.reply_text("No URL found. Send a direct link or upload a file.")
        return

    await _enqueue_job(update, context, src=urls[0], from_telegram=False, file_id=None)
