"""
LINE Todo Bot v3 - Interactive Flex Message + Comments
ฟีเจอร์:
  - เพิ่มงาน: พิมพ์ "เพิ่ม <ชื่องาน>"
  - ดูงาน: กดปุ่ม interactive (เสร็จ/แก้ไข/ลบ/comment)
  - แก้ไขงาน: พิมพ์ "แก้ <หมายเลข> <ชื่อใหม่>"
  - comment งาน: พิมพ์ "note <หมายเลข> <ข้อความ>"
  - ดู comment: พิมพ์ "ดูnote <หมายเลข>"
  - เข้างาน / สรุป / auto สรุป 18:00
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
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(hash_val).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_message(reply_token, messages):
    """ส่งข้อความตอบกลับ (รับ list ของ messages)."""
    url = LINE_API_URL + "/message/reply"
    if isinstance(messages, str):
        messages = [{"type": "text", "text": messages}]
    elif isinstance(messages, dict):
        messages = [messages]
    data = {"replyToken": reply_token, "messages": messages}
    requests.post(url, headers=line_headers(), json=data)


def push_message(to, messages):
    url = LINE_API_URL + "/message/push"
    if isinstance(messages, str):
        messages = [{"type": "text", "text": messages}]
    elif isinstance(messages, dict):
        messages = [messages]
    data = {"to": to, "messages": messages}
    requests.post(url, headers=line_headers(), json=data)


def get_profile(user_id):
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
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                author TEXT DEFAULT '',
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
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
def add_task(chat_id, title, added_by=""):
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (chat_id, title, added_by) VALUES (?, ?, ?)",
            (chat_id, title.strip(), added_by),
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


def complete_task_by_id(task_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row and row["status"] == "pending":
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (datetime.now().isoformat(), task_id),
            )
            return dict(row)
    return None


def edit_task(chat_id, task_number, new_title):
    pending = get_pending_tasks(chat_id)
    if 1 <= task_number <= len(pending):
        task = pending[task_number - 1]
        with get_db() as conn:
            conn.execute(
                "UPDATE tasks SET title = ? WHERE id = ?",
                (new_title.strip(), task["id"]),
            )
        return {"id": task["id"], "old_title": task["title"], "new_title": new_title.strip()}
    return None


def edit_task_by_id(task_id, new_title):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row:
            conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (new_title.strip(), task_id))
            return {"id": task_id, "old_title": row["title"], "new_title": new_title.strip()}
    return None


def delete_task(chat_id, task_number):
    pending = get_pending_tasks(chat_id)
    if 1 <= task_number <= len(pending):
        task = pending[task_number - 1]
        with get_db() as conn:
            conn.execute("DELETE FROM comments WHERE task_id = ?", (task["id"],))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
        return task
    return None


def delete_task_by_id(task_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row:
            conn.execute("DELETE FROM comments WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return dict(row)
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
# Comments
# ============================================================
def add_comment(task_id, chat_id, author, content):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO comments (task_id, chat_id, author, content) VALUES (?, ?, ?, ?)",
            (task_id, chat_id, author, content),
        )
    return True


def get_comments(task_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_task_by_id(task_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


# ============================================================
# Flex Message Builders
# ============================================================
def build_task_card(task, index):
    """สร้างการ์ดงานแต่ละชิ้น พร้อมปุ่มกด."""
    task_id = task["id"]
    added_info = ""
    if task["added_by"]:
        added_info = "โดย {}".format(task["added_by"])

    comments = get_comments(task_id)
    comment_count = len(comments)
    comment_text = "💬 {} ความคิดเห็น".format(comment_count) if comment_count > 0 else "💬 ยังไม่มีความคิดเห็น"

    card = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "text",
                    "text": "#{} ⬜".format(index),
                    "weight": "bold",
                    "color": "#1DB446",
                    "size": "sm",
                    "flex": 0,
                },
                {
                    "type": "text",
                    "text": task["title"],
                    "weight": "bold",
                    "size": "sm",
                    "wrap": True,
                    "flex": 5,
                    "margin": "sm",
                },
            ],
            "paddingAll": "12px",
            "backgroundColor": "#F5FFF5",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": added_info if added_info else "เพิ่มเอง",
                    "size": "xs",
                    "color": "#999999",
                },
                {
                    "type": "text",
                    "text": comment_text,
                    "size": "xs",
                    "color": "#666666",
                    "margin": "sm",
                },
            ],
            "paddingAll": "10px",
            "spacing": "xs",
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "✅ เสร็จ",
                        "data": "action=done&task_id={}".format(task_id),
                    },
                    "style": "primary",
                    "height": "sm",
                    "color": "#1DB446",
                },
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "✏️ แก้ไข",
                        "data": "action=edit_prompt&task_id={}".format(task_id),
                    },
                    "style": "secondary",
                    "height": "sm",
                    "margin": "sm",
                },
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "💬",
                        "data": "action=view_comments&task_id={}".format(task_id),
                    },
                    "style": "secondary",
                    "height": "sm",
                    "margin": "sm",
                },
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "🗑️",
                        "data": "action=delete&task_id={}".format(task_id),
                    },
                    "style": "secondary",
                    "height": "sm",
                    "margin": "sm",
                },
            ],
            "paddingAll": "10px",
        },
    }
    return card


def build_task_list_flex(chat_id):
    """สร้าง Flex Carousel แสดงงานค้างทั้งหมด."""
    pending = get_pending_tasks(chat_id)
    if not pending:
        return {"type": "text", "text": "🎉 ไม่มีงานค้าง! เยี่ยมไปเลย!"}

    bubbles = []
    for i, task in enumerate(pending, 1):
        bubbles.append(build_task_card(task, i))
        if len(bubbles) >= 10:
            break

    if len(bubbles) == 1:
        return {"type": "flex", "altText": "📋 งานค้าง {} งาน".format(len(pending)), "contents": bubbles[0]}

    return {
        "type": "flex",
        "altText": "📋 งานค้าง {} งาน".format(len(pending)),
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }


def build_comment_flex(task_id):
    """สร้าง Flex แสดง comment ของงาน."""
    task = get_task_by_id(task_id)
    if not task:
        return {"type": "text", "text": "❌ ไม่พบงานนี้"}

    comments = get_comments(task_id)

    comment_contents = []
    if comments:
        for c in comments[-10:]:
            time_str = ""
            if c["created_at"]:
                try:
                    dt = datetime.fromisoformat(c["created_at"])
                    time_str = dt.strftime("%H:%M")
                except Exception:
                    time_str = ""

            comment_contents.append({
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {
                        "type": "text",
                        "text": "{}:".format(c["author"] or "ไม่ทราบ"),
                        "size": "xs",
                        "color": "#1DB446",
                        "weight": "bold",
                        "flex": 2,
                    },
                    {
                        "type": "text",
                        "text": c["content"],
                        "size": "xs",
                        "color": "#333333",
                        "wrap": True,
                        "flex": 5,
                    },
                    {
                        "type": "text",
                        "text": time_str,
                        "size": "xxs",
                        "color": "#999999",
                        "flex": 1,
                        "align": "end",
                    },
                ],
                "margin": "md",
            })
    else:
        comment_contents.append({
            "type": "text",
            "text": "ยังไม่มีความคิดเห็น",
            "size": "sm",
            "color": "#999999",
            "style": "italic",
        })

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "💬 ความคิดเห็น",
                    "weight": "bold",
                    "color": "#1DB446",
                    "size": "md",
                },
                {
                    "type": "text",
                    "text": task["title"],
                    "size": "sm",
                    "color": "#333333",
                    "wrap": True,
                    "margin": "sm",
                },
            ],
            "paddingAll": "15px",
            "backgroundColor": "#F5FFF5",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": comment_contents,
            "paddingAll": "15px",
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": 'พิมพ์ "note {} <ข้อความ>" เพื่อเพิ่ม'.format(task_id),
                    "size": "xs",
                    "color": "#999999",
                    "align": "center",
                },
            ],
            "paddingAll": "10px",
        },
    }

    return {"type": "flex", "altText": "💬 comment: {}".format(task["title"]), "contents": bubble}


def build_clock_in_flex(chat_id):
    """สร้าง Flex Message ตอนเข้างาน."""
    now = datetime.now()
    pending = get_pending_tasks(chat_id)

    task_items = []
    if pending:
        for i, task in enumerate(pending, 1):
            task_items.append({
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {"type": "text", "text": "{}".format(i), "size": "sm", "color": "#1DB446", "flex": 0, "weight": "bold"},
                    {"type": "text", "text": "⬜ {}".format(task["title"]), "size": "sm", "wrap": True, "margin": "md"},
                ],
                "margin": "md",
            })
    else:
        task_items.append({
            "type": "text",
            "text": "🎉 ไม่มีงานค้าง!",
            "size": "sm",
            "color": "#1DB446",
        })

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "🌅 สวัสดีตอนเช้า!", "weight": "bold", "size": "lg", "color": "#1DB446"},
                {
                    "type": "text",
                    "text": "📅 {} เวลา {} น.".format(now.strftime("%d/%m/%Y"), now.strftime("%H:%M")),
                    "size": "sm",
                    "color": "#666666",
                    "margin": "sm",
                },
            ],
            "paddingAll": "15px",
            "backgroundColor": "#F5FFF5",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "📋 งานวันนี้ ({} งาน)".format(len(pending)), "weight": "bold", "size": "sm", "margin": "sm"},
                {"type": "separator", "margin": "md"},
            ] + task_items + [
                {"type": "separator", "margin": "lg"},
                {"type": "text", "text": "💪 สู้ๆ นะครับ!", "size": "sm", "color": "#1DB446", "margin": "lg", "align": "center"},
            ],
            "paddingAll": "15px",
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "📋 ดูงานค้าง", "data": "action=list"},
                    "style": "primary",
                    "height": "sm",
                    "color": "#1DB446",
                },
                {
                    "type": "button",
                    "action": {"type": "postback", "label": "📊 สรุป", "data": "action=summary"},
                    "style": "secondary",
                    "height": "sm",
                    "margin": "sm",
                },
            ],
            "paddingAll": "10px",
        },
    }

    return {"type": "flex", "altText": "🌅 เข้างาน - งานวันนี้ {} งาน".format(len(pending)), "contents": bubble}


def build_daily_summary_flex(chat_id):
    """สร้าง Flex สรุปรายวัน."""
    now = datetime.now()
    completed = get_completed_today(chat_id)
    pending = get_pending_tasks(chat_id)

    # สร้างรายการงานเสร็จ
    done_items = []
    if completed:
        for task in completed:
            done_items.append({
                "type": "text",
                "text": "  ✔️ {}".format(task["title"]),
                "size": "xs",
                "color": "#1DB446",
                "wrap": True,
            })
    else:
        done_items.append({"type": "text", "text": "  — ยังไม่มี", "size": "xs", "color": "#999999"})

    # สร้างรายการงานค้าง
    pending_items = []
    if pending:
        for i, task in enumerate(pending, 1):
            pending_items.append({
                "type": "text",
                "text": "  {}. ⬜ {}".format(i, task["title"]),
                "size": "xs",
                "color": "#FF6B35",
                "wrap": True,
            })
    else:
        pending_items.append({"type": "text", "text": "  — ไม่มีงานค้าง! 🎉", "size": "xs", "color": "#1DB446"})

    # สรุป
    if len(completed) > 0 and len(pending) == 0:
        summary_text = "🏆 ยอดเยี่ยม! ทำงานเสร็จหมด!"
        summary_color = "#1DB446"
    elif len(completed) > 0:
        summary_text = "👍 เสร็จไป {} งาน ค้าง {} งาน".format(len(completed), len(pending))
        summary_color = "#FF8C00"
    else:
        summary_text = "💪 พรุ่งนี้สู้ใหม่!"
        summary_color = "#FF6B35"

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "📊 สรุปประจำวัน", "weight": "bold", "size": "lg", "color": "#333333"},
                {"type": "text", "text": now.strftime("%d/%m/%Y"), "size": "sm", "color": "#999999"},
            ],
            "paddingAll": "15px",
            "backgroundColor": "#FFF9E6",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "✅ เสร็จวันนี้ ({})".format(len(completed)), "weight": "bold", "size": "sm", "color": "#1DB446"},
            ] + done_items + [
                {"type": "separator", "margin": "lg"},
                {"type": "text", "text": "⏳ งานค้าง ({})".format(len(pending)), "weight": "bold", "size": "sm", "color": "#FF6B35", "margin": "lg"},
            ] + pending_items + [
                {"type": "separator", "margin": "lg"},
                {"type": "text", "text": summary_text, "size": "sm", "color": summary_color, "weight": "bold", "margin": "lg", "align": "center"},
            ],
            "paddingAll": "15px",
            "spacing": "sm",
        },
    }

    return {"type": "flex", "altText": "📊 สรุปวัน - เสร็จ {} ค้าง {}".format(len(completed), len(pending)), "contents": bubble}


def build_quick_reply():
    """สร้าง Quick Reply ปุ่มด้านล่าง."""
    return {
        "items": [
            {"type": "action", "action": {"type": "postback", "label": "📋 ดูงาน", "data": "action=list"}},
            {"type": "action", "action": {"type": "postback", "label": "📊 สรุป", "data": "action=summary"}},
            {"type": "action", "action": {"type": "message", "label": "🌅 เข้างาน", "text": "เข้างาน"}},
            {"type": "action", "action": {"type": "message", "label": "❓ วิธีใช้", "text": "help"}},
        ]
    }


def build_help_message():
    return {
        "type": "flex",
        "altText": "📖 วิธีใช้ Todo Bot",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "📖 วิธีใช้ Todo Bot", "weight": "bold", "size": "lg", "color": "#1DB446"},
                ],
                "paddingAll": "15px",
                "backgroundColor": "#F5FFF5",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "📝 เพิ่มงาน", "weight": "bold", "size": "sm", "color": "#1DB446"},
                    {"type": "text", "text": 'พิมพ์ "เพิ่ม ส่งรายงาน"', "size": "xs", "color": "#666", "margin": "sm"},

                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "📋 ดูงาน (กดปุ่มได้!)", "weight": "bold", "size": "sm", "color": "#1DB446", "margin": "lg"},
                    {"type": "text", "text": 'พิมพ์ "งานค้าง" หรือ "ดูงาน"', "size": "xs", "color": "#666", "margin": "sm"},

                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "✏️ แก้ไขงาน", "weight": "bold", "size": "sm", "color": "#1DB446", "margin": "lg"},
                    {"type": "text", "text": 'พิมพ์ "แก้ 1 ชื่องานใหม่"', "size": "xs", "color": "#666", "margin": "sm"},

                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "💬 เพิ่ม comment", "weight": "bold", "size": "sm", "color": "#1DB446", "margin": "lg"},
                    {"type": "text", "text": 'พิมพ์ "note 1 รอข้อมูลจากลูกค้า"', "size": "xs", "color": "#666", "margin": "sm"},

                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "🌅 เข้างาน / 📊 สรุป", "weight": "bold", "size": "sm", "color": "#1DB446", "margin": "lg"},
                    {"type": "text", "text": 'พิมพ์ "เข้างาน" หรือ "สรุป"', "size": "xs", "color": "#666", "margin": "sm"},

                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "⏰ auto สรุปทุกวัน 18:00 น.", "size": "xs", "color": "#999", "margin": "lg", "align": "center"},
                    {"type": "text", "text": "ใช้ได้ทั้งแชทกลุ่มและส่วนตัว!", "size": "xs", "color": "#999", "margin": "sm", "align": "center"},
                ],
                "paddingAll": "15px",
            },
        },
    }


# ============================================================
# Command Processing (Text)
# ============================================================
def process_command(text, chat_id, display_name=""):
    text_lower = text.lower().strip()

    # เข้างาน
    if text_lower in ["เข้างาน", "clock in", "เริ่มงาน"]:
        return build_clock_in_flex(chat_id)

    # เพิ่มงาน
    match_add = re.match(r"^(?:เพิ่ม|add|todo)\s+(.+)", text, re.IGNORECASE)
    if match_add:
        title = match_add.group(1).strip()
        task = add_task(chat_id, title, added_by=display_name)
        pending_count = len(get_pending_tasks(chat_id))
        msg = {
            "type": "text",
            "text": "✅ เพิ่มงานแล้ว!\n📝 {}\n📌 งานค้างทั้งหมด: {} งาน".format(task["title"], pending_count),
            "quickReply": build_quick_reply(),
        }
        return msg

    # ดูงานค้าง
    if text_lower in ["งานค้าง", "ดูงาน", "list", "tasks", "รายการ", "ดู", "ดูงาน"]:
        return build_task_list_flex(chat_id)

    # แก้ไขงาน
    match_edit = re.match(r"^(?:แก้|edit)\s+(\d+)\s+(.+)", text, re.IGNORECASE)
    if match_edit:
        num = int(match_edit.group(1))
        new_title = match_edit.group(2).strip()
        result = edit_task(chat_id, num, new_title)
        if result:
            return {"type": "text", "text": "✏️ แก้ไขแล้ว!\nเดิม: {}\nใหม่: {}".format(result["old_title"], result["new_title"]), "quickReply": build_quick_reply()}
        return {"type": "text", "text": "❌ ไม่พบงานหมายเลข {}".format(num)}

    # แก้ไขงาน by ID (จากปุ่มกด)
    match_edit_id = re.match(r"^(?:editid)\s+(\d+)\s+(.+)", text, re.IGNORECASE)
    if match_edit_id:
        task_id = int(match_edit_id.group(1))
        new_title = match_edit_id.group(2).strip()
        result = edit_task_by_id(task_id, new_title)
        if result:
            return {"type": "text", "text": "✏️ แก้ไขแล้ว!\nเดิม: {}\nใหม่: {}".format(result["old_title"], result["new_title"]), "quickReply": build_quick_reply()}
        return {"type": "text", "text": "❌ ไม่พบงานนี้"}

    # เสร็จงาน
    match_done = re.match(r"^(?:เสร็จ|done|✅)\s*(\d+)", text, re.IGNORECASE)
    if match_done:
        num = int(match_done.group(1))
        task = complete_task(chat_id, num)
        if task:
            pending_count = len(get_pending_tasks(chat_id))
            return {"type": "text", "text": "✅ เสร็จแล้ว!\n✔️ {}\n📌 เหลือ: {} งาน".format(task["title"], pending_count), "quickReply": build_quick_reply()}
        return {"type": "text", "text": "❌ ไม่พบงานหมายเลข {}".format(num)}

    # ลบงาน
    match_delete = re.match(r"^(?:ลบ|delete|remove)\s*(\d+)", text, re.IGNORECASE)
    if match_delete:
        num = int(match_delete.group(1))
        task = delete_task(chat_id, num)
        if task:
            return {"type": "text", "text": "🗑️ ลบแล้ว: {}".format(task["title"]), "quickReply": build_quick_reply()}
        return {"type": "text", "text": "❌ ไม่พบงานหมายเลข {}".format(num)}

    # เพิ่ม comment (note <task_number or task_id> <content>)
    match_note = re.match(r"^(?:note|โน้ต|คอมเม้น|comment)\s+(\d+)\s+(.+)", text, re.IGNORECASE)
    if match_note:
        task_ref = int(match_note.group(1))
        content = match_note.group(2).strip()
        # ลองหา by ID ก่อน
        task = get_task_by_id(task_ref)
        if not task or task["chat_id"] != chat_id:
            # ลองเป็นลำดับ
            pending = get_pending_tasks(chat_id)
            if 1 <= task_ref <= len(pending):
                task = pending[task_ref - 1]
        if task:
            add_comment(task["id"], chat_id, display_name, content)
            return {"type": "text", "text": "💬 เพิ่ม comment แล้ว!\n📝 งาน: {}\n💭 {}".format(task["title"], content), "quickReply": build_quick_reply()}
        return {"type": "text", "text": "❌ ไม่พบงานหมายเลข {}".format(task_ref)}

    # ดู comment
    match_view_note = re.match(r"^(?:ดูnote|ดูโน้ต|viewnote)\s*(\d+)", text, re.IGNORECASE)
    if match_view_note:
        task_ref = int(match_view_note.group(1))
        task = get_task_by_id(task_ref)
        if not task or task["chat_id"] != chat_id:
            pending = get_pending_tasks(chat_id)
            if 1 <= task_ref <= len(pending):
                task = pending[task_ref - 1]
        if task:
            return build_comment_flex(task["id"])
        return {"type": "text", "text": "❌ ไม่พบงานหมายเลข {}".format(task_ref)}

    # สรุป
    if text_lower in ["สรุป", "summary", "รายงาน", "report"]:
        return build_daily_summary_flex(chat_id)

    # help
    if text_lower in ["help", "วิธีใช้", "ช่วย", "คำสั่ง", "?", "เมนู", "menu"]:
        return build_help_message()

    return None


# ============================================================
# Postback Handling (ปุ่มกด)
# ============================================================
def handle_postback(data_str, chat_id, reply_token, display_name=""):
    """จัดการ postback จากปุ่มกด."""
    params = {}
    for part in data_str.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = v

    action = params.get("action", "")
    task_id = params.get("task_id", "")

    if action == "done" and task_id:
        task = complete_task_by_id(int(task_id))
        if task:
            pending_count = len(get_pending_tasks(chat_id))
            reply_message(reply_token, {
                "type": "text",
                "text": "✅ เสร็จแล้ว!\n✔️ {}\n📌 เหลือ: {} งาน".format(task["title"], pending_count),
                "quickReply": build_quick_reply(),
            })
        else:
            reply_message(reply_token, "❌ งานนี้เสร็จไปแล้วหรือไม่พบ")

    elif action == "delete" and task_id:
        task = delete_task_by_id(int(task_id))
        if task:
            reply_message(reply_token, {
                "type": "text",
                "text": "🗑️ ลบแล้ว: {}".format(task["title"]),
                "quickReply": build_quick_reply(),
            })
        else:
            reply_message(reply_token, "❌ ไม่พบงานนี้")

    elif action == "edit_prompt" and task_id:
        task = get_task_by_id(int(task_id))
        if task:
            reply_message(reply_token, {
                "type": "text",
                "text": '✏️ ต้องการแก้ไขงาน: "{}"\n\nพิมพ์:\neditid {} ชื่องานใหม่'.format(task["title"], task_id),
            })
        else:
            reply_message(reply_token, "❌ ไม่พบงานนี้")

    elif action == "view_comments" and task_id:
        msg = build_comment_flex(int(task_id))
        reply_message(reply_token, msg)

    elif action == "list":
        msg = build_task_list_flex(chat_id)
        reply_message(reply_token, msg)

    elif action == "summary":
        msg = build_daily_summary_flex(chat_id)
        reply_message(reply_token, msg)


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
        reply_token = event.get("replyToken", "")
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

        # Text message
        if event.get("type") == "message" and event.get("message", {}).get("type") == "text":
            text = event["message"]["text"].strip()
            result = process_command(text, chat_id, display_name)
            if result:
                reply_message(reply_token, result)

        # Postback (ปุ่มกด)
        elif event.get("type") == "postback":
            postback_data = event.get("postback", {}).get("data", "")
            handle_postback(postback_data, chat_id, reply_token, display_name)

    return "OK"


# ============================================================
# Scheduled Daily Summary
# ============================================================
def send_daily_summary():
    chat_ids = get_all_active_chats()
    for chat_id in chat_ids:
        try:
            summary = build_daily_summary_flex(chat_id)
            push_message(chat_id, summary)
        except Exception as e:
            app.logger.error("Failed to send summary to {}: {}".format(chat_id, e))


# ============================================================
# Health Check & Main
# ============================================================
@app.route("/", methods=["GET"])
def health():
    return "LINE Todo Bot v3 is running! 🤖"


init_db()
scheduler = BackgroundScheduler(timezone="Asia/Bangkok")
scheduler.add_job(send_daily_summary, "cron", hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
