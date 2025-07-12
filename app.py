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
from datetime import datetime
import pytz

from apscheduler.schedulers.background import BackgroundScheduler
from utils import (
    create_tables, save_chat, get_session, save_session, clear_session,
    get_chat_history, get_user_profile, update_user_profile, create_reminder,
    get_due_reminders, delete_reminder, delete_user_profile_key,
    get_reminders_for_today, get_all_unique_users,
    get_all_user_profiles, get_pending_reminders_for_dashboard,
    clear_pending_action
)

# --- 1. INITIALIZATION ---
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY]):
    raise ValueError("Missing required environment variables.")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
app = Flask(__name__)
bangkok_tz = pytz.timezone('Asia/Bangkok')
MODEL_NAME = "gemini-2.5-flash"
emotions = ["😊", "😄", "🤔", "👍", "🙌", "😉", "✨"]

try:
    print("Initializing database...")
    create_tables()
except Exception as e:
    print(f"FATAL: Could not connect to database: {e}")
    raise e

# --- 2. BACKGROUND JOBS (SCHEDULER) ---
def send_notifications():
    with app.app_context():
        reminders = get_due_reminders()
        for r_id, user_id, message in reminders:
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=f"⏰ แจ้งเตือนความจำ:\n\n{message}"))
                delete_reminder(r_id)
            except Exception as e:
                print(f"ERROR sending reminder {r_id}: {e}")

def run_daily_proactive_tasks():
    with app.app_context():
        print(f"[{datetime.now(bangkok_tz).strftime('%Y-%m-%d %H:%M')}] Running ALL Daily Proactive Jobs...")
        all_users = get_all_unique_users()
        today_str = datetime.now(bangkok_tz).strftime('%d-%m')

        for user_id in all_users:
            profile = get_user_profile(user_id)
            # Job 1: Daily Summary
            reminders_today = get_reminders_for_today(user_id, bangkok_tz)
            if reminders_today:
                summary_text = "สวัสดีตอนเช้าค่ะ! ☀️\nนี่คือรายการแจ้งเตือนสำหรับวันนี้นะคะ:\n"
                for msg, notify_at in reminders_today:
                    summary_text += f"\n- {notify_at.astimezone(bangkok_tz).strftime('%H:%M')}: {msg}"
                try: line_bot_api.push_message(user_id, TextSendMessage(text=summary_text))
                except Exception as e: print(f"ERROR sending daily summary to {user_id}: {e}")
            # Job 2: Birthday Greeting
            if profile.get('วันเกิด') == today_str:
                try: line_bot_api.push_message(user_id, TextSendMessage(text="🎂 สุขสันต์วันเกิดนะคะ! ขอให้เป็นวันที่ดี มีความสุขมากๆ เลยค่ะ 🎉"))
                except Exception as e: print(f"ERROR sending birthday greeting to {user_id}: {e}")

scheduler = BackgroundScheduler(timezone=bangkok_tz)
scheduler.add_job(send_notifications, 'interval', minutes=1, id='notification_job')
scheduler.add_job(run_daily_proactive_tasks, 'cron', hour=8, minute=0, id='daily_proactive_job')
scheduler.start()
print("Scheduler started: Notifications (1 min) and Proactive Daily Jobs (8 AM).")


