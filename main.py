import requests
from bs4 import BeautifulSoup

def download_video(video_page_url, username, password):
    session = requests.Session()
    login_url = "https://example.com/login"  # CHANGE THIS

    # Login
    session.post(login_url, data={
        "username": username,
        "password": password
    })

    # Get video page
    page = session.get(video_page_url)
    soup = BeautifulSoup(page.content, "html.parser")

    # Find video src (adjust selector as needed)
    video_url = soup.find("video")["src"]

    # Download video
    video_data = session.get(video_url, stream=True)
    with open("video.mp4", "wb") as f:
        for chunk in video_data.iter_content(8192):
            f.write(chunk)

    return "video.mp4"



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
