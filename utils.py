# utils.py

import os
import psycopg2
from datetime import datetime
import pytesseract
from PIL import Image
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json # เพิ่ม json library

# -------------------------------------
# Google Sheets Functions
# -------------------------------------

def get_gsheet_client():
    """
    Authorizes gspread using credentials stored in an environment variable.
    """
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    # --- START MODIFICATION ---
    # อ่าน credentials จาก environment variable แทนการอ่านจากไฟล์
    creds_json_str = os.getenv("GCP_CREDENTIALS_JSON")
    if not creds_json_str:
        print("Error: GCP_CREDENTIALS_JSON environment variable not set.")
        raise ValueError("GCP credentials are not configured.")
        
    creds_dict = json.loads(creds_json_str)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    # --- END MODIFICATION ---

    return gspread.authorize(creds)

def get_slip_summary_by_day():
    try:
        client = get_gsheet_client()
        sheet = client.open("slip_records").sheet1
        records = sheet.get_all_records()
        summary = {}
        for r in records:
            # เพิ่มการตรวจสอบ key 'timestamp' ก่อนใช้งาน
            if 'timestamp' in r and r['timestamp']:
                date = r['timestamp'].split(' ')[0]
                amount = float(r['amount']) if r.get('amount') else 0
                summary.setdefault(date, 0)
                summary[date] += amount
        # แก้ไขการ return ให้เป็น dictionary เหมือนเดิมเพื่อให้ง่ายต่อการใช้งานใน template
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
            # เพิ่มการตรวจสอบ key 'timestamp' ก่อนใช้งาน
            if 'timestamp' in r and r['timestamp']:
                month = r['timestamp'][:7]  # YYYY-MM
                amount = float(r['amount']) if r.get('amount') else 0
                summary.setdefault(month, 0)
                summary[month] += amount
        # แก้ไขการ return ให้เป็น dictionary เหมือนเดิม
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


# -------------------------------------
# PostgreSQL Functions
# -------------------------------------

# DATABASE_URL จะถูกตั้งค่าโดย Render.com โดยอัตโนมัติ
DATABASE_URL = os.getenv("DATABASE_URL")

def connect_db():
    if not DATABASE_URL:
        raise ConnectionError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(DATABASE_URL)

def create_tables():
    """Initializes database tables if they don't exist."""
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    user_message TEXT,
                    bot_response TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS session_data (
                    user_id TEXT PRIMARY KEY,
                    context TEXT,
                    last_updated TIMESTAMP
                );
            """)
            conn.commit()
            print("Tables created or already exist.")
    except Exception as e:
        print(f"Error creating tables: {e}")
    finally:
        conn.close()

# Functions for save_chat, get_chat_history, save_session, get_session, clear_session
# ไม่มีการเปลี่ยนแปลงในส่วนนี้ (โค้ดเหมือนเดิม)
def save_chat(user_id, user_message, bot_response):
    conn = connect_db()
    cur = conn.cursor()
    timestamp = datetime.now()
    cur.execute("""
        INSERT INTO chat_history (user_id, user_message, bot_response, timestamp)
        VALUES (%s, %s, %s, %s)
    """, (user_id, user_message, bot_response, timestamp))
    conn.commit()
    conn.close()

def get_chat_history(limit=100):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM chat_history ORDER BY timestamp DESC LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def save_session(user_id, context):
    conn = connect_db()
    cur = conn.cursor()
    now = datetime.now()
    cur.execute("""
        INSERT INTO session_data (user_id, context, last_updated)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
        context = EXCLUDED.context,
        last_updated = EXCLUDED.last_updated
    """, (user_id, context, now))
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

# -------------------------------------
# OCR and Text Cleaning
# -------------------------------------

# บน Render.com เราต้องกำหนด path นี้ผ่าน Dockerfile หรือ build script
# แต่สำหรับ Python runtime, เราสามารถพึ่งพาการติดตั้งผ่าน OS packages ได้
# ไม่จำเป็นต้องแก้ path นี้ แต่ต้องแจ้งให้ Render ติดตั้ง tesseract
# pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract" 
# Comment ออกไปก่อน เพราะ path อาจจะต่างกัน ให้มันหาเองดีกว่า

def ocr_image(image_path):
    try:
        img = Image.open(image_path)
        return pytesseract.image_to_string(img, lang='tha+eng').strip()
    except Exception as e:
        print(f"OCR error: {e}")
        return ""

def clean_markdown(text):
    text = re.sub(r'(\*\*|__)', '', text)
    text = re.sub(r'(\*|_)', '', text)
    text = re.sub(r'`', '', text)
    text = re.sub(r'~~', '', text)
    text = re.sub(r'^> ', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\-\+\*] ', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# เขียนไฟล์ utils.py ที่แก้ไขแล้ว
with open('utils.py', 'w', encoding='utf-8') as f:
    f.write(
'''
import os
import psycopg2
from datetime import datetime
import pytesseract
from PIL import Image
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

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

DATABASE_URL = os.getenv("DATABASE_URL")

def connect_db():
    if not DATABASE_URL:
        raise ConnectionError("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(DATABASE_URL)

def create_tables():
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    user_message TEXT,
                    bot_response TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS session_data (
                    user_id TEXT PRIMARY KEY,
                    context TEXT,
                    last_updated TIMESTAMP
                );
            """)
            conn.commit()
            print("Tables created or already exist.")
    except Exception as e:
        print(f"Error creating tables: {e}")
    finally:
        conn.close()

def save_chat(user_id, user_message, bot_response):
    conn = connect_db()
    cur = conn.cursor()
    timestamp = datetime.now()
    cur.execute("""
        INSERT INTO chat_history (user_id, user_message, bot_response, timestamp)
        VALUES (%s, %s, %s, %s)
    """, (user_id, user_message, bot_response, timestamp))
    conn.commit()
    conn.close()

def get_chat_history(limit=100):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM chat_history ORDER BY timestamp DESC LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def save_session(user_id, context):
    conn = connect_db()
    cur = conn.cursor()
    now = datetime.now()
    cur.execute("""
        INSERT INTO session_data (user_id, context, last_updated)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
        context = EXCLUDED.context,
        last_updated = EXCLUDED.last_updated
    """, (user_id, context, now))
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

pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

def ocr_image(image_path):
    try:
        img = Image.open(image_path)
        return pytesseract.image_to_string(img, lang='tha+eng').strip()
    except Exception as e:
        print("OCR error:", e)
        return ""

def clean_markdown(text):
    # This function is not used in app.py but we keep it here
    text = re.sub(r'(\*\*|__)', '', text)
    text = re.sub(r'(\*|_)', '', text)
    text = re.sub(r'`', '', text)
    text = re.sub(r'~~', '', text)
    text = re.sub(r'^> ', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\-\+\*] ', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
''')
print("File 'utils.py' has been updated.")
