# utils_postgres.py - สำหรับ Render + PostgreSQL

import os
import psycopg2
from datetime import datetime
import pytesseract
from PIL import Image
import re
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# -------------------------------------
# Google Sheets: สรุปยอดรายวัน / รายเดือน
# -------------------------------------
def get_slip_summary_by_day():
    try:
        client = get_gsheet_client()
        sheet = client.open("slip_records").sheet1
        records = sheet.get_all_records()
        summary = {}
        for r in records:
            date = r['timestamp'].split(' ')[0]
            amount = float(r['amount']) if r['amount'] else 0
            if date not in summary:
                summary[date] = 0
            summary[date] += amount
        return sorted(summary.items(), key=lambda x: x[0], reverse=True)
    except Exception as e:
        print("Summary error:", e)
        return []

def get_slip_summary_by_month():
    try:
        client = get_gsheet_client()
        sheet = client.open("slip_records").sheet1
        records = sheet.get_all_records()
        summary = {}
        for r in records:
            month = r['timestamp'][:7]  # YYYY-MM
            amount = float(r['amount']) if r['amount'] else 0
            if month not in summary:
                summary[month] = 0
            summary[month] += amount
        return sorted(summary.items(), key=lambda x: x[0], reverse=True)
    except Exception as e:
        print("Summary error:", e)
        return []

def get_gsheet_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("gcp_key.json", scope)
    return gspread.authorize(creds)

def append_slip_to_gsheet(user_id, amount, direction, party, source_text, channel=None, transaction_id=None):
    try:
        client = get_gsheet_client()
        sheet = client.open("slip_records").sheet1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([
            timestamp, user_id, amount, direction, party,
            channel or "", transaction_id or "", source_text
        ])
    except Exception as e:
        print("Google Sheets Error:", e)

# PostgreSQL connect
DATABASE_URL = os.getenv("DATABASE_URL")

def connect_db():
    return psycopg2.connect(DATABASE_URL)

def create_tables():
    conn = connect_db()
    cur = conn.cursor()
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

# OCR
pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

def ocr_image(image_path):
    try:
        img = Image.open(image_path)
        return pytesseract.image_to_string(img, lang='tha+eng').strip()
    except Exception as e:
        print("OCR error:", e)
        return ""

# Markdown Cleaner

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
