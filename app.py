"""
LINE Todo Bot - ระบบจัดการ To-Do List ผ่าน LINE Chat
ฟีเจอร์:
  - เพิ่มงาน: พิมพ์ "เพิ่ม <ชื่องาน>"
  - ดูงานค้าง: พิมพ์ "งานค้าง" หรือ "ดูงาน"
  - เสร็จงาน: พิมพ์ "เสร็จ <หมายเลข>" หรือ "done <หมายเลข>"
  - เข้างาน: พิมพ์ "เข้างาน" → แสดงงานวันนี้อัตโนมัติ
  - สรุปวัน: พิมพ์ "สรุป" หรือ auto สรุปตอนท้ายวัน
  - ลบงาน: พิมพ์ "ลบ <หมายเลข>"
  - ช่วยเหลือ: พิมพ์ "help" หรือ "วิธีใช้"
"""

import os
import re
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler

# ============================================================
# Config
# ============================================================
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# เวลาสรุปรายวัน (24-hour format, Thailand timezone UTC+7)
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "18"))
DAILY_SUMMARY_MINUTE = int(os.environ.get("DAILY_SUMMARY_MINUTE", "0"))

DATABASE_PATH = os.environ.get("DATABASE_PATH", "todo.db")

# ============================================================
# Database
# ============================================================
@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """สร้างตาราง database."""
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
# Task Management Functions
# ============================================================
def add_task(chat_id, title, added_by="", due_date=None):
    """เพิ่มงานใหม่."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (chat_id, title, added_by, due_date) VALUES (?, ?, ?, ?)",
            (chat_id, title.strip(), added_by, due_date),
        )
        task_id = cursor.lastrowid
    return {"id": task_id, "title": title.strip()}


def get_pending_tasks(chat_id):
    """ดึงงานที่ยังไม่เสร็จ."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'pending' ORDER BY created_at ASC",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_completed_today(chat_id):
    """ดึงงานที่เสร็จวันนี้."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'done' AND DATE(completed_at) = ? ORDER BY completed_at ASC",
            (chat_id, today),
        ).fetchall()
    return [dict(r) for r in rows]


def complete_task(chat_id, task_number):
    """ทำเครื่องหมายงานเสร็จ (ใช้ลำดับจาก pending list)."""
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
    """ลบงาน (ใช้ลำดับจาก pending list)."""
    pending = get_pending_tasks(chat_id)
    if 1 <= task_number <= len(pending):
        task = pending[task_number - 1]
        with get_db() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
        return task
    return None


def get_all_active_chats():
    """ดึง chat_id ทั้งหมดที่มีงานค้าง."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM tasks WHERE status = 'pending'"
        ).fetchall()
    return [r["chat_id"] for r in rows]


