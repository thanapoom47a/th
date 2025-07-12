# app.py

from flask import Flask, request, render_template_string
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
import os
import random
import json
import requests
import traceback

# เปลี่ยนการ import จาก utils_postgres มาเป็น utils
from utils import (
    create_tables, save_chat, get_session, save_session, clear_session,
    get_chat_history, get_slip_summary_by_day, get_slip_summary_by_month
)

# โหลดค่าจากไฟล์ .env (มีประโยชน์ตอนทดสอบบนเครื่อง)
load_dotenv()

# --- ดึงค่าจาก Environment Variables ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- ตรวจสอบว่าได้ตั้งค่า Key ครบถ้วนหรือไม่ ---
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY]):
    raise ValueError("Missing required environment variables (LINE, GEMINI).")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
app = Flask(__name__)

# --- START MODIFICATION ---
# ย้าย create_tables() มาไว้ที่นี่
# เมื่อ Render.com รันแอปพลิเคชันของคุณ, โค้ดส่วนนี้จะทำงานหนึ่งครั้ง
# เพื่อให้แน่ใจว่าตารางในฐานข้อมูลพร้อมใช้งาน
try:
    print("Initializing database...")
    create_tables()
except Exception as e:
    # หากไม่สามารถเชื่อมต่อ DB ได้ ให้แอปหยุดทำงานไปเลย
    print(f"FATAL: Could not connect to database and create tables: {e}")
    # ในสภาพแวดล้อม Production จริง อาจจะต้องมี logic ที่ดีกว่านี้
    # แต่สำหรับตอนนี้ การหยุดทำงานไปเลยจะช่วยให้เห็นปัญหาได้ชัดเจน
    raise e
# --- END MODIFICATION ---

emotions = ["😊", "😄", "🤔", "👍", "🙌", "😉", "✨"]
MODEL_NAME = "gemini-1.5-flash" # แก้ไขเป็น 1.5-flash ตามที่แนะนำ

