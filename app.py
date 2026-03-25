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

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from apscheduler.schedulers.background import BackgroundScheduler

# ============================================================
# Config
# ============================================================
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
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
def add_task(chat_id: str, title: str, added_by: str = "", due_date: str = None) -> dict:
    """เพิ่มงานใหม่."""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (chat_id, title, added_by, due_date) VALUES (?, ?, ?, ?)",
            (chat_id, title.strip(), added_by, due_date),
        )
        task_id = cursor.lastrowid
    return {"id": task_id, "title": title.strip()}


def get_pending_tasks(chat_id: str) -> list:
    """ดึงงานที่ยังไม่เสร็จ."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'pending' ORDER BY created_at ASC",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_completed_today(chat_id: str) -> list:
    """ดึงงานที่เสร็จวันนี้."""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND status = 'done' AND DATE(completed_at) = ? ORDER BY completed_at ASC",
            (chat_id, today),
        ).fetchall()
    return [dict(r) for r in rows]


def complete_task(chat_id: str, task_number: int) -> dict | None:
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


def delete_task(chat_id: str, task_number: int) -> dict | None:
    """ลบงาน (ใช้ลำดับจาก pending list)."""
    pending = get_pending_tasks(chat_id)
    if 1 <= task_number <= len(pending):
        task = pending[task_number - 1]
        with get_db() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
        return task
    return None


def get_all_active_chats() -> list:
    """ดึง chat_id ทั้งหมดที่มีงานค้าง."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM tasks WHERE status = 'pending'"
        ).fetchall()
    return [r["chat_id"] for r in rows]


def register_member(chat_id: str, user_id: str, display_name: str = ""):
    """บันทึกสมาชิกของแชท."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_members (chat_id, user_id, display_name) VALUES (?, ?, ?)",
            (chat_id, user_id, display_name),
        )


# ============================================================
# Message Builders
# ============================================================
def build_task_list_message(chat_id: str, header: str = "📋 งานค้างทั้งหมด") -> str:
    """สร้างข้อความแสดงรายการงานค้าง."""
    pending = get_pending_tasks(chat_id)
    if not pending:
        return "🎉 ไม่มีงานค้าง! เยี่ยมไปเลย!"

    lines = [header, "─" * 20]
    for i, task in enumerate(pending, 1):
        added = f" (โดย {task['added_by']})" if task["added_by"] else ""
        due = ""
        if task["due_date"]:
            due = f" 📅 {task['due_date']}"
        lines.append(f"{i}. ⬜ {task['title']}{added}{due}")
    lines.append(f"\n📌 รวม {len(pending)} งานค้าง")
    return "\n".join(lines)


def build_clock_in_message(chat_id: str) -> str:
    """สร้างข้อความตอน 'เข้างาน'."""
    now = datetime.now()
    pending = get_pending_tasks(chat_id)

    lines = [
        f"🌅 สวัสดีตอนเช้า!",
        f"📅 วันที่ {now.strftime('%d/%m/%Y')} เวลา {now.strftime('%H:%M')} น.",
        "─" * 20,
    ]

    if pending:
        lines.append(f"📋 งานที่ต้องทำวันนี้ ({len(pending)} งาน):")
        for i, task in enumerate(pending, 1):
            lines.append(f"  {i}. ⬜ {task['title']}")
        lines.append("\n💪 สู้ๆ นะครับ! ทำงานให้เสร็จกันเยอะๆ")
    else:
        lines.append("🎉 ไม่มีงานค้าง! วันนี้เริ่มต้นด้วยความสดใส")

    return "\n".join(lines)


def build_daily_summary(chat_id: str) -> str:
    """สร้างข้อความสรุปรายวัน."""
    now = datetime.now()
    completed = get_completed_today(chat_id)
    pending = get_pending_tasks(chat_id)

    lines = [
        f"📊 สรุปประจำวัน - {now.strftime('%d/%m/%Y')}",
        "═" * 25,
    ]

    # งานที่เสร็จวันนี้
    lines.append(f"\n✅ งานที่เสร็จวันนี้ ({len(completed)} งาน):")
    if completed:
        for task in completed:
            lines.append(f"  ✔️ {task['title']}")
    else:
        lines.append("  — ยังไม่มีงานเสร็จวันนี้")

    # งานที่ยังค้าง
    lines.append(f"\n⏳ งานที่ยังค้าง ({len(pending)} งาน):")
    if pending:
        for i, task in enumerate(pending, 1):
            lines.append(f"  {i}. ⬜ {task['title']}")
    else:
        lines.append("  — ไม่มีงานค้าง! 🎉")

    # งานพรุ่งนี้ (= งานค้างที่ต้องทำต่อ)
    if pending:
        lines.append(f"\n📅 งานที่ต้องทำพรุ่งนี้:")
        for i, task in enumerate(pending, 1):
            lines.append(f"  {i}. ➡️ {task['title']}")

    lines.append(f"\n{'─' * 25}")
    if len(completed) > 0 and len(pending) == 0:
        lines.append("🏆 ยอดเยี่ยม! ทำงานเสร็จหมดวันนี้!")
    elif len(completed) > 0:
        lines.append(f"👍 ทำได้ดีมาก! เสร็จไป {len(completed)} งาน")
    else:
        lines.append("💪 พรุ่งนี้มาสู้ใหม่กันนะ!")

    return "\n".join(lines)


def build_help_message() -> str:
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


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """จัดการข้อความที่เข้ามา."""
    text = event.message.text.strip()
    chat_id = event.source.group_id if hasattr(event.source, "group_id") and event.source.group_id else event.source.user_id
    user_id = event.source.user_id if hasattr(event.source, "user_id") else ""
    reply_token = event.reply_token

    # ดึงชื่อผู้ส่ง (ถ้าทำได้)
    display_name = get_display_name(user_id)
    if display_name and chat_id != user_id:
        register_member(chat_id, user_id, display_name)

    reply_text = process_command(text, chat_id, display_name)

    if reply_text:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=reply_text)],
                )
            )


def get_display_name(user_id: str) -> str:
    """ดึงชื่อผู้ใช้จาก LINE API."""
    if not user_id:
        return ""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            profile = line_bot_api.get_profile(user_id)
            return profile.display_name
    except Exception:
        return ""


def process_command(text: str, chat_id: str, display_name: str = "") -> str | None:
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
        return f"✅ เพิ่มงานแล้ว!\n📝 {task['title']}\n📌 งานค้างทั้งหมด: {pending_count} งาน"

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
            return f"✅ เสร็จแล้ว!\n✔️ {task['title']}\n📌 งานค้างเหลือ: {pending_count} งาน"
        return f"❌ ไม่พบงานหมายเลข {num}\nลองพิมพ์ 'งานค้าง' เพื่อดูรายการ"

    # === ลบงาน ===
    match_delete = re.match(r"^(?:ลบ|delete|remove)\s*(\d+)", text, re.IGNORECASE)
    if match_delete:
        num = int(match_delete.group(1))
        task = delete_task(chat_id, num)
        if task:
            return f"🗑️ ลบงานแล้ว: {task['title']}"
        return f"❌ ไม่พบงานหมายเลข {num}"

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
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for chat_id in chat_ids:
            try:
                summary = build_daily_summary(chat_id)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=chat_id,
                        messages=[TextMessage(text=summary)],
                    )
                )
            except Exception as e:
                app.logger.error(f"Failed to send summary to {chat_id}: {e}")


# ============================================================
# Health Check
# ============================================================
@app.route("/", methods=["GET"])
def health():
    return "LINE Todo Bot is running! 🤖"


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
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

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
