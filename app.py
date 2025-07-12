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
from datetime import datetime, timedelta
import pytz

# --- เพิ่มการ import ไลบรารีและฟังก์ชันใหม่ ---
from apscheduler.schedulers.background import BackgroundScheduler
from utils import (
    create_tables, save_chat, get_session, save_session, clear_session,
    get_chat_history, get_slip_summary_by_day, get_slip_summary_by_month,
    # --- ฟังก์ชันสำหรับฟีเจอร์ใหม่ ---
    get_user_profile, update_user_profile, create_reminder,
    get_due_reminders, delete_reminder
)

# โหลดค่าจากไฟล์ .env
load_dotenv()

# --- ดึงค่าจาก Environment Variables ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY]):
    raise ValueError("Missing required environment variables (LINE, GEMINI).")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
app = Flask(__name__)

# --- กำหนด Timezone ---
bangkok_tz = pytz.timezone('Asia/Bangkok')

# --- เริ่มต้นฐานข้อมูล ---
try:
    print("Initializing database...")
    create_tables()
except Exception as e:
    print(f"FATAL: Could not connect to database and create tables: {e}")
    raise e

# =======================================================
# START: ระบบแจ้งเตือนเบื้องหลัง (Background Scheduler)
# =======================================================
def send_notifications():
    """
    ฟังก์ชันนี้จะถูกเรียกโดย Scheduler เพื่อตรวจสอบและส่งการแจ้งเตือน
    """
    with app.app_context(): # จำเป็นเพื่อให้เข้าถึง app context ภายนอก request
        print(f"[{datetime.now(bangkok_tz).strftime('%Y-%m-%d %H:%M:%S')}] Running notification job...")
        reminders = get_due_reminders()
        if not reminders:
            return

        print(f"Found {len(reminders)} due reminders to send.")
        for r_id, user_id, message in reminders:
            try:
                reminder_text = f"⏰ แจ้งเตือนความจำ:\n\n{message}"
                line_bot_api.push_message(
                    user_id,
                    TextSendMessage(text=reminder_text)
                )
                print(f"Successfully sent reminder ID {r_id} to user {user_id}")
                # เมื่อส่งสำเร็จ ให้ลบออกจากฐานข้อมูล
                delete_reminder(r_id)
            except Exception as e:
                print(f"ERROR: Could not send reminder ID {r_id} to user {user_id}. Reason: {e}")
                # อาจจะมีการอัปเดต status เป็น 'error' แทนการลบก็ได้

# --- ตั้งค่าและเริ่ม Scheduler ---
scheduler = BackgroundScheduler(timezone=bangkok_tz)
scheduler.add_job(send_notifications, 'interval', minutes=1, id='notification_job')
scheduler.start()
print("Scheduler started successfully. Checking for reminders every minute.")
# =======================================================
# END: ระบบแจ้งเตือนเบื้องหลัง
# =======================================================


emotions = ["😊", "😄", "🤔", "👍", "🙌", "😉", "✨"]
MODEL_NAME = "gemini-1.5-flash"

