from flask import Flask, request, render_template_string
from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage
    # ไม่ได้ใช้ TemplateSendMessage, ButtonsTemplate, MessageAction แล้ว จึงลบออกได้ (หรือเก็บไว้ก็ได้)
)
from dotenv import load_dotenv
import os
import random
import json
import requests
import traceback

# สมมติว่าไฟล์ utils.py ของคุณทำงานได้ถูกต้อง
from utils import (
    create_tables, save_chat, get_session, save_session, clear_session,
    get_chat_history, get_slip_summary_by_day, get_slip_summary_by_month
)

# โหลดค่าจากไฟล์ .env
load_dotenv()

# --- ดึงค่าจาก Environment Variables ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- ตรวจสอบว่าได้ตั้งค่า Key ครบถ้วนหรือไม่ ---
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY]):
    raise ValueError("กรุณาตั้งค่า LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, และ GEMINI_API_KEY ในไฟล์ .env ให้ครบถ้วน")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
app = Flask(__name__)

emotions = ["😊", "😄", "🤔", "👍", "🙌", "😉", "✨"]

MODEL_NAME = "gemini-2.5-flash"

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
                if isinstance(msg, dict):
                    if "content" in msg and "parts" not in msg:
                        history.append({
                            "role": msg.get("role", "user"),
                            "parts": [{"text": msg.get("content", "")}]
                        })
                    elif "parts" in msg:
                        history.append(msg)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Session load error for user {user_id}: {e}. Starting new session.")

    history.append({"role": "user", "parts": [{"text": user_text}]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    
    headers = {
        "Content-Type": "application/json"
    }

    data = {
        "contents": history,
        "systemInstruction": system_instruction,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048, # ตั้งค่าให้สูงพอสำหรับโมเดลในการคิดและตอบ
            "topP": 0.95,
            "topK": 40
        }
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()

        reply_text = "ขออภัยค่ะ ไม่สามารถสร้างคำตอบที่สมบูรณ์ได้ในขณะนี้" 

        if 'candidates' in result and result['candidates']:
            first_candidate = result['candidates'][0]
            
            if 'finishReason' in first_candidate and first_candidate['finishReason'] == 'SAFETY':
                reply_text = "ขออภัยค่ะ คำตอบถูกกรองด้วยเหตุผลด้านความปลอดภัยของเนื้อหา"
            elif 'content' in first_candidate and 'parts' in first_candidate['content'] and first_candidate['content']['parts']:
                if 'text' in first_candidate['content']['parts'][0]:
                    reply_text = first_candidate['content']['parts'][0]['text']

        history.append({"role": "model", "parts": [{"text": reply_text}]})
        save_session(user_id, json.dumps(history[-8:], ensure_ascii=False)) 
        
        return reply_text + " " + random.choice(emotions)

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred: {http_err}")
        print(f"Response body: {response.text}")
        return "ขออภัยค่ะ มีปัญหาในการสื่อสารกับ AI ลองใหม่อีกครั้งนะคะ"
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

    # 1. รับคำตอบจาก Gemini
    reply_text = ask_gemini(user_id, user_text)
    
    # 2. บันทึกการสนทนา
    save_chat(user_id, user_text, reply_text)

    # 3. สร้างข้อความตอบกลับแบบธรรมดา (TextSendMessage)
    reply_message = TextSendMessage(text=reply_text)

    # 4. ส่งข้อความกลับไปหาผู้ใช้
    line_bot_api.reply_message(
        event.reply_token,
        reply_message
    )


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Handler error: {e}")
        app.logger.error(f"Error handling webhook: {e}")
        return "Bad Request", 400
    return "OK", 200

# ส่วน Dashboard ไม่มีการแก้ไข
@app.route("/")
def dashboard():
    chat_logs = get_chat_history(limit=50)
    daily = get_slip_summary_by_day()
    monthly = get_slip_summary_by_month()
    
    html = """
    <html>
    <head>
        <title>💬 SmartBot Dashboard</title>
        <style>
            body { font-family: sans-serif; margin: 30px; }
            h2 { color: #333; }
            table { width: 100%; border-collapse: collapse; margin-bottom: 30px; }
            th, td { border: 1px solid #aaa; padding: 8px; text-align: left; word-break: break-all; }
            th { background-color: #eee; }
        </style>
    </head>
    <body>
        <h1>📊 LINE SmartBot Dashboard</h1>
        <h2>📆 สรุปยอดรายวัน</h2>
        <table>
            <tr><th>วันที่</th><th>ยอดรวม</th></tr>
            {% for row in daily.items() %}
            <tr><td>{{row[0]}}</td><td>{{row[1]}}</td></tr>
            {% endfor %}
        </table>
        <h2>🗓️ สรุปยอดรายเดือน</h2>
        <table>
            <tr><th>เดือน</th><th>ยอดรวม</th></tr>
            {% for row in monthly.items() %}
            <tr><td>{{row[0]}}</td><td>{{row[1]}}</td></tr>
            {% endfor %}
        </table>
        <h2>💬 แชทล่าสุด</h2>
        <table>
            <tr><th>เวลา</th><th>User</th><th>ข้อความ</th><th>ตอบกลับ</th></tr>
            {% for log in chat_logs %}
            <tr><td>{{log[4]}}</td><td>{{log[1]}}</td><td>{{log[2]}}</td><td>{{log[3]}}</td></tr>
            {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(html, chat_logs=chat_logs, daily=daily, monthly=monthly)


def initialize():
    create_tables()

if __name__ == "__main__":
    create_tables()
    port = int(os.environ.get("PORT", 7000))
    app.run(debug=True, host="0.0.0.0", port=port)