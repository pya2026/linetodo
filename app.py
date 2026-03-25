"""
LINE Todo Bot - ระบบจัดการ To-Do List ผ่าน LINE Chat
ใช้ Flask + requests เท่านั้น (ไม่ใช้ line-bot-sdk)
"""

import os
import re
import json
import sqlite3
import hashlib
import hmac
import base64
from datetime import datetime
from contextlib import contextmanager

import requests
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler

# ============================================================
# Config
# ============================================================
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_API_URL = "https://api.line.me/v2/bot"

DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "18"))
DAILY_SUMMARY_MINUTE = int(os.environ.get("DAILY_SUMMARY_MINUTE", "0"))
DATABASE_PATH = os.environ.get("DATABASE_PATH", "todo.db")


def line_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + LINE_CHANNEL_ACCESS_TOKEN,
    }


def verify_signature(body, signature):
    """ตรวจสอบ signature จาก LINE."""
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(hash_val).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_message(reply_token, text):
    """ส่งข้อความตอบกลับ."""
    url = LINE_API_URL + "/message/reply"
    data = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    requests.post(url, headers=line_headers(), json=data)


def push_message(to, text):
    """ส่งข้อความแบบ push."""
    url = LINE_API_URL + "/message/push"
    data = {
        "to": to,
        "messages": [{"type": "text", "text": text}],
    }
    requests.post(url, headers=line_headers(), json=data)


def get_profile(user_id):
    """ดึงโปรไฟล์ผู้ใช้."""
    try:
        url = LINE_API_URL + "/profile/" + user_id
        resp = requests.get(url, headers=line_headers())
        if resp.status_code == 200:
            return resp.json().get("displayName", "")
    except Exception:
        pass
    return ""


# ============================================================
# Database
# ============================================================
@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                title TEXT NOT NULL,
                added_by TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                due_date DATE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                PRIMARY KEY (chat_id, user_id)
            )
        """)


# ============================================================
# Task Management
# ============================================================
def add_task(chat_id, title, added_by="", due_date=None):
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (chat_id, title, added_by, due_date) VALUES (?, ?, ?, ?)",
            (chat_id, title.strip(), added_by, due_date),
        )
        task_id = cursor.lastrowid
    return {"id": task_id, "title": title.strip()}


def get_pending_tasks(chat_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'pending' ORDER BY created_at ASC",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_completed_today(chat_id):
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'done' AND DATE(completed_at) = ? ORDER BY completed_at ASC",
            (chat_id, today),
        ).fetchall()
    return [dict(r) for r in rows]


def complete_task(chat_id, task_number):
    pending = get_pending_tasks(chat_id)
    if 1 <= task_number <= len(pending):
        task = pending[task_number - 1]
        with get_db() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (datetime.now().isoformat(), task["id"]),
            )
        return task
    return None


def delete_task(chat_id, task_number):
    pending = get_pending_tasks(chat_id)
    if 1 <= task_number <= len(pending):
        task = pending[task_number - 1]
        with get_db() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
        return task
    return None


def get_all_active_chats():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM tasks WHERE status = 'pending'"
        ).fetchall()
    return [r["chat_id"] for r in rows]


def register_member(chat_id, user_id, display_name=""):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_members (chat_id, user_id, display_name) VALUES (?, ?, ?)",
            (chat_id, user_id, display_name),
        )


# ============================================================
# Message Builders
# ============================================================
def build_task_list_message(chat_id, header="📋 งานค้างทั้งหมด"):
    pending = get_pending_tasks(chat_id)
    if not pending:
        return "🎉 ไม่มีงานค้าง! เยี่ยมไปเลย!"

    lines = [header, "─" * 20]
    for i, task in enumerate(pending, 1):
        added = " (โดย {})".format(task["added_by"]) if task["added_by"] else ""
        due = ""
        if task["due_date"]:
            due = " 📅 {}".format(task["due_date"])
        lines.append("{}. ⬜ {}{}{}".format(i, task["title"], added, due))
    lines.append("\n📌 รวม {} งานค้าง".format(len(pending)))
    return "\n".join(lines)


def build_clock_in_message(chat_id):
    now = datetime.now()
    pending = get_pending_tasks(chat_id)

    lines = [
        "🌅 สวัสดีตอนเช้า!",
        "📅 วันที่ {} เวลา {} น.".format(now.strftime("%d/%m/%Y"), now.strftime("%H:%M")),
        "─" * 20,
    ]

    if pending:
        lines.append("📋 งานที่ต้องทำวันนี้ ({} งาน):".format(len(pending)))
        for i, task in enumerate(pending, 1):
            lines.append("  {}. ⬜ {}".format(i, task["title"]))
        lines.append("\n💪 สู้ๆ นะครับ! ทำงานให้เสร็จกันเยอะๆ")
    else:
        lines.append("🎉 ไม่มีงานค้าง! วันนี้เริ่มต้นด้วยความสดใส")

    return "\n".join(lines)


def build_daily_summary(chat_id):
    now = datetime.now()
    completed = get_completed_today(chat_id)
    pending = get_pending_tasks(chat_id)

    lines = [
        "📊 สรุปประจำวัน - {}".format(now.strftime("%d/%m/%Y")),
        "═" * 25,
    ]

    lines.append("\n✅ งานที่เสร็จวันนี้ ({} งาน):".format(len(completed)))
    if completed:
        for task in completed:
            lines.append("  ✔️ {}".format(task["title"]))
    else:
        lines.append("  — ยังไม่มีงานเสร็จวันนี้")

    lines.append("\n⏳ งานที่ยังค้าง ({} งาน):".format(len(pending)))
    if pending:
        for i, task in enumerate(pending, 1):
            lines.append("  {}. ⬜ {}".format(i, task["title"]))
    else:
        lines.append("  — ไม่มีงานค้าง! 🎉")

    if pending:
        lines.append("\n📅 งานที่ต้องทำพรุ่งนี้:")
        for i, task in enumerate(pending, 1):
            lines.append("  {}. ➡️ {}".format(i, task["title"]))

    lines.append("\n{}".format("─" * 25))
    if len(completed) > 0 and len(pending) == 0:
        lines.append("🏆 ยอดเยี่ยม! ทำงานเสร็จหมดวันนี้!")
    elif len(completed) > 0:
        lines.append("👍 ทำได้ดีมาก! เสร็จไป {} งาน".format(len(completed)))
    else:
        lines.append("💪 พรุ่งนี้มาสู้ใหม่กันนะ!")

    return "\n".join(lines)


def build_help_message():
    return """📖 วิธีใช้ Todo Bot
