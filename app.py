# app.py
from flask import Flask, request
import requests
import psycopg2
from db import init_db, get_connection
import config

app = Flask(__name__)

# تهيئة قاعدة البيانات
init_db()

URL = f"https://api.telegram.org/bot{config.BOT_TOKEN}"

def send_message(chat_id, text):
    requests.post(f"{URL}/sendMessage", json={"chat_id": chat_id, "text": text})

@app.route("/")
def home():
    return "Bot is running!"

@app.route(f"/{config.BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json()

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        if str(chat_id) == str(config.ADMIN_ID):
            if text.startswith("/add"):
                # مثال: /add BTC 25000 27000 24000
                parts = text.split()
                if len(parts) == 5:
                    _, symbol, entry, target, stop = parts
                    conn = get_connection()
                    cur = conn.cursor()
                    cur.execute("INSERT INTO signals (symbol, entry_price, target_price, stop_loss) VALUES (%s,%s,%s,%s)",
                                (symbol, float(entry), float(target), float(stop)))
                    conn.commit()
                    cur.close()
                    conn.close()
                    send_message(chat_id, f"✅ Signal for {symbol} added!")
                    # نشر في القناة
                    send_message(config.CHANNEL_ID, f"📢 New Signal:\n{symbol}\nEntry: {entry}\nTarget: {target}\nStop: {stop}")
                else:
                    send_message(chat_id, "❌ Wrong format. Use: /add SYMBOL ENTRY TARGET STOP")

        else:
            send_message(chat_id, "🚫 You are not authorized.")

    return "ok"