def ask_gemini(user_id, user_text):
    # --- 1. ดึงความจำถาวร (Profile) ---
    profile = get_user_profile(user_id)
    profile_prompt = ""
    if profile:
        # แปลง dict เป็นสตริงที่ AI อ่านเข้าใจง่าย
        profile_str = ", ".join([f"{key}คือ{value}" for key, value in profile.items()])
        profile_prompt = f"ข้อมูลเกี่ยวกับผู้ใช้คนนี้ที่คุณเคยบันทึกไว้: {profile_str}. ใช้ข้อมูลนี้เพื่อทำให้การสนทนาเป็นกันเองมากขึ้น"

    # --- 2. สร้าง Prompt อัจฉริยะสำหรับ AI ---
    system_instruction = {
        "role": "system",
        "parts": [{"text": f"""
            คุณคือผู้ช่วย AI ส่วนตัวที่ฉลาดและเป็นมิตร ตอบเป็นภาษาไทยเสมอ
            {profile_prompt}

            # ความสามารถพิเศษของคุณ:
            1.  **การจดจำข้อมูลส่วนตัว**: หากผู้ใช้บอกข้อมูลเกี่ยวกับตัวเอง (เช่น ชื่อเล่น, ของที่ชอบ, วันเกิด) ให้คุณตอบกลับบทสนทนาอย่างเป็นธรรมชาติ และ "ต้อง" ต่อท้ายคำตอบด้วยคำสั่งพิเศษในรูปแบบนี้เท่านั้น: `[SAVE_PROFILE:{{"key":"value"}}]`
                - ตัวอย่าง: ผู้ใช้บอก "ฉันชอบกินข้าวมันไก่" คุณต้องตอบประมาณว่า "ข้าวมันไก่อร่อยจริงๆ ค่ะ เดี๋ยวบันทึกไว้ให้นะคะ [SAVE_PROFILE:{{"เมนูโปรด":"ข้าวมันไก่"}}]`

            2.  **การตั้งค่าการแจ้งเตือน**: หากผู้ใช้ต้องการให้ "เตือน" หรือ "แจ้งเตือน" เกี่ยวกับอะไรบางอย่าง ให้คุณแปลงเวลาที่ผู้ใช้บอก (เช่น พรุ่งนี้, 5 โมงเย็น, วันศุกร์) ให้เป็นวันเวลาที่ชัดเจนในอนาคตเสมอ (รูปแบบ YYYY-MM-DD HH:MM:SS) และ "ต้อง" ต่อท้ายคำตอบด้วยคำสั่งพิเศษนี้: `[SET_REMINDER:{{"time":"YYYY-MM-DD HH:MM:SS", "message":"ข้อความแจ้งเตือน"}}]`
                - ตัวอย่าง: ผู้ใช้บอก "เตือนฉันตอน 6 โมงเย็นให้ไปออกกำลังกาย" และสมมติว่าวันนี้คือ 2025-07-12 คุณต้องตอบประมาณว่า "ได้เลยค่ะ 6 โมงเย็นจะเตือนให้นะคะ [SET_REMINDER:{{"time":"2025-07-12 18:00:00", "message":"ไปออกกำลังกาย"}}]`
                - ถ้าผู้ใช้บอก "พรุ่งนี้ 9 โมงเช้า" คุณต้องคำนวณวันที่ของวันพรุ่งนี้ให้ถูกต้อง

            สำคัญมาก: ห้ามแสดง Markdown ในคำตอบเด็ดขาด และห้ามแสดงส่วนของคำสั่งพิเศษให้ผู้ใช้เห็นในแชทปกติ
        """}]
    }

    # --- 3. ดึงความจำระยะสั้น (ประวัติแชท) ---
    history = []
    context_from_db = get_session(user_id)
    if context_from_db:
        try:
            saved_msgs = json.loads(context_from_db) if isinstance(context_from_db, str) else context_from_db
            for msg in saved_msgs:
                if isinstance(msg, dict) and "role" in msg and "parts" in msg:
                    history.append(msg)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Session load error for user {user_id}: {e}. Starting new session.")
    
    history.append({"role": "user", "parts": [{"text": user_text}]})

    # --- 4. เรียก Gemini API ---
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    data = {"contents": history, "systemInstruction": system_instruction, "generationConfig": {"temperature": 0.8, "maxOutputTokens": 2048}}

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
        response.raise_for_status()
        result = response.json()

        reply_text = "ขออภัยค่ะ มีปัญหาในการสร้างคำตอบ"
        if 'candidates' in result and result['candidates'] and 'content' in result['candidates'][0] and 'parts' in result['candidates'][0]['content']:
            reply_text = result['candidates'][0]['content']['parts'][0].get('text', reply_text)

        # --- 5. ประมวลผลคำสั่งพิเศษจาก AI ---
        clean_reply = reply_text

        # ตรวจจับและประมวลผล [SAVE_PROFILE]
        if '[SAVE_PROFILE:' in reply_text:
            command_str = reply_text.split('[SAVE_PROFILE:')[1].split(']')[0]
            try:
                data_to_save = json.loads(command_str)
                update_user_profile(user_id, data_to_save)
                print(f"SUCCESS: Profile updated for {user_id} with {data_to_save}")
                clean_reply = reply_text.split('[SAVE_PROFILE:')[0].strip()
            except Exception as e:
                print(f"ERROR: Could not parse [SAVE_PROFILE] command. Data: {command_str}, Error: {e}")

        # ตรวจจับและประมวลผล [SET_REMINDER]
        if '[SET_REMINDER:' in reply_text:
            command_str = reply_text.split('[SET_REMINDER:')[1].split(']')[0]
            try:
                reminder_data = json.loads(command_str)
                notify_dt_naive = datetime.strptime(reminder_data["time"], "%Y-%m-%d %H:%M:%S")
                notify_dt_aware = bangkok_tz.localize(notify_dt_naive)
                create_reminder(user_id, reminder_data["message"], notify_dt_aware)
                print(f"SUCCESS: Reminder set for {user_id} at {notify_dt_aware}")
                clean_reply = reply_text.split('[SET_REMINDER:')[0].strip()
            except Exception as e:
                print(f"ERROR: Could not parse [SET_REMINDER] command. Data: {command_str}, Error: {e}")

        # --- 6. บันทึกและส่งคำตอบ ---
        history.append({"role": "model", "parts": [{"text": clean_reply}]})
        save_session(user_id, json.dumps(history[-8:], ensure_ascii=False))
        
        return clean_reply + " " + random.choice(emotions)

    except requests.exceptions.RequestException as req_err:
        print(f"Request error occurred: {req_err}\n{traceback.format_exc()}")
        return "ขออภัยค่ะ มีปัญหาในการเชื่อมต่อกับ AI ลองใหม่อีกครั้งนะคะ"
    except Exception as e:
        print(f"An unexpected error occurred in ask_gemini: {e}\n{traceback.format_exc()}")
        return "ขอโทษค่ะ ระบบขัดข้อง ไม่สามารถตอบกลับได้ในขณะนี้"

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    if user_text.lower() in ["/reset", "ล้างความจำ", "reset", "clear"]:
        clear_session(user_id) # ตอนนี้จะล้างแค่ session chat
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="🧠 ความจำระยะสั้น (บทสนทนา) ถูกล้างแล้วค่ะ เริ่มคุยใหม่ได้เลย!")
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
    return "OK"

@app.route("/")
def dashboard():
    # โค้ดส่วน Dashboard เหมือนเดิม ไม่มีการเปลี่ยนแปลง
    chat_logs = get_chat_history(limit=50)
    daily = get_slip_summary_by_day()
    monthly = get_slip_summary_by_month()
    html = """
    <html>
    <head><title>💬 SmartBot Dashboard</title>
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
    </body></html>
    """
    return render_template_string(html, chat_logs=chat_logs, daily=daily, monthly=monthly)

# --- เพิ่ม Endpoint สำหรับ Keep-alive ---
@app.route("/ping")
def ping():
    """Endpoint สำหรับให้ Cron Job เรียกเพื่อปลุกแอป"""
    print("Ping received, keeping app alive.")
    return "OK", 200

# หมายเหตุ: ไม่ต้องมี if __name__ == "__main__": อีกต่อไปเมื่อ deploy บน gunicorn