def register_member(chat_id, user_id, display_name=""):
    """บันทึกสมาชิกของแชท."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_members (chat_id, user_id, display_name) VALUES (?, ?, ?)",
            (chat_id, user_id, display_name),
        )


# ============================================================
# Message Builders
# ============================================================
def build_task_list_message(chat_id, header="📋 งานค้างทั้งหมด"):
    """สร้างข้อความแสดงรายการงานค้าง."""
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
    """สร้างข้อความตอน 'เข้างาน'."""
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
    """สร้างข้อความสรุปรายวัน."""
    now = datetime.now()
    completed = get_completed_today(chat_id)
    pending = get_pending_tasks(chat_id)

    lines = [
        "📊 สรุปประจำวัน - {}".format(now.strftime("%d/%m/%Y")),
        "═" * 25,
    ]

    # งานที่เสร็จวันนี้
    lines.append("\n✅ งานที่เสร็จวันนี้ ({} งาน):".format(len(completed)))
    if completed:
        for task in completed:
            lines.append("  ✔️ {}".format(task["title"]))
    else:
        lines.append("  — ยังไม่มีงานเสร็จวันนี้")

    # งานที่ยังค้าง
    lines.append("\n⏳ งานที่ยังค้าง ({} งาน):".format(len(pending)))
    if pending:
        for i, task in enumerate(pending, 1):
            lines.append("  {}. ⬜ {}".format(i, task["title"]))
    else:
        lines.append("  — ไม่มีงานค้าง! 🎉")

    # งานพรุ่งนี้ (= งานค้างที่ต้องทำต่อ)
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
    """สร้างข้อความวิธีใช้."""
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
# LINE Webhook
# ============================================================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature")
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """จัดการข้อความที่เข้ามา."""
    text = event.message.text.strip()

    # ดึง chat_id (กลุ่ม หรือ ส่วนตัว)
    source = event.source
    if source.type == "group":
        chat_id = source.group_id
    elif source.type == "room":
        chat_id = source.room_id
    else:
        chat_id = source.user_id

    user_id = getattr(source, "user_id", "")
    reply_token = event.reply_token

    # ดึงชื่อผู้ส่ง (ถ้าทำได้)
    display_name = get_display_name(user_id)
    if display_name and chat_id != user_id:
        register_member(chat_id, user_id, display_name)

    reply_text = process_command(text, chat_id, display_name)

    if reply_text:
        line_bot_api.reply_message(
            reply_token,
            TextSendMessage(text=reply_text)
        )


def get_display_name(user_id):
    """ดึงชื่อผู้ใช้จาก LINE API."""
    if not user_id:
        return ""
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return ""


def process_command(text, chat_id, display_name=""):
    """ประมวลผลคำสั่ง."""
    text_lower = text.lower().strip()

    # === เข้างาน ===
    if text_lower in ["เข้างาน", "clock in", "เริ่มงาน"]:
        return build_clock_in_message(chat_id)

    # === เพิ่มงาน ===
    match_add = re.match(r"^(?:เพิ่ม|add|todo|งาน)\s+(.+)", text, re.IGNORECASE)
    if match_add:
        title = match_add.group(1).strip()
        task = add_task(chat_id, title, added_by=display_name)
        pending_count = len(get_pending_tasks(chat_id))
        return "✅ เพิ่มงานแล้ว!\n📝 {}\n📌 งานค้างทั้งหมด: {} งาน".format(task["title"], pending_count)

    # === ดูงานค้าง ===
    if text_lower in ["งานค้าง", "ดูงาน", "list", "tasks", "รายการ", "ดู"]:
        return build_task_list_message(chat_id)

    # === เสร็จงาน ===
    match_done = re.match(r"^(?:เสร็จ|done|✅)\s*(\d+)", text, re.IGNORECASE)
    if match_done:
        num = int(match_done.group(1))
        task = complete_task(chat_id, num)
        if task:
            pending_count = len(get_pending_tasks(chat_id))
            return "✅ เสร็จแล้ว!\n✔️ {}\n📌 งานค้างเหลือ: {} งาน".format(task["title"], pending_count)
        return "❌ ไม่พบงานหมายเลข {}\nลองพิมพ์ 'งานค้าง' เพื่อดูรายการ".format(num)

    # === ลบงาน ===
    match_delete = re.match(r"^(?:ลบ|delete|remove)\s*(\d+)", text, re.IGNORECASE)
    if match_delete:
        num = int(match_delete.group(1))
        task = delete_task(chat_id, num)
        if task:
            return "🗑️ ลบงานแล้ว: {}".format(task["title"])
        return "❌ ไม่พบงานหมายเลข {}".format(num)

    # === สรุปรายวัน ===
    if text_lower in ["สรุป", "summary", "รายงาน", "report"]:
        return build_daily_summary(chat_id)

    # === ช่วยเหลือ ===
    if text_lower in ["help", "วิธีใช้", "ช่วย", "คำสั่ง", "?", "เมนู", "menu"]:
        return build_help_message()

    # ไม่ตรงกับคำสั่งใดเลย → ไม่ตอบ (เพื่อไม่รบกวนแชทกลุ่ม)
    return None


# ============================================================
# Scheduled Daily Summary
# ============================================================
def send_daily_summary():
    """ส่งสรุปรายวันให้ทุก chat ที่มีงานค้าง."""
    chat_ids = get_all_active_chats()
    for chat_id in chat_ids:
        try:
            summary = build_daily_summary(chat_id)
            line_bot_api.push_message(chat_id, TextSendMessage(text=summary))
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

# ตั้ง scheduler สรุปรายวัน
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