─────────────────
📝 เพิ่มงาน:
  พิมพ์ "เพิ่ม <ชื่องาน>"
  เช่น: เพิ่ม ส่งรายงานให้หัวหน้า

📋 ดูงานค้าง:
  พิมพ์ "งานค้าง" หรือ "ดูงาน"

✅ งานเสร็จ:
  พิมพ์ "เสร็จ <หมายเลข>"
  เช่น: เสร็จ 1

🗑️ ลบงาน:
  พิมพ์ "ลบ <หมายเลข>"
  เช่น: ลบ 3

🌅 เข้างาน:
  พิมพ์ "เข้างาน"
  → แสดงงานวันนี้อัตโนมัติ

📊 สรุปรายวัน:
  พิมพ์ "สรุป"
  → สรุปงานเสร็จ/ค้าง/พรุ่งนี้
  (auto สรุปทุกวันเวลา 18:00)

💡 เคล็ดลับ:
  ใช้ได้ทั้งแชทกลุ่มและแชทส่วนตัว!"""


# ============================================================
# Command Processing
# ============================================================
def process_command(text, chat_id, display_name=""):
    text_lower = text.lower().strip()

    if text_lower in ["เข้างาน", "clock in", "เริ่มงาน"]:
        return build_clock_in_message(chat_id)

    match_add = re.match(r"^(?:เพิ่ม|add|todo|งาน)\s+(.+)", text, re.IGNORECASE)
    if match_add:
        title = match_add.group(1).strip()
        task = add_task(chat_id, title, added_by=display_name)
        pending_count = len(get_pending_tasks(chat_id))
        return "✅ เพิ่มงานแล้ว!\n📝 {}\n📌 งานค้างทั้งหมด: {} งาน".format(task["title"], pending_count)

    if text_lower in ["งานค้าง", "ดูงาน", "list", "tasks", "รายการ", "ดู"]:
        return build_task_list_message(chat_id)

    match_done = re.match(r"^(?:เสร็จ|done|✅)\s*(\d+)", text, re.IGNORECASE)
    if match_done:
        num = int(match_done.group(1))
        task = complete_task(chat_id, num)
        if task:
            pending_count = len(get_pending_tasks(chat_id))
            return "✅ เสร็จแล้ว!\n✔️ {}\n📌 งานค้างเหลือ: {} งาน".format(task["title"], pending_count)
        return "❌ ไม่พบงานหมายเลข {}\nลองพิมพ์ 'งานค้าง' เพื่อดูรายการ".format(num)

    match_delete = re.match(r"^(?:ลบ|delete|remove)\s*(\d+)", text, re.IGNORECASE)
    if match_delete:
        num = int(match_delete.group(1))
        task = delete_task(chat_id, num)
        if task:
            return "🗑️ ลบงานแล้ว: {}".format(task["title"])
        return "❌ ไม่พบงานหมายเลข {}".format(num)

    if text_lower in ["สรุป", "summary", "รายงาน", "report"]:
        return build_daily_summary(chat_id)

    if text_lower in ["help", "วิธีใช้", "ช่วย", "คำสั่ง", "?", "เมนู", "menu"]:
        return build_help_message()

    return None


# ============================================================
# Webhook
# ============================================================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    if not verify_signature(body, signature):
        abort(400)

    data = json.loads(body)
    events = data.get("events", [])

    for event in events:
        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue

        text = event["message"]["text"].strip()
        reply_token = event["replyToken"]

        source = event.get("source", {})
        source_type = source.get("type", "")
        if source_type == "group":
            chat_id = source.get("groupId", "")
        elif source_type == "room":
            chat_id = source.get("roomId", "")
        else:
            chat_id = source.get("userId", "")

        user_id = source.get("userId", "")
        display_name = get_profile(user_id) if user_id else ""

        if display_name and chat_id != user_id:
            register_member(chat_id, user_id, display_name)

        reply_text = process_command(text, chat_id, display_name)

        if reply_text:
            reply_message(reply_token, reply_text)

    return "OK"


# ============================================================
# Scheduled Daily Summary
# ============================================================
def send_daily_summary():
    chat_ids = get_all_active_chats()
    for chat_id in chat_ids:
        try:
            summary = build_daily_summary(chat_id)
            push_message(chat_id, summary)
        except Exception as e:
            app.logger.error("Failed to send summary to {}: {}".format(chat_id, e))


# ============================================================
# Health Check
# ============================================================
@app.route("/", methods=["GET"])
def health():
    return "LINE Todo Bot is running! 🤖"


# ============================================================
# Main
# ============================================================
init_db()

scheduler = BackgroundScheduler(timezone="Asia/Bangkok")
scheduler.add_job(
    send_daily_summary,
    "cron",
    hour=DAILY_SUMMARY_HOUR,
    minute=DAILY_SUMMARY_MINUTE,
)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
