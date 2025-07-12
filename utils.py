# utils.py

import os
import psycopg2
from datetime import datetime, timezone
import pytesseract
from PIL import Image
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# =====================================
# Google Sheets Functions (คงเดิม)
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

def get_slip_summary_by_day():
    try:
        client = get_gsheet_client()
        sheet = client.open("slip_records").sheet1
        records = sheet.get_all_records()
        summary = {}
        for r in records:
            if 'timestamp' in r and r['timestamp']:
                date = r['timestamp'].split(' ')[0]
                amount = float(r['amount']) if r.get('amount') else 0
                summary.setdefault(date, 0)
                summary[date] += amount
        return dict(sorted(summary.items(), key=lambda item: item[0], reverse=True))
    except Exception as e:
        print(f"Error in get_slip_summary_by_day: {e}")
        return {}

def get_slip_summary_by_month():
    try:
        client = get_gsheet_client()
        sheet = client.open("slip_records").sheet1
        records = sheet.get_all_records()
        summary = {}
        for r in records:
            if 'timestamp' in r and r['timestamp']:
                month = r['timestamp'][:7]
                amount = float(r['amount']) if r.get('amount') else 0
                summary.setdefault(month, 0)
                summary[month] += amount
        return dict(sorted(summary.items(), key=lambda item: item[0], reverse=True))
    except Exception as e:
        print(f"Error in get_slip_summary_by_month: {e}")
        return {}

def append_slip_to_gsheet(user_id, amount, direction, party, source_text, channel=None, transaction_id=None):
    try:
        client = get_gsheet_client()
        sheet = client.open("slip_records").sheet1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([
            timestamp, user_id, str(amount), direction, party,
            channel or "", transaction_id or "", source_text
        ])
    except Exception as e:
        print(f"Google Sheets Error: {e}")

# =====================================
# PostgreSQL Functions (อัปเกรด)
# =====================================

DATABASE_URL = os.getenv("DATABASE_URL")

def connect_db():
    if not DATABASE_URL:
        raise ConnectionError("DATABASE_URL environment variable is not set.")
    # เพิ่ม keepalives เพื่อรักษาการเชื่อมต่อให้นานขึ้นบน Render
    return psycopg2.connect(DATABASE_URL, keepalives_idle=60, keepalives_interval=10, keepalives_count=5)

def create_tables():
    """Initializes all database tables if they don't exist."""
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            # ตารางแชท (เหมือนเดิม)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    user_message TEXT,
                    bot_response TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # ตารางความจำระยะสั้น (เหมือนเดิม)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS session_data (
                    user_id TEXT PRIMARY KEY,
                    context TEXT,
                    last_updated TIMESTAMP
                );
            """)
            # --- ตารางใหม่: ความจำถาวร ---
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    profile_data JSONB,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # --- ตารางใหม่: ระบบแจ้งเตือน ---
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    reminder_message TEXT NOT NULL,
                    notify_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    status TEXT DEFAULT 'pending' -- pending, sent, error
                );
            """)
            conn.commit()
            print("All tables created or already exist.")
    except Exception as e:
        print(f"Error creating tables: {e}")
        raise e
    finally:
        conn.close()

# --- START: ฟังก์ชันจัดการความจำถาวร (User Profile) ---
def get_user_profile(user_id):
    """ดึงข้อมูลโปรไฟล์ถาวรของผู้ใช้"""
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT profile_data FROM user_profiles WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else {}

def update_user_profile(user_id, data_to_update):
    """อัปเดตหรือผสานข้อมูลใหม่เข้ากับโปรไฟล์เดิม"""
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_profiles (user_id, profile_data, last_updated)
        VALUES (%s, %s::jsonb, %s)
        ON CONFLICT (user_id) DO UPDATE SET
        profile_data = user_profiles.profile_data || EXCLUDED.profile_data,
        last_updated = EXCLUDED.last_updated;
    """, (user_id, json.dumps(data_to_update), datetime.now(timezone.utc)))
    conn.commit()
    conn.close()
# --- END: ฟังก์ชันจัดการความจำถาวร ---

# --- START: ฟังก์ชันจัดการระบบแจ้งเตือน (Reminders) ---
def create_reminder(user_id, message, notify_at_datetime):
    """บันทึกการแจ้งเตือนใหม่ลงฐานข้อมูล"""
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reminders (user_id, reminder_message, notify_at)
        VALUES (%s, %s, %s)
    """, (user_id, message, notify_at_datetime))
    conn.commit()
    conn.close()

def get_due_reminders():
    """ดึงรายการแจ้งเตือนทั้งหมดที่ถึงกำหนดส่ง"""
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, user_id, reminder_message FROM reminders
        WHERE notify_at <= %s AND status = 'pending'
    """, (datetime.now(timezone.utc),))
    reminders = cur.fetchall()
    return reminders

def delete_reminder(reminder_id):
    """ลบการแจ้งเตือนออกจากฐานข้อมูล (หลังจากส่งแล้ว)"""
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE id = %s", (reminder_id,))
    conn.commit()
    conn.close()
# --- END: ฟังก์ชันจัดการระบบแจ้งเตือน ---


# --- ฟังก์ชันจัดการแชทและความจำระยะสั้น (คงเดิม) ---
def save_chat(user_id, user_message, bot_response):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO chat_history (user_id, user_message, bot_response, timestamp)
        VALUES (%s, %s, %s, %s)
    """, (user_id, user_message, bot_response, datetime.now()))
    conn.commit()
    conn.close()

def get_chat_history(limit=100):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM chat_history ORDER BY timestamp DESC LIMIT %s", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def save_session(user_id, context):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO session_data (user_id, context, last_updated)
        VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET
        context = EXCLUDED.context, last_updated = EXCLUDED.last_updated
    """, (user_id, context, datetime.now()))
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
    # เพิ่มการลบโปรไฟล์ถาวรด้วย (อาจจะแยกเป็นอีกคำสั่งก็ได้)
    cur.execute("DELETE FROM session_data WHERE user_id = %s", (user_id,))
    # cur.execute("DELETE FROM user_profiles WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()

# --- OCR and Text Cleaning (คงเดิม) ---
def ocr_image(image_path):
    try:
        img = Image.open(image_path)
        return pytesseract.image_to_string(img, lang='tha+eng').strip()
    except Exception as e:
        print(f"OCR error: {e}")
        return ""