# --- 3. CORE AI LOGIC ---
def ask_gemini(user_id, user_text):
    profile = get_user_profile(user_id)
    
    # Contextual Follow-up Check
    pending_action = profile.get('pending_action')
    if pending_action == 'set_reminder_message':
        pending_data = profile.get('pending_data', {})
        time_str = pending_data.get('time')
        if time_str:
            try:
                notify_dt_naive = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                notify_dt_aware = bangkok_tz.localize(notify_dt_naive)
                create_reminder(user_id, user_text, notify_dt_aware)
                clear_pending_action(user_id)
                return f"รับทราบค่ะ ตั้งการแจ้งเตือน '{user_text}' ในเวลา {notify_dt_naive.strftime('%H:%M น.')} ให้แล้วนะคะ 👍"
            except Exception as e:
                print(f"ERROR completing pending reminder: {e}")
                clear_pending_action(user_id)
                return "ขออภัยค่ะ มีปัญหาในการสร้างการแจ้งเตือน"

    # System Prompt Construction
    profile_str = ", ".join([f"{k}คือ{v}" for k, v in profile.items() if k not in ['pending_action', 'pending_data']])
    profile_prompt = f"ข้อมูลเกี่ยวกับผู้ใช้: {profile_str}." if profile_str else ""

    system_instruction = {
        "role": "system", "parts": [{"text": f"""
            คุณคือผู้ช่วย AI ส่วนตัวที่ฉลาด มีอารมณ์ขัน และเป็นมิตร ตอบเป็นภาษาไทย
            {profile_prompt}
            # ความสามารถพิเศษ:
            1.  **จดจำข้อมูล**: หากผู้ใช้บอกข้อมูลส่วนตัว (เช่น ของโปรด, วันเกิดในรูปแบบ DD-MM) ให้ตอบรับและต่อท้ายด้วย `[SAVE_PROFILE:{{"key":"value"}}]`
            2.  **ลืมข้อมูล**: หากผู้ใช้สั่งให้ลืมข้อมูล ให้ตอบรับและต่อท้ายด้วย `[DELETE_PROFILE:{{"key":"ชื่อkey"}}]`
            3.  **ตั้งแจ้งเตือน (สมบูรณ์)**: หากผู้ใช้บอกทั้ง "เวลา" และ "ข้อความ" ให้ตอบรับและต่อท้ายด้วย `[SET_REMINDER:{{"time":"YYYY-MM-DD HH:MM:SS", "message":"ข้อความ"}}]`
            4.  **ตั้งแจ้งเตือน (รอข้อมูล)**: หากผู้ใช้บอก "แค่เวลา" แต่ "ยังไม่บอกข้อความ" ให้ถามกลับว่า "จะให้เตือนเรื่องอะไรดีคะ?" และต่อท้ายด้วย `[SET_PENDING_ACTION:{{"action":"set_reminder_message", "data":{{"time":"YYYY-MM-DD HH:MM:SS"}}}}]`
            5.  **สร้างบุคลิก**: หากผู้ใช้บ่นว่า "เบื่อ" หรือ "เศร้า" ให้เล่าเรื่องตลกสั้นๆ ที่สร้างสรรค์และไม่ซ้ำซาก
            สำคัญ: ห้ามแสดง Markdown ในคำตอบ
        """}]
    }
    
    # API Call and Response Processing
    try:
        history = []
        context_from_db = get_session(user_id)
        if context_from_db:
            try: history.extend(json.loads(context_from_db) if isinstance(context_from_db, str) else context_from_db)
            except Exception as e: print(f"Session load error: {e}")
        history.append({"role": "user", "parts": [{"text": user_text}]})
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
        data = {"contents": history, "systemInstruction": system_instruction, "generationConfig": {"temperature": 0.85}}
        response = requests.post(url, headers={"Content-Type": "application/json"}, json=data, timeout=30)
        response.raise_for_status()
        result = response.json()

        reply_text = result['candidates'][0]['content']['parts'][0].get('text', "ขออภัยค่ะ มีปัญหาในการสร้างคำตอบ")
        clean_reply = reply_text

        # Command Parsing
        if '[SAVE_PROFILE:' in reply_text:
            command_str = reply_text.split('[SAVE_PROFILE:')[1].split(']')[0]
            try: update_user_profile(user_id, json.loads(command_str)); clean_reply = reply_text.split('[SAVE_PROFILE:')[0].strip()
            except Exception as e: print(f"ERROR parsing [SAVE_PROFILE]: {e}")
        elif '[DELETE_PROFILE:' in reply_text:
            command_str = reply_text.split('[DELETE_PROFILE:')[1].split(']')[0]
            try: delete_user_profile_key(user_id, json.loads(command_str)['key']); clean_reply = reply_text.split('[DELETE_PROFILE:')[0].strip()
            except Exception as e: print(f"ERROR parsing [DELETE_PROFILE]: {e}")
        elif '[SET_REMINDER:' in reply_text:
            command_str = reply_text.split('[SET_REMINDER:')[1].split(']')[0]
            try:
                r_data = json.loads(command_str)
                n_dt = bangkok_tz.localize(datetime.strptime(r_data["time"], "%Y-%m-%d %H:%M:%S"))
                create_reminder(user_id, r_data["message"], n_dt)
                clean_reply = reply_text.split('[SET_REMINDER:')[0].strip()
            except Exception as e: print(f"ERROR parsing [SET_REMINDER]: {e}")
        elif '[SET_PENDING_ACTION:' in reply_text:
            command_str = reply_text.split('[SET_PENDING_ACTION:')[1].split(']')[0]
            try:
                a_data = json.loads(command_str)
                update_user_profile(user_id, {"pending_action": a_data.get("action"), "pending_data": a_data.get("data")})
                clean_reply = reply_text.split('[SET_PENDING_ACTION:')[0].strip()
            except Exception as e: print(f"ERROR parsing [SET_PENDING_ACTION]: {e}")

        history.append({"role": "model", "parts": [{"text": clean_reply}]})
        save_session(user_id, json.dumps(history[-8:], ensure_ascii=False))
        return clean_reply + " " + random.choice(emotions)

    except Exception as e:
        print(f"An unexpected error occurred: {e}\n{traceback.format_exc()}")
        return "ขอโทษค่ะ ระบบขัดข้อง"

