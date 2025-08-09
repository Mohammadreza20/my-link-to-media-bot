# video_bot.py
import os
import re
import time
import tempfile
import logging
import asyncio
import requests
from bs4 import BeautifulSoup
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler
)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "PUT_YOUR_BOT_TOKEN_HERE"
# Optional site login credentials (if your site requires login)
SITE_USERNAME = os.getenv("SITE_USERNAME")  # or set below
SITE_PASSWORD = os.getenv("SITE_PASSWORD")

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- State ----------
BUSY = {}             # user_id -> True if a job is running
CANCEL_FLAGS = {}     # user_id -> True if user requested cancel

# ---------- Helper: login session ----------
def create_session(username=None, password=None):
    s = requests.Session()
    # If credentials provided, try login (change keys to match site)
    if username and password:
        login_url = "https://www.whoreshub.com/login/"  # change if needed
        payload = {
            "username": username,
            "pass": password,
            "action": "login",
            "email_link": "https://www.whoreshub.com/email/"
        }
        r = s.post(login_url, data=payload, timeout=15)
        logger.info("Login status: %s", r.status_code)
    return s
    
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text.strip()
    print(f"📩 Received URL: {user_message}")  # Debug print
    await update.message.reply_text("Got your URL! Processing...")

    try:
        video_url = extract_best_video(user_message)  # your custom function
        print(f"🎯 Extracted video URL: {video_url}")
        await update.message.reply_video(video_url)
    except Exception as e:
        print(f"❌ Error: {e}")
        await update.message.reply_text("Sorry, something went wrong.")


