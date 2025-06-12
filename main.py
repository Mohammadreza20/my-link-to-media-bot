import requests
from bs4 import BeautifulSoup

import re
import requests
from bs4 import BeautifulSoup

def extract_resolution(text):
    """Extracts resolution like 2160, 1080 from a string"""
    match = re.search(r"(\d{3,4})p", text)
    return int(match.group(1)) if match else 0

def download_video(video_page_url, username, password):
    session = requests.Session()
    
    # 🟡 Change this to your actual login URL
    login_url = "https://www.whoreshub.com/login/"

    # Login payload — double-check parameter names from browser dev tools
    payload = {
        "username": username,
        "password": password
    }

    # 🔐 Perform login
    login_response = session.post(login_url, data=payload)
    if login_response.status_code != 200:
        raise Exception("Login failed!")

    # 🟢 Fetch video page
    response = session.get(video_page_url)
    soup = BeautifulSoup(response.content, "html.parser")

    # 🧠 Find all download links
    download_links = []
    for a in soup.select("ul.tags-list a"):
        href = a.get("href")
        text = a.get_text(strip=True)

        if "mp4" in text.lower() and "p" in text:
            resolution = extract_resolution(text)
            download_links.append((resolution, href))

    if not download_links:
        raise Exception("No downloadable videos found.")

    # 🎯 Select highest resolution link
    best_link = max(download_links, key=lambda x: x[0])[1]

    # 💾 Download the video
    print(f"Downloading: {best_link}")
    video_data = session.get(best_link, stream=True)
    video_path = "video.mp4"

    with open(video_path, "wb") as f:
        for chunk in video_data.iter_content(chunk_size=8192):
            f.write(chunk)

    return video_path





import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# Load credentials (you'll store them in .env)
LOGIN_USERNAME = os.getenv("LOGIN_USERNAME")
LOGIN_PASSWORD = os.getenv("LOGIN_PASSWORD")
BOT_TOKEN = os.getenv("BOT_TOKEN")

user_sessions = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {}
    await update.message.reply_text("Welcome! Please send the video page URL.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    await update.message.reply_text("🔍 Logging in and fetching video. Please wait...")

    try:
        video_path = download_video(url, LOGIN_USERNAME, LOGIN_PASSWORD)
        await context.bot.send_video(chat_id=update.effective_chat.id, video=open(video_path, "rb"))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")



app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app.run_polling()