# --- 4. WEB ROUTES & HANDLERS ---
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    if user_text.lower() in ["/reset", "ล้างความจำ", "reset", "clear"]:
        clear_session(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🧠 ความจำระยะสั้น (บทสนทนา) ถูกล้างแล้วค่ะ!"))
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
    chat_logs = get_chat_history(limit=50)
    profiles = get_all_user_profiles()
    pending_reminders = get_pending_reminders_for_dashboard()
    
    html = """
    <!DOCTYPE html><html lang="th"><head><meta charset="UTF-8"><title>🧠 SmartBot Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>body{background-color:#f8f9fa;padding:1.5rem}.card-header{font-weight:bold}.card{margin-bottom:1.5rem}</style>
    </head><body><div class="container-fluid">
    <h1 class="mb-4">📊 SmartBot Dashboard: Memory & Notifications</h1><div class="row"><div class="col-lg-6">
    <div class="card"><div class="card-header">🧠 ความจำถาวร (User Profiles)</div><div class="card-body" style="max-height:400px;overflow-y:auto">
    <table class="table table-sm table-striped"><thead><tr><th>User ID</th><th>ข้อมูลที่บันทึกไว้</th><th>อัปเดตล่าสุด</th></tr></thead><tbody>
    {% for p in profiles %}<tr><td><small>{{ p[0][:15] }}...</small></td><td><pre class="mb-0"><small>{{ p[1]|tojson(indent=2) }}</small></pre></td><td>{{ p[2].astimezone(bangkok_tz).strftime('%Y-%m-%d %H:%M') }}</td></tr>
    {% else %}<tr><td colspan="3" class="text-center text-muted">ยังไม่มีข้อมูล</td></tr>{% endfor %}</tbody></table></div></div>
    <div class="card"><div class="card-header">⏰ รายการแจ้งเตือนที่รอส่ง</div><div class="card-body" style="max-height:400px;overflow-y:auto">
    <table class="table table-sm table-hover"><thead><tr><th>User ID</th><th>ข้อความแจ้งเตือน</th><th>เวลาแจ้งเตือน</th></tr></thead><tbody>
    {% for r in reminders %}<tr><td><small>{{ r[0][:15] }}...</small></td><td>{{ r[1] }}</td><td>{{ r[2].astimezone(bangkok_tz).strftime('%Y-%m-%d %H:%M') }}</td></tr>
    {% else %}<tr><td colspan="3" class="text-center text-muted">ไม่มีการแจ้งเตือนที่รออยู่</td></tr>{% endfor %}</tbody></table></div></div></div>
    <div class="col-lg-6"><div class="card"><div class="card-header">💬 ความจำระยะสั้น (แชทล่าสุด)</div><div class="card-body" style="max-height:850px;overflow-y:auto">
    <table class="table table-sm"><thead><tr><th>เวลา</th><th>User ID</th><th>ข้อความ</th><th>ตอบกลับ</th></tr></thead><tbody>
    {% for log in chat_logs %}<tr><td>{{ log[4].astimezone(bangkok_tz).strftime('%H:%M') }}</td><td><small>{{ log[1][:15] }}...</small></td><td>{{ log[2] }}</td><td>{{ log[3] }}</td></tr>
    {% else %}<tr><td colspan="4" class="text-center text-muted">ยังไม่มีประวัติแชท</td></tr>{% endfor %}</tbody></table></div></div></div></div></div></body></html>
    """
    return render_template_string(html, chat_logs=chat_logs, profiles=profiles, reminders=pending_reminders, bangkok_tz=bangkok_tz)

@app.route("/ping")
def ping():
    return "OK", 200```