# ---------- Helper: extract mp4 links from page ----------
def extract_mp4_links(session: requests.Session, page_url: str):
    """
    Try to find direct mp4 links on the page.
    If the page contains an async/data-limit-url block, fetch that block and parse it too.
    Returns list of tuples: (href, label_text)
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    r = session.get(page_url, headers=headers, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    links = []
    # Common: links inside ul.tags-list a
    for a in soup.select("ul.tags-list a[href$='.mp4']"):
        href = a.get("href")
        text = a.get_text(strip=True)
        if href:
            links.append((href, text))

    # If none found, check for data-limit-url (site loads block asynchronously)
    if not links:
        el = soup.select_one("div.tab-box[data-limit-url], a.js-limit-url[data-limit-url], a.js-limit-url[href]")
        if el:
            # try to find data-limit-url attribute or href
            async_url = el.get("data-limit-url") or el.get("href")
            if async_url:
                # make absolute if needed
                if async_url.startswith("/"):
                    base = "{0}://{1}".format(requests.utils.urlparse(page_url).scheme, requests.utils.urlparse(page_url).netloc)
                    async_url = base + async_url
                r2 = session.get(async_url, headers=headers, timeout=15)
                if r2.ok:
                    soup2 = BeautifulSoup(r2.text, "html.parser")
                    for a in soup2.select("a[href$='.mp4']"):
                        href = a.get("href")
                        text = a.get_text(strip=True)
                        if href:
                            links.append((href, text))

    # Fallback: search entire page for any .mp4 link in anchors or script
    if not links:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".mp4" in href:
                links.append((href, a.get_text(strip=True) or href))

        # also try to parse script blocks for "mp4" urls
        if not links:
            scripts = soup.find_all("script")
            for sc in scripts:
                txt = sc.string or sc.get_text()
                found = re.findall(r"(https?://[^\s\"']+\.mp4[^\s\"']*)", txt)
                for f in found:
                    links.append((f, "from_script"))

    # Normalize relative urls
    normalized = []
    for href, txt in links:
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/"):
            parsed = requests.utils.urlparse(page_url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        normalized.append((href, txt))
    return normalized

# ---------- Helper: parse resolution ----------
def resolution_priority(href, label=""):
    """Return resolution integer (e.g., 2160, 1080, 720, 480), 0 if none found."""
    # Try to find patterns like 2160p or 1080p or _2160 or -4k
    m = re.search(r"(\d{3,4})p", href, re.IGNORECASE) or re.search(r"(\d{3,4})p", label, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.search(r"\b4k\b", href, re.IGNORECASE) or re.search(r"\b4k\b", label, re.IGNORECASE):
        return 2160
    # if nothing found, try to infer from filename (rare)
    return 0

# ---------- Helper: HEAD check public accessibility ----------
def is_publicly_accessible(url: str, timeout=10):
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code == 200:
            # If content-length exists and >0, consider accessible
            cl = r.headers.get("Content-Length")
            if cl and int(cl) > 0:
                return True, int(cl)
        return False, 0
    except Exception as e:
        logger.info("HEAD check failed: %s", e)
        return False, 0

# ---------- Download function (blocking) with progress callback ----------
def download_file(session: requests.Session, url: str, out_path: str, user_id: int, progress_cb=None):
    headers = {"User-Agent":"Mozilla/5.0"}
    with session.get(url, stream=True, headers=headers, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        downloaded = 0
        start = time.time()
        chunk_size = 256 * 1024
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if CANCEL_FLAGS.get(user_id):
                    return False, downloaded, total
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    elapsed = time.time() - start
                    speed = downloaded / (elapsed + 1e-6)
                    eta = int((total - downloaded) / (speed + 1e-6)) if total else 0
                    if progress_cb:
                        progress_cb(downloaded, total, speed, eta)
    return True, downloaded, total

# ---------- Upload function using requests_toolbelt to observe progress ----------
def upload_file_to_telegram(bot_token, chat_id, file_path, progress_cb=None):
    """
    Upload via Telegram HTTP API with a MultipartEncoderMonitor so we can call progress_cb(bytes_sent, total, speed, eta).
    This is blocking.
    """
    send_url = f"https://api.telegram.org/bot{bot_token}/sendVideo"
    filename = os.path.basename(file_path)
    filesize = os.path.getsize(file_path)
    with open(file_path, "rb") as f:
        encoder = MultipartEncoder(fields={
            "chat_id": str(chat_id),
            "video": (filename, f, "video/mp4"),
            "supports_streaming": "true"
        })
        start = time.time()
        def _callback(monitor):
            sent = monitor.bytes_read
            elapsed = time.time() - start
            speed = sent / (elapsed + 1e-6)
            eta = int((monitor.len - sent) / (speed + 1e-6)) if monitor.len else 0
            if progress_cb:
                # provide bytes_sent relative to actual file; monitor.len includes boundary overhead but it's okay
                progress_cb(sent, monitor.len, speed, eta)
        monitor = MultipartEncoderMonitor(encoder, _callback)
        headers = {"Content-Type": monitor.content_type}
        r = requests.post(send_url, data=monitor, headers=headers, timeout=3600)
        r.raise_for_status()
        return r.json()

# ---------- Async helpers to edit messages from threads ----------
def thread_safe_edit(bot, chat_id, message_id, text):
    """
    Schedule an edit_message_text from a thread using asyncio run_coroutine_threadsafe.
    """
    coro = bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
    loop = asyncio.get_event_loop()
    asyncio.run_coroutine_threadsafe(coro, loop)

def thread_safe_edit_reply_markup(bot, chat_id, message_id, text, reply_markup=None):
    coro = bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
    loop = asyncio.get_event_loop()
    asyncio.run_coroutine_threadsafe(coro, loop)

# ---------- Bot conversation flow ----------
AWAITING_URL, AWAIT_CONFIRM = range(2)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📥 Send me a video page URL (from your site). I will find the best quality and offer to download/upload.")

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if BUSY.get(user_id):
        await update.message.reply_text("⏳ You already have a task running. Please wait or /cancel.")
        return ConversationHandler.END

    page_url = update.message.text.strip()
    await update.message.reply_text("🔎 Analyzing page, please wait...")

    # create a session (site login if env creds present)
    session = create_session(SITE_USERNAME, SITE_PASSWORD)

    # fetch links (run in thread)
    try:
        links = await asyncio.to_thread(extract_mp4_links, session, page_url)
    except Exception as e:
        await update.message.reply_text(f"❌ Error while parsing page: {e}")
        return ConversationHandler.END

    if not links:
        await update.message.reply_text("❌ No mp4 links found on page.")
        return ConversationHandler.END

    # choose best quality
    scored = []
    for href, label in links:
        res = resolution_priority(href, label)
        scored.append((res, href, label))
    scored.sort(reverse=True, key=lambda x: x[0])
    best_res, best_href, best_label = scored[0]

    # get filesize using HEAD (prefer session.head if cookies required)
    try:
        head = session.head(best_href, allow_redirects=True, timeout=15)
        size = int(head.headers.get("Content-Length") or 0)
    except Exception:
        size = 0
    size_mb = round(size / (1024*1024), 2) if size else "Unknown"

    # check if URL is publicly accessible (no cookie required)
    public_ok, public_size = is_publicly_accessible(best_href)
    public_text = "Yes" if public_ok else "No (requires session)"

    context.user_data['job'] = {
        "page_url": page_url,
        "best_href": best_href,
        "best_res": best_res,
        "size": size,
        "session": session  # keep session for later if needed
    }

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm download & send", callback_data="confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ])

    info = (f"🎬 Found best quality: {best_res if best_res else 'Unknown'}p\n"
            f"📦 Size: {size_mb} MB\n"
            f"🌐 Publicly fetchable by Telegram: {public_text}\n\n"
            "Do you want to download and send this file?")
    await update.message.reply_text(info, reply_markup=kb)
    return AWAIT_CONFIRM

async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    job = context.user_data.get('job')
    if not job:
        await query.edit_message_text("No job found. Please send the URL again.")
        return ConversationHandler.END

    if BUSY.get(user_id):
        await query.edit_message_text("You already have a running task. Please wait.")
        return ConversationHandler.END

    BUSY[user_id] = True
    CANCEL_FLAGS[user_id] = False

    best_href = job['best_href']
    session = job['session']
    chat_id = query.message.chat_id

    # If direct public URL works, send it to Telegram directly (very fast, Telegram fetches it)
    public_ok, _ = is_publicly_accessible(best_href)
    if public_ok:
        await query.edit_message_text("📤 Sending URL to Telegram (Telegram will fetch it directly). This is fastest.")
        try:
            # Telegram will itself fetch the video URL; this is fastest & saves your bandwidth
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
            await context.bot.send_video(chat_id=chat_id, video=best_href)
            await context.bot.send_message(chat_id=chat_id, text="✅ Sent via direct URL.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed to send via URL: {e}\nFalling back to server download.")
            public_ok = False

    if not public_ok:
        # Need to download using session (login cookies) and upload via monitored multipart (so we can display upload progress)
        msg = await context.bot.send_message(chat_id=chat_id, text="⬇️ Starting download...")
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="video_bot_", suffix=".mp4")
        os.close(tmp_fd)

        # progress updater from thread -> edit message
        def dl_progress_cb(downloaded, total, speed, eta):
            if total:
                percent = int(downloaded * 100 / total)
                text = f"⬇️ Downloading... {percent}%\n{downloaded/1024/1024:.2f}/{total/1024/1024:.2f} MB\n🚀 {speed/1024:.1f} KB/s | ⏳ {eta}s"
            else:
                text = f"⬇️ Downloading... {downloaded/1024/1024:.2f} MB\n🚀 {speed/1024:.1f} KB/s"
            try:
                thread_safe_edit(context.bot, chat_id, msg.message_id, text)
            except Exception:
                pass

        # perform download in thread
        success, downloaded, total = await asyncio.to_thread(download_file, session, best_href, tmp_path, user_id, dl_progress_cb)

        if not success:
            await context.bot.send_message(chat_id=chat_id, text="❌ Download cancelled or failed.")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            BUSY[user_id] = False
            CANCEL_FLAGS[user_id] = False
            return ConversationHandler.END

        # Now upload to Telegram with progress monitor
        up_msg = await context.bot.send_message(chat_id=chat_id, text="📤 Uploading to Telegram...")

        def up_progress_cb(sent, total_len, speed, eta):
            # sent and total_len are bytes in multipart; approximate percent
            percent = int(sent * 100 / (total_len + 1))
            text = f"📤 Uploading... {percent}%\n{sent/1024/1024:.2f}/{total_len/1024/1024:.2f} MB (approx)\n🚀 {speed/1024:.1f} KB/s | ⏳ {eta}s"
            try:
                thread_safe_edit(context.bot, chat_id, up_msg.message_id, text)
            except Exception:
                pass

        try:
            # This blocks, so run in a thread
            result = await asyncio.to_thread(upload_file_to_telegram, BOT_TOKEN, chat_id, tmp_path, up_progress_cb)
            await context.bot.edit_message_text(chat_id=chat_id, message_id=up_msg.message_id, text="✅ Upload complete.")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Upload failed: {e}")
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except:
                pass

    BUSY[user_id] = False
    CANCEL_FLAGS[user_id] = False
    return ConversationHandler.END

async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # invoked by inline button 'cancel'
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    CANCEL_FLAGS[user_id] = True
    BUSY[user_id] = False
    await query.edit_message_text("❌ Canceled by user.")
    return ConversationHandler.END

async def manual_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    CANCEL_FLAGS[user_id] = True
    await update.message.reply_text("Cancel requested. Current download/upload will stop shortly.")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_cmd)],
        states={
            AWAITING_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)],
            AWAIT_CONFIRM: [CallbackQueryHandler(confirm_cb, pattern="^confirm$"), CallbackQueryHandler(cancel_cb, pattern="^cancel$")]
        },
        fallbacks=[CommandHandler("cancel", manual_cancel_command)]
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", manual_cancel_command))

    print("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()


