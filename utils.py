# utils.py

import os
import psycopg2
from datetime import datetime, timezone, time
import pytesseract
from PIL import Image
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# =====================================
# Google Sheets Functions (คงไว้เผื่อใช้งาน)
# =====================================
def get_gsheet_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_json_str = os.getenv("GCP_CREDENTIALS_JSON")
    if not creds_json_str:
        print("Error: GCP_CREDENTIALS_JSON environment variable not set.")
        raise ValueError("GCP credentials are not configured.")
    creds_dict = json.loads(creds_json_str)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

# =====================================
# PostgreSQL Functions (เวอร์ชันสมบูรณ์)
# =====================================

DATABASE_URL = os.getenv("DATABASE_URL")

def connect_db():
    if not DATABASE_URL:
        raise ConnectionError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(DATABASE_URL, keepalives_idle=60, keepalives_interval=10, keepalives_count=5)

def create_tables():
    """Initializes all database tables if they don't exist."""
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS chat_history (id SERIAL PRIMARY KEY, user_id TEXT, user_message TEXT, bot_response TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
            cur.execute("CREATE TABLE IF NOT EXISTS session_data (user_id TEXT PRIMARY KEY, context TEXT, last_updated TIMESTAMP);")
            cur.execute("CREATE TABLE IF NOT EXISTS user_profiles (user_id TEXT PRIMARY KEY, profile_data JSONB, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
            cur.execute("CREATE TABLE IF NOT EXISTS reminders (id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, reminder_message TEXT NOT NULL, notify_at TIMESTAMP WITH TIME ZONE NOT NULL, status TEXT DEFAULT 'pending');")
            conn.commit()
            print("All tables created or already exist.")
    except Exception as e:
        print(f"Error creating tables: {e}")
        raise e
    finally:
        conn.close()

# --- ฟังก์ชันจัดการความจำถาวร (User Profile) ---
def get_user_profile(user_id):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT profile_data FROM user_profiles WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else {}

def update_user_profile(user_id, data_to_update):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_profiles (user_id, profile_data, last_updated) VALUES (%s, %s::jsonb, %s)
        ON CONFLICT (user_id) DO UPDATE SET
        profile_data = user_profiles.profile_data || EXCLUDED.profile_data,
        last_updated = EXCLUDED.last_updated;
    """, (user_id, json.dumps(data_to_update), datetime.now(timezone.utc)))
    conn.commit()
    conn.close()

def delete_user_profile_key(user_id, key_to_delete):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("UPDATE user_profiles SET profile_data = profile_data - %s WHERE user_id = %s;", (key_to_delete, user_id))
    conn.commit()
    conn.close()

def clear_pending_action(user_id):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("UPDATE user_profiles SET profile_data = profile_data - 'pending_action' - 'pending_data' WHERE user_id = %s;", (user_id,))
    conn.commit()
    conn.close()

# --- ฟังก์ชันจัดการระบบแจ้งเตือน (Reminders) ---
def create_reminder(user_id, message, notify_at_datetime):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO reminders (user_id, reminder_message, notify_at) VALUES (%s, %s, %s)", (user_id, message, notify_at_datetime))
    conn.commit()
    conn.close()

def get_due_reminders():
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, reminder_message FROM reminders WHERE notify_at <= %s AND status = 'pending'", (datetime.now(timezone.utc),))
    return cur.fetchall()

def delete_reminder(reminder_id):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE id = %s", (reminder_id,))
    conn.commit()
    conn.close()

def get_reminders_for_today(user_id, tz):
    today = datetime.now(tz).date()
    start_of_day = datetime.combine(today, time.min, tzinfo=tz)
    end_of_day = datetime.combine(today, time.max, tzinfo=tz)
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT reminder_message, notify_at FROM reminders WHERE user_id = %s AND notify_at BETWEEN %s AND %s ORDER BY notify_at ASC", (user_id, start_of_day, end_of_day))
    return cur.fetchall()

# --- ฟังก์ชันสำหรับ Dashboard และงานเบื้องหลัง ---
def get_all_user_profiles():
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, profile_data, last_updated FROM user_profiles ORDER BY last_updated DESC")
    return cur.fetchall()

def get_pending_reminders_for_dashboard():
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, reminder_message, notify_at FROM reminders WHERE status = 'pending' ORDER BY notify_at ASC")
    return cur.fetchall()

def get_all_unique_users():
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM chat_history;")
    return [row[0] for row in cur.fetchall()]

# --- ฟังก์ชันจัดการแชทและความจำระยะสั้น (Session) ---
def save_chat(user_id, user_message, bot_response):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO chat_history (user_id, user_message, bot_response, timestamp) VALUES (%s, %s, %s, %s)", (user_id, user_message, bot_response, datetime.now()))
    conn.commit()
    conn.close()

def get_chat_history(limit=100):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM chat_history ORDER BY timestamp DESC LIMIT %s", (limit,))
    return cur.fetchall()

def save_session(user_id, context):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO session_data (user_id, context, last_updated) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET context = EXCLUDED.context, last_updated = EXCLUDED.last_updated", (user_id, context, datetime.now()))
    conn.commit()
    conn.close()

def get_session(user_id):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT context FROM session_data WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def clear_session(user_id):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM session_data WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()

def ocr_image(image_path):
    # ฟังก์ชันนี้ยังคงอยู่ แต่ไม่ได้ถูกเรียกใช้ใน app.py
    try:
        img = Image.open(image_path)
        return pytesseract.image_to_string(img, lang='tha+eng').strip()
    except Exception as e:
        print(f"OCR error: {e}")
        return ""