import requests
from bs4 import BeautifulSoup

def download_video(video_page_url, username, password):
    session = requests.Session()

    # STEP 1: Log in
    login_url = "https://www.whoreshub.com/login"  # Update if different
    session.post(login_url, data={
        "username": username,
        "password": password
    })

    # STEP 2: Get the video page
    response = session.get(video_page_url)
    soup = BeautifulSoup(response.content, "html.parser")

    # STEP 3: Find the .mp4 download link
    video_link_tag = soup.find("a", href=lambda href: href and href.endswith(".mp4"))

    if not video_link_tag:
        raise Exception("⚠️ Could not find video download link on the page.")

    video_url = video_link_tag["href"]

    # If link is relative, make it absolute
    if video_url.startswith("/"):
        video_url = f"https://www.whoreshub.com{video_url}"

    # STEP 4: Download the video
    video_response = session.get(video_url, stream=True)
    file_path = "video.mp4"
    with open(file_path, "wb") as f:
        for chunk in video_response.iter_content(8192):
            f.write(chunk)

    return file_path




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
    await update.message.reply_text("Downloading the video. Please wait...")

    try:
        video_path = download_video(url, LOGIN_USERNAME, LOGIN_PASSWORD)
        await context.bot.send_video(chat_id=update.effective_chat.id, video=open(video_path, "rb"))
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

app.run_polling()