def ask_gemini(user_id, user_text):
    system_instruction = {
        "role": "system",
        "parts": [{"text": "ตอบกลับเป็นภาษาไทยด้วยน้ำเสียงเป็นกันเอง, เข้าใจง่าย, และไม่ใช้ Markdown"}]
    }
    
    history = []
    context_json = get_session(user_id)
    
    if context_json:
        try:
            saved_msgs = json.loads(context_json)
            for msg in saved_msgs:
                # เพิ่มความเสถียรในการโหลด context
                if isinstance(msg, dict) and "role" in msg and "parts" in msg:
                    history.append(msg)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Session load error for user {user_id}: {e}. Starting new session.")

    history.append({"role": "user", "parts": [{"text": user_text}]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    data = {
        "contents": history,
        "systemInstruction": system_instruction,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048,
            "topP": 0.95,
            "topK": 40
        }
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30) # เพิ่ม timeout
        response.raise_for_status()
        result = response.json()

        reply_text = "ขออภัยค่ะ ไม่สามารถสร้างคำตอบที่สมบูรณ์ได้ในขณะนี้" 
        
        # ปรับปรุงการดึงคำตอบให้ปลอดภัยขึ้น
        if 'candidates' in result and result['candidates']:
            candidate = result['candidates'][0]
            if candidate.get('finishReason') == 'SAFETY':
                reply_text = "ขออภัยค่ะ คำตอบถูกกรองด้วยเหตุผลด้านความปลอดภัยของเนื้อหา"
            elif 'content' in candidate and 'parts' in candidate['content'] and candidate['content']['parts']:
                reply_text = candidate['content']['parts'][0].get('text', reply_text)

        history.append({"role": "model", "parts": [{"text": reply_text}]})
        save_session(user_id, json.dumps(history[-8:], ensure_ascii=False)) 
        
        return reply_text + " " + random.choice(emotions)

    except requests.exceptions.RequestException as req_err:
        print(f"Request error occurred: {req_err}")
        traceback.print_exc()
        return "ขออภัยค่ะ มีปัญหาในการเชื่อมต่อกับ AI ลองใหม่อีกครั้งนะคะ"
    except Exception as e:
        print(f"An unexpected error occurred in ask_gemini: {type(e).__name__}: {e}")
        traceback.print_exc()
        return "ขอโทษค่ะ ระบบขัดข้อง ไม่สามารถตอบกลับได้ในขณะนี้"

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    if user_text.lower() in ["/reset", "ล้างความจำ", "reset", "clear"]:
        clear_session(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="🧠 ล้างความจำของ AI สำเร็จแล้ว! เริ่มคุยใหม่ได้เลยครับ")
        )
        return

    reply_text = ask_gemini(user_id, user_text)
    save_chat(user_id, user_text, reply_text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        app.logger.error(f"Error handling webhook: {e}")
        return "Bad Request", 400
    return "OK", 200

@app.route("/")
def dashboard():
    chat_logs = get_chat_history(limit=50)
    daily = get_slip_summary_by_day()
    monthly = get_slip_summary_by_month()
    
    html = """
    <html>
    <head>
        <title>💬 SmartBot Dashboard</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style> body { padding: 2rem; } </style>
    </head>
    <body>
        <h1 class="mb-4">📊 LINE SmartBot Dashboard</h1>
        
        <h2 class="mt-5">📆 สรุปยอดรายวัน</h2>
        <table class="table table-striped table-hover">
            <thead><tr><th>วันที่</th><th>ยอดรวม</th></tr></thead>
            <tbody>
                {% for date, total in daily.items() %}
                <tr><td>{{ date }}</td><td>{{ "%.2f"|format(total) }}</td></tr>
                {% endfor %}
            </tbody>
        </table>

        <h2 class="mt-5">🗓️ สรุปยอดรายเดือน</h2>
        <table class="table table-striped table-hover">
            <thead><tr><th>เดือน</th><th>ยอดรวม</th></tr></thead>
            <tbody>
                {% for month, total in monthly.items() %}
                <tr><td>{{ month }}</td><td>{{ "%.2f"|format(total) }}</td></tr>
                {% endfor %}
            </tbody>
        </table>

        <h2 class="mt-5">💬 แชทล่าสุด (50 ข้อความ)</h2>
        <table class="table table-sm">
            <thead><tr><th>เวลา</th><th>User ID</th><th>ข้อความ</th><th>ตอบกลับ</th></tr></thead>
            <tbody>
            {% for log in chat_logs %}
            <tr><td>{{ log[4].strftime('%Y-%m-%d %H:%M') }}</td><td>{{ log[1] }}</td><td>{{ log[2] }}</td><td>{{ log[3] }}</td></tr>
            {% endfor %}
            </tbody>
        </table>
    </body>
    </html>
    """
    # แก้ไขการวนลูปใน template ให้ถูกต้องตาม dictionary ที่ได้จาก utils.py
    return render_template_string(html, chat_logs=chat_logs, daily=daily, monthly=monthly)

# --- START MODIFICATION ---
# ลบ if __name__ == "__main__" block ทั้งหมด
# --- END MODIFICATION ---

# เขียนไฟล์ app.py ที่แก้ไขแล้ว
with open('app.py', 'w', encoding='utf-8') as f:
    f.write(
'''
from flask import Flask, request, render_template_string
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
import os
import random
import json
import requests
import traceback

from utils import (
    create_tables, save_chat, get_session, save_session, clear_session,
    get_chat_history, get_slip_summary_by_day, get_slip_summary_by_month
)

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY]):
    raise ValueError("Missing required environment variables (LINE, GEMINI).")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
app = Flask(__name__)

try:
    print("Initializing database...")
    create_tables()
except Exception as e:
    print(f"FATAL: Could not connect to database and create tables: {e}")
    raise e

emotions = ["😊", "😄", "🤔", "👍", "🙌", "😉", "✨"]
MODEL_NAME = "gemini-1.5-flash"

def ask_gemini(user_id, user_text):
    system_instruction = {
        "role": "system",
        "parts": [{"text": "ตอบกลับเป็นภาษาไทยด้วยน้ำเสียงเป็นกันเอง, เข้าใจง่าย, และไม่ใช้ Markdown"}]
    }
    history = []
    context_json = get_session(user_id)
    if context_json:
        try:
            saved_msgs = json.loads(context_json)
            for msg in saved_msgs:
                if isinstance(msg, dict) and "role" in msg and "parts" in msg:
                    history.append(msg)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Session load error for user {user_id}: {e}. Starting new session.")
    history.append({"role": "user", "parts": [{"text": user_text}]})
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    data = {
        "contents": history,
        "systemInstruction": system_instruction,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048,
            "topP": 0.95,
            "topK": 40
        }
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        result = response.json()
        reply_text = "ขออภัยค่ะ ไม่สามารถสร้างคำตอบที่สมบูรณ์ได้ในขณะนี้"
        if 'candidates' in result and result['candidates']:
            candidate = result['candidates'][0]
            if candidate.get('finishReason') == 'SAFETY':
                reply_text = "ขออภัยค่ะ คำตอบถูกกรองด้วยเหตุผลด้านความปลอดภัยของเนื้อหา"
            elif 'content' in candidate and 'parts' in candidate['content'] and candidate['content']['parts']:
                reply_text = candidate['content']['parts'][0].get('text', reply_text)
        history.append({"role": "model", "parts": [{"text": reply_text}]})
        save_session(user_id, json.dumps(history[-8:], ensure_ascii=False))
        return reply_text + " " + random.choice(emotions)
    except requests.exceptions.RequestException as req_err:
        print(f"Request error occurred: {req_err}")
        traceback.print_exc()
        return "ขออภัยค่ะ มีปัญหาในการเชื่อมต่อกับ AI ลองใหม่อีกครั้งนะคะ"
    except Exception as e:
        print(f"An unexpected error occurred in ask_gemini: {type(e).__name__}: {e}")
        traceback.print_exc()
        return "ขอโทษค่ะ ระบบขัดข้อง ไม่สามารถตอบกลับได้ในขณะนี้"

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()
    if user_text.lower() in ["/reset", "ล้างความจำ", "reset", "clear"]:
        clear_session(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="🧠 ล้างความจำของ AI สำเร็จแล้ว! เริ่มคุยใหม่ได้เลยครับ")
        )
        return
    reply_text = ask_gemini(user_id, user_text)
    save_chat(user_id, user_text, reply_text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        app.logger.error(f"Error handling webhook: {e}")
        return "Bad Request", 400
    return "OK", 200

@app.route("/")
def dashboard():
    chat_logs = get_chat_history(limit=50)
    daily = get_slip_summary_by_day()
    monthly = get_slip_summary_by_month()
    html = """
    <html>
    <head>
        <title>💬 SmartBot Dashboard</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style> body { padding: 2rem; } </style>
    </head>
    <body>
        <h1 class="mb-4">📊 LINE SmartBot Dashboard</h1>
        <h2 class="mt-5">📆 สรุปยอดรายวัน</h2>
        <table class="table table-striped table-hover">
            <thead><tr><th>วันที่</th><th>ยอดรวม</th></tr></thead>
            <tbody>
                {% for date, total in daily.items() %}
                <tr><td>{{ date }}</td><td>{{ "%.2f"|format(total) }}</td></tr>
                {% endfor %}
            </tbody>
        </table>
        <h2 class="mt-5">🗓️ สรุปยอดรายเดือน</h2>
        <table class="table table-striped table-hover">
            <thead><tr><th>เดือน</th><th>ยอดรวม</th></tr></thead>
            <tbody>
                {% for month, total in monthly.items() %}
                <tr><td>{{ month }}</td><td>{{ "%.2f"|format(total) }}</td></tr>
                {% endfor %}
            </tbody>
        </table>
        <h2 class="mt-5">💬 แชทล่าสุด (50 ข้อความ)</h2>
        <table class="table table-sm">
            <thead><tr><th>เวลา</th><th>User ID</th><th>ข้อความ</th><th>ตอบกลับ</th></tr></thead>
            <tbody>
            {% for log in chat_logs %}
            <tr><td>{{ log[4].strftime('%Y-%m-%d %H:%M') }}</td><td>{{ log[1] }}</td><td>{{ log[2] }}</td><td>{{ log[3] }}</td></tr>
            {% endfor %}
            </tbody>
        </table>
    </body>
    </html>
    """
    return render_template_string(html, chat_logs=chat_logs, daily=daily, monthly=monthly)
''')
print("File 'app.py' has been updated.")
