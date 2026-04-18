"""
LINE Todo Bot v6 — LIFF Hybrid + Activity Log
- Messaging API: Flex cards, Quick Reply, auto สรุป 18:00
- LIFF: หน้าเว็บจัดการงาน (แก้ไข/comment/ถามคนสั่ง/log)
- Activity Log: บันทึกทุก action (เปิด/แก้/comment/เสร็จ/ลบ)
- สรุป: dropdown เลือกงานเสร็จ + ถังขยะลบ + ยืนยัน
"""

import os, re, json, sqlite3, hashlib, hmac, base64
from datetime import datetime
from contextlib import contextmanager

import requests
from flask import Flask, request, abort, jsonify
# APScheduler removed — use "เลิกงาน" command instead

app = Flask(__name__)

# Fix JSON serialization for PostgreSQL datetime objects
from datetime import date as date_type
class CustomJSONProvider(app.json_provider_class):
    def default(self, obj):
        if isinstance(obj, datetime): return obj.isoformat()
        if isinstance(obj, date_type): return obj.isoformat()
        return super().default(obj)
app.json_provider_class = CustomJSONProvider
app.json = CustomJSONProvider(app)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
LIFF_ID                   = os.environ.get("LIFF_ID", "")
APP_URL                   = os.environ.get("APP_URL", "")
LINE_API_URL              = "https://api.line.me/v2/bot"
# Scheduled summary removed — triggered by "เลิกงาน" command
DATABASE_PATH             = os.environ.get("DATABASE_PATH", "todo.db")
DATABASE_URL              = os.environ.get("DATABASE_URL", "")
USE_PG                    = bool(DATABASE_URL)

if USE_PG:
    import psycopg2, psycopg2.extras
    app.logger.info("Using PostgreSQL: %s", DATABASE_URL[:30]+"...")

def lh():
    return {"Content-Type":"application/json","Authorization":"Bearer "+LINE_CHANNEL_ACCESS_TOKEN}
def verify_sig(body, sig):
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode(), sig)
def reply_msg(tok, msgs):
    if isinstance(msgs, str): msgs = [{"type":"text","text":msgs}]
    elif isinstance(msgs, dict): msgs = [msgs]
    try:
        r = requests.post(LINE_API_URL+"/message/reply", headers=lh(), json={"replyToken":tok,"messages":msgs}, timeout=10)
        if r.status_code!=200: app.logger.error("Reply err: %s %s", r.status_code, r.text)
    except Exception as e:
        app.logger.error("Reply exception: %s", e)
def push_msg(to, msgs):
    if isinstance(msgs, str): msgs = [{"type":"text","text":msgs}]
    elif isinstance(msgs, dict): msgs = [msgs]
    try:
        requests.post(LINE_API_URL+"/message/push", headers=lh(), json={"to":to,"messages":msgs}, timeout=10)
    except Exception as e:
        app.logger.error("Push exception: %s", e)
def get_profile(uid):
    try:
        r = requests.get(LINE_API_URL+"/profile/"+uid, headers=lh(), timeout=5)
        if r.status_code==200: return r.json().get("displayName","")
    except Exception as e:
        app.logger.warning("get_profile err for %s: %s", uid, e)
    return ""

# ── Database ─────────────────────────────────────────────────
class DictRow:
    """Wrap psycopg2 RealDictRow to behave like sqlite3.Row for dict(r)"""
    pass

@contextmanager
def get_db():
    if USE_PG:
        c = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try: yield c; c.commit()
        finally: c.close()
    else:
        c = sqlite3.connect(DATABASE_PATH); c.row_factory = sqlite3.Row
        try: yield c; c.commit()
        finally: c.close()

def q(sql):
    """Convert ? placeholders to %s for PostgreSQL"""
    if USE_PG: return sql.replace("?", "%s")
    return sql

def init_db():
    if USE_PG:
        with get_db() as c:
            cur = c.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY, chat_id TEXT NOT NULL,
                title TEXT NOT NULL, added_by TEXT DEFAULT '', added_by_user_id TEXT DEFAULT '',
                assigned_to TEXT DEFAULT '', assigned_to_user_id TEXT DEFAULT '',
                status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP, due_date DATE)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY, task_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL, author TEXT DEFAULT '', author_user_id TEXT DEFAULT '',
                content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS chat_members (
                chat_id TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT DEFAULT '',
                PRIMARY KEY (chat_id, user_id))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS pending_actions (
                user_chat_key TEXT PRIMARY KEY, action TEXT NOT NULL,
                data TEXT DEFAULT '', created_at TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY, task_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL, user_name TEXT DEFAULT '', user_id TEXT DEFAULT '',
                action TEXT NOT NULL, detail TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT NOW())""")
            # Add missing columns to existing tables (safe to run repeatedly)
            for col in ["added_by_user_id","assigned_to","assigned_to_user_id"]:
                try: cur.execute("ALTER TABLE tasks ADD COLUMN {} TEXT DEFAULT ''".format(col))
                except: c.rollback()
            for col in ["author_user_id"]:
                try: cur.execute("ALTER TABLE comments ADD COLUMN {} TEXT DEFAULT ''".format(col))
                except: c.rollback()
            for col in ["due_date"]:
                try: cur.execute("ALTER TABLE tasks ADD COLUMN {} DATE".format(col))
                except: c.rollback()
            c.commit()
    else:
        with get_db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
                title TEXT NOT NULL, added_by TEXT DEFAULT '', added_by_user_id TEXT DEFAULT '',
                assigned_to TEXT DEFAULT '', assigned_to_user_id TEXT DEFAULT '',
                status TEXT DEFAULT 'pending', created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME, due_date DATE)""")
            for col in ["added_by_user_id","assigned_to","assigned_to_user_id"]:
                try: c.execute("ALTER TABLE tasks ADD COLUMN {} TEXT DEFAULT ''".format(col))
                except: pass
            c.execute("""CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL, author TEXT DEFAULT '', author_user_id TEXT DEFAULT '',
                content TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            for col in ["author_user_id"]:
                try: c.execute("ALTER TABLE comments ADD COLUMN {} TEXT DEFAULT ''".format(col))
                except: pass
            c.execute("""CREATE TABLE IF NOT EXISTS chat_members (
                chat_id TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT DEFAULT '',
                PRIMARY KEY (chat_id, user_id))""")
            c.execute("""CREATE TABLE IF NOT EXISTS pending_actions (
                user_chat_key TEXT PRIMARY KEY, action TEXT NOT NULL,
                data TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL, user_name TEXT DEFAULT '', user_id TEXT DEFAULT '',
                action TEXT NOT NULL, detail TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")

# ── DB Execute helpers ───────────────────────────────────────
def db_exec(conn, sql, params=()):
    """Execute SQL on either SQLite or PostgreSQL connection"""
    if USE_PG:
        cur = conn.cursor()
        cur.execute(q(sql), params)
        return cur
    else:
        return conn.execute(sql, params)

def db_fetchone(conn, sql, params=()):
    cur = db_exec(conn, sql, params)
    row = cur.fetchone()
    if row is None: return None
    return dict(row)

def db_fetchall(conn, sql, params=()):
    cur = db_exec(conn, sql, params)
    return [dict(r) for r in cur.fetchall()]

# ── Activity Log ─────────────────────────────────────────────
def log_activity(task_id, chat_id, user_name, user_id, action, detail=""):
    with get_db() as c:
        db_exec(c, "INSERT INTO activity_log(task_id,chat_id,user_name,user_id,action,detail) VALUES(?,?,?,?,?,?)",
                  (task_id, chat_id, user_name, user_id, action, detail))

def get_activity_log(task_id, limit=20):
    with get_db() as c:
        return db_fetchall(c, "SELECT * FROM activity_log WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit))

# ── Pending Actions ──────────────────────────────────────────
def set_pending(uid, cid, act, data=""):
    with get_db() as c:
        if USE_PG:
            db_exec(c, "INSERT INTO pending_actions VALUES(%s,%s,%s,%s) ON CONFLICT(user_chat_key) DO UPDATE SET action=%s,data=%s,created_at=%s",
                    ("{}:{}".format(uid,cid),act,data,datetime.now().isoformat(),act,data,datetime.now().isoformat()))
        else:
            db_exec(c, "INSERT OR REPLACE INTO pending_actions VALUES(?,?,?,?)",("{}:{}".format(uid,cid),act,data,datetime.now().isoformat()))
def get_pending(uid, cid):
    with get_db() as c:
        r = db_fetchone(c, "SELECT action,data FROM pending_actions WHERE user_chat_key=?",("{}:{}".format(uid,cid),))
    return {"action":r["action"],"data":r["data"]} if r else None
def clear_pending(uid, cid):
    with get_db() as c: db_exec(c, "DELETE FROM pending_actions WHERE user_chat_key=?",("{}:{}".format(uid,cid),))

# ── Task CRUD ────────────────────────────────────────────────
def add_task(cid, title, by="", by_uid="", assign_to="", assign_to_uid=""):
    with get_db() as c:
        if USE_PG:
            cur = db_exec(c, "INSERT INTO tasks(chat_id,title,added_by,added_by_user_id,assigned_to,assigned_to_user_id) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                          (cid,title.strip(),by,by_uid,assign_to,assign_to_uid))
            tid = cur.fetchone()["id"]
        else:
            cur = db_exec(c, "INSERT INTO tasks(chat_id,title,added_by,added_by_user_id,assigned_to,assigned_to_user_id) VALUES(?,?,?,?,?,?)",(cid,title.strip(),by,by_uid,assign_to,assign_to_uid))
            tid = cur.lastrowid
    detail = "สร้างงาน: {}".format(title.strip())
    if assign_to: detail += " → มอบหมายให้ {}".format(assign_to)
    log_activity(tid, cid, by, by_uid, "created", detail)
    return get_task(tid)

def get_task(tid):
    with get_db() as c:
        return db_fetchone(c, "SELECT * FROM tasks WHERE id=?",(tid,))

def get_pending_tasks(cid):
    with get_db() as c:
        return db_fetchall(c, "SELECT * FROM tasks WHERE chat_id=? AND status='pending' ORDER BY created_at",(cid,))

def get_completed_today(cid):
    with get_db() as c:
        return db_fetchall(c, "SELECT * FROM tasks WHERE chat_id=? AND status='done' AND DATE(completed_at)=? ORDER BY completed_at",
            (cid,datetime.now().strftime("%Y-%m-%d")))

def get_tasks_by_assignee(cid, assignee_uid="", assignee_name=""):
    with get_db() as c:
        if assignee_uid:
            return db_fetchall(c, "SELECT * FROM tasks WHERE chat_id=? AND status='pending' AND assigned_to_user_id=? ORDER BY created_at",(cid,assignee_uid))
        elif assignee_name:
            return db_fetchall(c, "SELECT * FROM tasks WHERE chat_id=? AND status='pending' AND assigned_to=? ORDER BY created_at",(cid,assignee_name))
        return []

def get_tasks_by_person(cid, uid="", name=""):
    with get_db() as c:
        if uid:
            return db_fetchall(c,
                "SELECT * FROM tasks WHERE chat_id=? AND status='pending' AND (added_by_user_id=? OR assigned_to_user_id=?) ORDER BY created_at",
                (cid,uid,uid))
        elif name:
            pat = "%{}%".format(name)
            return db_fetchall(c,
                "SELECT * FROM tasks WHERE chat_id=? AND status='pending' AND (added_by LIKE ? OR assigned_to LIKE ?) ORDER BY created_at",
                (cid,pat,pat))
        return []

def find_member_by_name(cid, name_query):
    with get_db() as c:
        pat = "%{}%".format(name_query)
        return db_fetchall(c, "SELECT user_id, display_name FROM chat_members WHERE chat_id=? AND display_name LIKE ?",(cid,pat))

def complete_task(tid, by_name="", by_uid=""):
    result = None
    with get_db() as c:
        r = db_fetchone(c, "SELECT * FROM tasks WHERE id=?",(tid,))
        if r and r["status"]=="pending":
            db_exec(c, "UPDATE tasks SET status='done',completed_at=? WHERE id=?",(datetime.now().isoformat(),tid))
            result = r
    if result:
        try: log_activity(tid, result["chat_id"], by_name, by_uid, "completed", "ทำเสร็จ: {}".format(result["title"]))
        except Exception as e: app.logger.error("log_activity err (complete): %s", e)
    return result

def edit_task(tid, new_title, by_name="", by_uid=""):
    result = None
    with get_db() as c:
        r = db_fetchone(c, "SELECT * FROM tasks WHERE id=?",(tid,))
        if r:
            db_exec(c, "UPDATE tasks SET title=? WHERE id=?",(new_title.strip(),tid))
            result = {"id":tid,"old":r["title"],"new":new_title.strip(),"chat_id":r["chat_id"]}
    if result:
        try: log_activity(tid, result["chat_id"], by_name, by_uid, "edited", "แก้ไข: {} → {}".format(result["old"], result["new"]))
        except Exception as e: app.logger.error("log_activity err (edit): %s", e)
    return result

def delete_task(tid, by_name="", by_uid=""):
    result = None
    with get_db() as c:
        r = db_fetchone(c, "SELECT * FROM tasks WHERE id=?",(tid,))
        if r:
            result = r
            db_exec(c, "DELETE FROM comments WHERE task_id=?",(tid,))
            db_exec(c, "DELETE FROM tasks WHERE id=?",(tid,))
    if result:
        try: log_activity(tid, result["chat_id"], by_name, by_uid, "deleted", "ลบงาน: {}".format(result["title"]))
        except Exception as e: app.logger.error("log_activity err (delete): %s", e)
    return result

def get_active_chats():
    with get_db() as c:
        rows = db_fetchall(c, "SELECT DISTINCT chat_id FROM tasks WHERE status='pending'")
        return [r["chat_id"] for r in rows]

def register_member(cid, uid, name=""):
    with get_db() as c:
        if USE_PG:
            db_exec(c, "INSERT INTO chat_members VALUES(%s,%s,%s) ON CONFLICT(chat_id,user_id) DO UPDATE SET display_name=%s",(cid,uid,name,name))
        else:
            db_exec(c, "INSERT OR REPLACE INTO chat_members VALUES(?,?,?)",(cid,uid,name))

def get_task_index(cid, tid):
    for i,t in enumerate(get_pending_tasks(cid),1):
        if t["id"]==tid: return i
    return 0

# ── Comments ─────────────────────────────────────────────────
def add_comment(tid, cid, author, author_uid, content):
    with get_db() as c:
        db_exec(c, "INSERT INTO comments(task_id,chat_id,author,author_user_id,content) VALUES(?,?,?,?,?)",(tid,cid,author,author_uid,content))
    log_activity(tid, cid, author, author_uid, "commented", content[:50])

def get_comments(tid):
    with get_db() as c:
        return db_fetchall(c, "SELECT * FROM comments WHERE task_id=? ORDER BY created_at",(tid,))

# ── Quick Reply ──────────────────────────────────────────────
def qr():
    return {"items":[
        {"type":"action","action":{"type":"postback","label":"➕ เพิ่มงาน","data":"action=add_prompt","displayText":"➕ เพิ่มงาน"}},
        {"type":"action","action":{"type":"postback","label":"📋 ดูงาน","data":"action=list","displayText":"📋 ดูงาน"}},
        {"type":"action","action":{"type":"postback","label":"📊 สรุป","data":"action=summary","displayText":"📊 สรุป"}},
        {"type":"action","action":{"type":"message","label":"🌅 เข้างาน","text":"เข้างาน"}},
        {"type":"action","action":{"type":"message","label":"🌙 เลิกงาน","text":"เลิกงาน"}},
        {"type":"action","action":{"type":"postback","label":"❓ วิธีใช้","data":"action=help","displayText":"❓ วิธีใช้"}},
    ]}
def aqr(msg):
    if isinstance(msg, str): msg = {"type":"text","text":msg}
    if isinstance(msg, dict) and "quickReply" not in msg: msg["quickReply"] = qr()
    return msg

def task_page_url(tid):
    if APP_URL: return APP_URL.rstrip("/")+"/liff/task?task_id={}".format(tid)
    if LIFF_ID: return "https://liff.line.me/{}?task_id={}".format(LIFF_ID, tid)
    return None

# ── Flex Cards ───────────────────────────────────────────────
def build_mini_card(task, idx):
    tid = task["id"]; cc = len(get_comments(tid)); by = task.get("added_by","") or "-"
    assign = task.get("assigned_to","")
    lu = task_page_url(tid)
    if lu:
        footer_contents=[{"type":"button","action":{"type":"uri","label":"📖 เปิดดู / จัดการ","uri":lu},"style":"primary","height":"sm","color":"#1DB446"}]
    else:
        footer_contents=[
            {"type":"button","action":{"type":"postback","label":"📖 เปิดดู","data":"action=view_task&task_id={}".format(tid)},"style":"primary","height":"sm","color":"#1DB446"},
            {"type":"button","action":{"type":"postback","label":"✅ เสร็จ","data":"action=done&task_id={}".format(tid)},"style":"secondary","height":"sm","margin":"sm"}]
    comments=get_comments(tid)
    body_contents=[
        {"type":"text","text":"สั่งโดย: {}".format(by),"size":"xs","color":"#888888"}]
    if assign:
        body_contents.append({"type":"text","text":"👤 ผู้รับผิดชอบ: {}".format(assign),"size":"xs","color":"#E65100","weight":"bold","margin":"xs"})
    body_contents.append({"type":"text","text":"💬 {} comment".format(cc),"size":"xs","color":"#666666","margin":"sm"})
    if comments:
        c=comments[-1]; ts=""
        if c.get("created_at"):
            try: ts=datetime.fromisoformat(c["created_at"]).strftime("%H:%M")
            except: pass
        body_contents.append({"type":"box","layout":"vertical","contents":[
            {"type":"box","layout":"horizontal","contents":[
                {"type":"text","text":c.get("author","") or "?","size":"xxs","color":"#1DB446","weight":"bold","flex":4},
                {"type":"text","text":ts or "-","size":"xxs","color":"#AAAAAA","flex":1,"align":"end"}]},
            {"type":"text","text":c["content"],"size":"xs","color":"#333333","wrap":True,"margin":"xs"}
        ],"margin":"sm","paddingAll":"6px","backgroundColor":"#F8F8F8","cornerRadius":"6px"})
    return {"type":"bubble","size":"kilo",
        "header":{"type":"box","layout":"horizontal","contents":[
            {"type":"text","text":"#{} ⬜".format(idx),"weight":"bold","color":"#1DB446","size":"sm","flex":0},
            {"type":"text","text":task["title"],"weight":"bold","size":"sm","wrap":True,"flex":5,"margin":"sm"},
        ],"paddingAll":"12px","backgroundColor":"#F5FFF5"},
        "body":{"type":"box","layout":"vertical","contents":body_contents,"paddingAll":"10px"},
        "footer":{"type":"box","layout":"horizontal","contents":footer_contents,"paddingAll":"10px"}}

def build_full_card(task):
    tid=task["id"];cid=task["chat_id"];idx=get_task_index(cid,tid);by=task.get("added_by","") or "ไม่ระบุ"
    assign=task.get("assigned_to","")
    ca=""
    if task.get("created_at"):
        try: ca=datetime.fromisoformat(task["created_at"]).strftime("%d/%m %H:%M")
        except: pass
    body=[
        {"type":"box","layout":"horizontal","contents":[
            {"type":"text","text":"สั่งโดย:","size":"xs","color":"#888888","flex":2},
            {"type":"text","text":by,"size":"xs","color":"#333333","flex":5,"weight":"bold"}]}]
    if assign:
        body.append({"type":"box","layout":"horizontal","contents":[
            {"type":"text","text":"👤 ผู้รับผิดชอบ:","size":"xs","color":"#E65100","flex":2},
            {"type":"text","text":assign,"size":"xs","color":"#E65100","flex":5,"weight":"bold"}],"margin":"sm"})
    body.append({"type":"box","layout":"horizontal","contents":[
            {"type":"text","text":"เมื่อ:","size":"xs","color":"#888888","flex":2},
            {"type":"text","text":ca or "-","size":"xs","color":"#333333","flex":5}],"margin":"sm"})
    comments=get_comments(tid)
    body.append({"type":"separator","margin":"lg"})
    body.append({"type":"text","text":"💬 ความคิดเห็น ({})".format(len(comments)),"size":"sm","weight":"bold","color":"#1DB446","margin":"lg"})
    if comments:
        # แสดงเฉพาะ comment ล่าสุด
        c = comments[-1]
        ts=""
        if c.get("created_at"):
            try: ts=datetime.fromisoformat(c["created_at"]).strftime("%H:%M")
            except: pass
        body.append({"type":"box","layout":"vertical","contents":[
            {"type":"box","layout":"horizontal","contents":[
                {"type":"text","text":c.get("author","") or "?","size":"xxs","color":"#1DB446","weight":"bold","flex":4},
                {"type":"text","text":ts or "-","size":"xxs","color":"#AAAAAA","flex":1,"align":"end"}]},
            {"type":"text","text":c["content"],"size":"xs","color":"#333333","wrap":True,"margin":"xs"}
        ],"margin":"md","paddingAll":"8px","backgroundColor":"#F8F8F8","cornerRadius":"8px"})
        if len(comments)>1:
            body.append({"type":"text","text":"... อีก {} comment ก่อนหน้า".format(len(comments)-1),"size":"xxs","color":"#AAAAAA","margin":"sm","align":"center"})
    else:
        body.append({"type":"text","text":"ยังไม่มี comment","size":"xs","color":"#AAAAAA","margin":"md"})
    # log preview
    logs=get_activity_log(tid,3)
    if logs:
        body.append({"type":"separator","margin":"lg"})
        body.append({"type":"text","text":"📋 Log ล่าสุด","size":"xs","weight":"bold","color":"#888888","margin":"lg"})
        for l in logs:
            lt=""
            if l.get("created_at"):
                try: lt=datetime.fromisoformat(l["created_at"]).strftime("%d/%m %H:%M")
                except: pass
            body.append({"type":"text","text":"{} {} — {}".format(lt,l.get("user_name","?"),l.get("detail","")[:30]),"size":"xxs","color":"#AAAAAA","margin":"xs","wrap":True})

    lu=task_page_url(tid)
    if lu:
        # มี LIFF → ปุ่มเดียว ทุกอย่างจัดการใน LIFF
        footer=[
            {"type":"button","action":{"type":"uri","label":"📖 เปิดจัดการ (แก้ไข / comment / เสร็จ / ลบ)","uri":lu},"style":"primary","height":"sm","color":"#1DB446"}]
    else:
        # ไม่มี LIFF → fallback ปุ่มเยอะในแชท
        footer=[
            {"type":"box","layout":"horizontal","contents":[
                {"type":"button","action":{"type":"postback","label":"✅ เสร็จแล้ว","data":"action=confirm_done&task_id={}".format(tid)},"style":"primary","height":"sm","color":"#1DB446"},
                {"type":"button","action":{"type":"postback","label":"✏️ แก้ไข","data":"action=edit_prompt&task_id={}".format(tid)},"style":"secondary","height":"sm","margin":"sm"}]},
            {"type":"box","layout":"horizontal","contents":[
                {"type":"button","action":{"type":"postback","label":"💬 Comment","data":"action=comment_prompt&task_id={}".format(tid)},"style":"secondary","height":"sm"},
                {"type":"button","action":{"type":"postback","label":"🗑️ ลบ","data":"action=confirm_delete&task_id={}".format(tid)},"style":"secondary","height":"sm","margin":"sm"}],"margin":"sm"}]
        if task.get("added_by_user_id"):
            footer.append({"type":"button","action":{"type":"postback","label":"🙋 ถามคนสั่ง ({})".format(by[:8]),"data":"action=ask_owner&task_id={}".format(tid)},"style":"secondary","height":"sm","margin":"sm"})

    return {"type":"bubble","size":"mega",
        "header":{"type":"box","layout":"vertical","contents":[
            {"type":"box","layout":"horizontal","contents":[
                {"type":"text","text":"#{} ⬜".format(idx or tid),"size":"sm","color":"#1DB446","weight":"bold","flex":0},
                {"type":"text","text":task["title"],"size":"md","weight":"bold","wrap":True,"flex":5,"margin":"md"}]}
        ],"paddingAll":"15px","backgroundColor":"#F5FFF5"},
        "body":{"type":"box","layout":"vertical","contents":body,"paddingAll":"15px","spacing":"xs"},
        "footer":{"type":"box","layout":"vertical","contents":footer,"paddingAll":"10px"}}

def build_task_flex(tid):
    t=get_task(tid)
    if not t: return aqr("❌ ไม่พบงานนี้")
    return aqr({"type":"flex","altText":"📋 {}".format(t["title"]),"contents":build_full_card(t)})

def build_list_flex(cid):
    p=get_pending_tasks(cid)
    if not p: return aqr("🎉 ไม่มีงานค้าง!")
    if len(p)==1:
        lu=task_page_url(p[0]["id"])
        if lu: return aqr({"type":"flex","altText":"📋 งานค้าง 1 งาน","contents":build_mini_card(p[0],1)})
        return build_task_flex(p[0]["id"])
    return aqr({"type":"flex","altText":"📋 งานค้าง {} งาน".format(len(p)),"contents":{"type":"carousel","contents":[build_mini_card(t,i) for i,t in enumerate(p[:10],1)]}})

# ── Summary with checkboxes ─────────────────────────────────
def build_summary(cid):
    now=datetime.now(); done=get_completed_today(cid); pend=get_pending_tasks(cid)
    di=[{"type":"text","text":"  ✔️ {}".format(t["title"]),"size":"xs","color":"#1DB446","wrap":True} for t in done] or [{"type":"text","text":"  — ยังไม่มี","size":"xs","color":"#999999"}]
    # pending items with done+delete buttons — direct action, no confirm
    pi=[]
    for i,t in enumerate(pend,1):
        assign=t.get("assigned_to","")
        label="{}. {}".format(i,t["title"])
        if assign: label="{}. {} [{}]".format(i,t["title"],assign)
        pi.append({"type":"box","layout":"horizontal","contents":[
            {"type":"button","action":{"type":"postback","label":"☑️","data":"action=done_refresh&task_id={}".format(t["id"])},"style":"secondary","height":"sm","flex":0,"gravity":"center"},
            {"type":"text","text":label,"size":"xs","color":"#FF6B35","wrap":True,"flex":5,"gravity":"center","margin":"sm"},
            {"type":"button","action":{"type":"postback","label":"🗑️","data":"action=delete_refresh&task_id={}".format(t["id"])},"style":"secondary","height":"sm","flex":0,"gravity":"center","margin":"sm"},
        ],"margin":"sm"})
    if not pi: pi.append({"type":"text","text":"  — ไม่มีงานค้าง! 🎉","size":"xs","color":"#1DB446"})
    if done and not pend: st,sc="🏆 ยอดเยี่ยม!","#1DB446"
    elif done: st,sc="👍 เสร็จ {} ค้าง {}".format(len(done),len(pend)),"#FF8C00"
    else: st,sc="💪 พรุ่งนี้สู้ใหม่!","#FF6B35"
    return aqr({"type":"flex","altText":"📊 สรุป","contents":{"type":"bubble","size":"mega",
        "header":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"📊 สรุปประจำวัน","weight":"bold","size":"lg","color":"#333333"},
            {"type":"text","text":now.strftime("%d/%m/%Y"),"size":"sm","color":"#999999"},
        ],"paddingAll":"15px","backgroundColor":"#FFF9E6"},
        "body":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"✅ เสร็จวันนี้ ({})".format(len(done)),"weight":"bold","size":"sm","color":"#1DB446"},
        ]+di+[
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"⏳ งานค้าง ({}) — กด ☑️ เสร็จ / 🗑️ ลบ".format(len(pend)),"weight":"bold","size":"sm","color":"#FF6B35","margin":"lg"},
        ]+pi+[
            {"type":"separator","margin":"lg"},
            {"type":"text","text":st,"size":"sm","color":sc,"weight":"bold","margin":"lg","align":"center"},
        ],"paddingAll":"15px","spacing":"sm"}}})

def build_clockin(cid):
    now=datetime.now(); pend=get_pending_tasks(cid)
    items=[]
    for i,t in enumerate(pend,1):
        items.append({"type":"box","layout":"horizontal","contents":[
            {"type":"text","text":"{}".format(i),"size":"sm","color":"#1DB446","flex":0,"weight":"bold"},
            {"type":"text","text":"⬜ {}".format(t["title"]),"size":"sm","wrap":True,"margin":"md"}],"margin":"md"})
    if not items: items.append({"type":"text","text":"🎉 ไม่มีงานค้าง!","size":"sm","color":"#1DB446"})
    return aqr({"type":"flex","altText":"🌅 เข้างาน","contents":{"type":"bubble",
        "header":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"🌅 สวัสดีตอนเช้า!","weight":"bold","size":"lg","color":"#1DB446"},
            {"type":"text","text":"📅 {} เวลา {} น.".format(now.strftime("%d/%m/%Y"),now.strftime("%H:%M")),"size":"sm","color":"#666666","margin":"sm"}
        ],"paddingAll":"15px","backgroundColor":"#F5FFF5"},
        "body":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"📋 งานวันนี้ ({} งาน)".format(len(pend)),"weight":"bold","size":"sm"},
            {"type":"separator","margin":"md"}]+items+[
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"💪 สู้ๆ นะครับ!","size":"sm","color":"#1DB446","margin":"lg","align":"center"}
        ],"paddingAll":"15px"},
        "footer":{"type":"box","layout":"horizontal","contents":[
            {"type":"button","action":{"type":"postback","label":"📋 ดูงาน","data":"action=list"},"style":"primary","height":"sm","color":"#1DB446"},
            {"type":"button","action":{"type":"postback","label":"➕ เพิ่มงาน","data":"action=add_prompt"},"style":"secondary","height":"sm","margin":"sm"}
        ],"paddingAll":"10px"}}})

def build_help():
    return aqr({"type":"flex","altText":"📖 วิธีใช้","contents":{"type":"bubble",
        "header":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"📖 วิธีใช้ Todo Bot","weight":"bold","size":"lg","color":"#1DB446"}
        ],"paddingAll":"15px","backgroundColor":"#F5FFF5"},
        "body":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"🔘 กดปุ่มด้านล่างได้เลย!","weight":"bold","size":"sm","color":"#FF6B35"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"➕ เพิ่มงาน → พิมพ์แค่ชื่องาน","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"📖 เปิดดู → หน้าจัดการงาน LIFF","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"text","text":"แก้ไข / comment / ถามคนสั่ง / ดู log","size":"xs","color":"#666666","margin":"sm"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"📊 สรุป → กด ☑️ เสร็จ / 🗑️ ลบ ได้เลย","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"@ดูงาน → ดูงานค้างของตัวเอง","weight":"bold","size":"sm","color":"#1565C0","margin":"lg"},
            {"type":"text","text":"@ชื่อ งาน → ดูงานค้างของคนนั้น","size":"xs","color":"#666666","margin":"sm"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"📋 Activity Log ทุกงาน","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"text","text":"ดูย้อนหลังว่าใครทำอะไรเมื่อไหร่","size":"xs","color":"#666666","margin":"sm"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"⏰ auto สรุปทุกวัน 18:00","size":"xs","color":"#999999","margin":"lg","align":"center"},
        ],"paddingAll":"15px"}}})

def build_person_tasks(person_name, tasks, is_self=False):
    """Build Flex card showing a person's pending tasks"""
    label = "📋 งานของฉัน" if is_self else "📋 งานของ {}".format(person_name)
    if not tasks:
        return aqr({"type":"flex","altText":label,"contents":{"type":"bubble","size":"kilo",
            "body":{"type":"box","layout":"vertical","backgroundColor":"#FFF8E1","cornerRadius":"lg","paddingAll":"lg","contents":[
                {"type":"text","text":"📋 ไม่พบงานค้าง","weight":"bold","size":"md","color":"#F57F17","align":"center"},
                {"type":"text","text":"ของ {}".format("คุณ" if is_self else person_name),"size":"sm","color":"#666","align":"center","margin":"sm"}
            ]}}})
    items = []
    for i, t in enumerate(tasks, 1):
        lu = task_page_url(t["id"])
        row = [
            {"type":"text","text":"{}. {}".format(i, t["title"]),"size":"sm","weight":"bold","color":"#333","flex":4,"wrap":True},
        ]
        if t.get("added_by"): row.append({"type":"text","text":"by {}".format(t["added_by"]),"size":"xxs","color":"#999","flex":2,"align":"end"})
        item = {"type":"box","layout":"horizontal","contents":row,"margin":"md"}
        if lu:
            item["action"] = {"type":"uri","label":"เปิด","uri":lu}
        items.append(item)
        if i < len(tasks): items.append({"type":"separator","margin":"sm","color":"#E0E0E0"})
    return aqr({"type":"flex","altText":label,"contents":{"type":"bubble",
        "header":{"type":"box","layout":"vertical","backgroundColor":"#E3F2FD","paddingAll":"12px","contents":[
            {"type":"text","text":label,"weight":"bold","size":"md","color":"#1565C0"},
            {"type":"text","text":"📌 {} งานค้าง".format(len(tasks)),"size":"xs","color":"#666","margin":"sm"}
        ]},
        "body":{"type":"box","layout":"vertical","paddingAll":"12px","contents":items}}})

# ── Text Commands ────────────────────────────────────────────
CANCEL=["ยกเลิก","cancel","ไม่","no"]
CMDS=["เพิ่ม","add","todo","เพิ่มงาน","งานค้าง","ดูงาน","list","tasks","สรุป","summary","เข้างาน","clock in","เลิกงาน","off","งานงาน","วิธีใช้","เมนู","menu","@ดูงาน"]

def process_text(text, cid, uid="", name=""):
    ts=text.strip(); tl=ts.lower()
    pa=get_pending(uid,cid)
    if pa:
        clear_pending(uid,cid)
        if tl in CANCEL: return aqr("❌ ยกเลิกแล้ว")
        is_cmd=any(tl==c or tl.startswith(c+" ") for c in CMDS) or any(tl.startswith(p) for p in ["note ","แก้ ","เสร็จ","ลบ","log "])
        if not is_cmd:
            if pa["action"]=="waiting_add":
                lines=[l.strip() for l in ts.split("\n") if l.strip()]
                if len(lines)==1:
                    t=add_task(cid,lines[0],by=name,by_uid=uid); return build_task_flex(t["id"])
                else:
                    added=[]
                    for l in lines:
                        t=add_task(cid,l,by=name,by_uid=uid); added.append(t)
                    return aqr("✅ เพิ่ม {} งานแล้ว:\n{}".format(len(added),"\n".join("• {}".format(a["title"]) for a in added)))
            elif pa["action"]=="waiting_edit" and pa["data"]:
                edit_task(int(pa["data"]),ts,name,uid); return build_task_flex(int(pa["data"]))
            elif pa["action"]=="waiting_comment" and pa["data"]:
                tid=int(pa["data"]); t=get_task(tid)
                if t and t["chat_id"]==cid: add_comment(tid,cid,name,uid,ts); return build_task_flex(tid)
                return aqr("❌ ไม่พบงานนี้")

    if tl in ["เข้างาน","clock in","เริ่มงาน"]: return build_clockin(cid)
    if tl in ["เลิกงาน","off","ออก","เลิก"]: return build_summary(cid)

    # @ดูงาน → ดูงานค้างของตัวเอง
    if tl in ["@ดูงาน","@งานของฉัน","@mywork","@mytasks","งานของฉัน"]:
        tasks = get_tasks_by_person(cid, uid=uid)
        return build_person_tasks(name or "ฉัน", tasks, is_self=True)

    # @ชื่อ งาน → ดูงานค้างของคนนั้น
    m = re.match(r"^@(.+?)\s*(?:งาน|tasks?|work)$", ts)
    if m:
        query_name = m.group(1).strip()
        members = find_member_by_name(cid, query_name)
        if members:
            mem = members[0]
            tasks = get_tasks_by_person(cid, uid=mem["user_id"])
            return build_person_tasks(mem["display_name"], tasks)
        else:
            tasks = get_tasks_by_person(cid, name=query_name)
            return build_person_tasks(query_name, tasks)

    m=re.match(r"^(?:เพิ่ม|add|todo)\s+(.+)",ts,re.I|re.S)
    if m:
        raw=m.group(1).strip()
        lines=[l.strip() for l in raw.split("\n") if l.strip()]
        if len(lines)==1:
            t=add_task(cid,lines[0],by=name,by_uid=uid); return build_task_flex(t["id"])
        else:
            added=[]
            for l in lines:
                t=add_task(cid,l,by=name,by_uid=uid); added.append(t)
            return aqr("✅ เพิ่ม {} งานแล้ว:\n{}".format(len(added),"\n".join("• {}".format(a["title"]) for a in added)))
    if tl in ["เพิ่ม","add","todo","เพิ่มงาน"]:
        set_pending(uid,cid,"waiting_add"); return aqr("📝 พิมพ์ชื่องานเลยครับ\n(พิมพ์ \"ยกเลิก\" เพื่อยกเลิก)")
    if tl in ["งานค้าง","ดูงาน","list","tasks","รายการ","ดู"]: return build_list_flex(cid)

    # log command
    m=re.match(r"^(?:log|ประวัติ)\s*(\d+)",ts,re.I)
    if m:
        tid=int(m.group(1)); t=get_task(tid)
        if not t: return aqr("❌ ไม่พบงาน #{}".format(tid))
        logs=get_activity_log(tid,10)
        if not logs: return aqr("📋 ยังไม่มี log สำหรับงาน: {}".format(t["title"]))
        lines=["📋 Activity Log: {}".format(t["title"]),""]
        for l in logs:
            lt=""
            if l.get("created_at"):
                try: lt=datetime.fromisoformat(l["created_at"]).strftime("%d/%m %H:%M")
                except: pass
            icon={"created":"🆕","edited":"✏️","commented":"💬","completed":"✅","deleted":"🗑️"}.get(l["action"],"📌")
            lines.append("{} {} {} — {}".format(icon,lt,l.get("user_name","?"),l.get("detail","")))
        return aqr("\n".join(lines))

    m=re.match(r"^(?:แก้|edit)\s+(\d+)\s+(.+)",ts,re.I)
    if m:
        p=get_pending_tasks(cid);n=int(m.group(1))
        if 1<=n<=len(p): edit_task(p[n-1]["id"],m.group(2).strip(),name,uid); return build_task_flex(p[n-1]["id"])
        return aqr("❌ ไม่พบงาน #{}".format(n))
    m=re.match(r"^(?:เสร็จ|done|✅)\s*(\d+)",ts,re.I)
    if m:
        p=get_pending_tasks(cid);n=int(m.group(1))
        if 1<=n<=len(p): complete_task(p[n-1]["id"],name,uid); return aqr("✅ เสร็จ! ✔️ {}\n📌 เหลือ {} งาน".format(p[n-1]["title"],len(get_pending_tasks(cid))))
        return aqr("❌ ไม่พบงาน #{}".format(n))
    m=re.match(r"^(?:ลบ|delete|remove)\s*(\d+)",ts,re.I)
    if m:
        p=get_pending_tasks(cid);n=int(m.group(1))
        if 1<=n<=len(p): delete_task(p[n-1]["id"],name,uid); return aqr("🗑️ ลบแล้ว: {}".format(p[n-1]["title"]))
        return aqr("❌ ไม่พบงาน #{}".format(n))
    m=re.match(r"^(?:note|โน้ต|คอมเม้น|comment)\s+(\d+)\s+(.+)",ts,re.I)
    if m:
        ref=int(m.group(1));t=get_task(ref)
        if not t or t["chat_id"]!=cid:
            p=get_pending_tasks(cid)
            if 1<=ref<=len(p): t=p[ref-1]
        if t: add_comment(t["id"],cid,name,uid,m.group(2).strip()); return build_task_flex(t["id"])
        return aqr("❌ ไม่พบงาน #{}".format(ref))
    if tl in ["สรุป","summary","รายงาน","report"]: return build_summary(cid)
    if tl in ["งานงาน","วิธีใช้","ช่วย","คำสั่ง","?","เมนู","menu"]: return build_help()
    return None

# ── Postback ─────────────────────────────────────────────────
def handle_pb(data, cid, tok, uid="", name=""):
    p={}
    for part in data.split("&"):
        if "=" in part: k,v=part.split("=",1); p[k]=v
    act=p.get("action",""); tid=p.get("task_id","")

    if act=="add_prompt":
        set_pending(uid,cid,"waiting_add"); reply_msg(tok,aqr("📝 พิมพ์ชื่องานเลยครับ\n(พิมพ์ \"ยกเลิก\" เพื่อยกเลิก)"))

    elif act=="view_task" and tid: reply_msg(tok, build_task_flex(int(tid)))

    # ── ยืนยันก่อนทำเสร็จ ──
    elif act=="confirm_done" and tid:
        t=get_task(int(tid))
        if t:
            reply_msg(tok, aqr({"type":"flex","altText":"ยืนยันเสร็จ?","contents":{"type":"bubble","size":"kilo",
                "body":{"type":"box","layout":"vertical","contents":[
                    {"type":"text","text":"✅ ยืนยันว่างานนี้เสร็จ?","weight":"bold","size":"sm","color":"#1DB446"},
                    {"type":"text","text":t["title"],"size":"sm","wrap":True,"margin":"md","color":"#333333"},
                ],"paddingAll":"15px"},
                "footer":{"type":"box","layout":"horizontal","contents":[
                    {"type":"button","action":{"type":"postback","label":"✅ ยืนยัน เสร็จแล้ว","data":"action=done&task_id={}".format(tid)},"style":"primary","height":"sm","color":"#1DB446"},
                    {"type":"button","action":{"type":"postback","label":"❌ ยกเลิก","data":"action=cancel"},"style":"secondary","height":"sm","margin":"sm"},
                ],"paddingAll":"10px"}}}))
        else: reply_msg(tok, aqr("❌ ไม่พบงานนี้"))

    elif act=="done" and tid:
        t=complete_task(int(tid),name,uid)
        if t: reply_msg(tok,aqr("✅ เสร็จแล้ว!\n✔️ {}\n📌 เหลือ {} งาน".format(t["title"],len(get_pending_tasks(cid)))))
        else: reply_msg(tok,aqr("❌ งานนี้เสร็จไปแล้ว"))

    # ── done/delete จากหน้าสรุป → ทำเลย + ตอบ Flex card ──
    elif act=="done_refresh" and tid:
        try:
            t=complete_task(int(tid),name,uid)
            if t:
                pend=get_pending_tasks(cid)
                reply_msg(tok,aqr({"type":"flex","altText":"✅ เสร็จ: {}".format(t["title"]),"contents":{"type":"bubble","size":"kilo",
                    "body":{"type":"box","layout":"vertical","backgroundColor":"#E8F5E9","cornerRadius":"lg","paddingAll":"lg","contents":[
                        {"type":"text","text":"✅ เสร็จแล้ว!","weight":"bold","size":"lg","color":"#2E7D32","align":"center"},
                        {"type":"text","text":t["title"],"size":"md","color":"#333333","align":"center","margin":"md","wrap":True},
                        {"type":"separator","margin":"lg","color":"#C8E6C9"},
                        {"type":"text","text":"📌 เหลือ {} งาน".format(len(pend)),"size":"sm","color":"#666666","align":"center","margin":"md"}
                    ]}}}))
            else:
                reply_msg(tok,aqr({"type":"flex","altText":"งานนี้เสร็จไปแล้ว","contents":{"type":"bubble","size":"kilo",
                    "body":{"type":"box","layout":"vertical","backgroundColor":"#FFF3E0","cornerRadius":"lg","paddingAll":"lg","contents":[
                        {"type":"text","text":"⚠️ งานนี้เสร็จไปแล้ว","weight":"bold","size":"sm","color":"#E65100","align":"center"}
                    ]}}}))
        except Exception as e:
            app.logger.error("done_refresh err: %s",e)
            reply_msg(tok,aqr("❌ error: {}".format(str(e))))
    elif act=="delete_refresh" and tid:
        try:
            t=delete_task(int(tid),name,uid)
            if t:
                pend=get_pending_tasks(cid)
                reply_msg(tok,aqr({"type":"flex","altText":"🗑️ ลบแล้ว: {}".format(t["title"]),"contents":{"type":"bubble","size":"kilo",
                    "body":{"type":"box","layout":"vertical","backgroundColor":"#FFEBEE","cornerRadius":"lg","paddingAll":"lg","contents":[
                        {"type":"text","text":"🗑️ ลบงานแล้ว","weight":"bold","size":"lg","color":"#C62828","align":"center"},
                        {"type":"text","text":t["title"],"size":"md","color":"#333333","align":"center","margin":"md","wrap":True,"decoration":"line-through"},
                        {"type":"separator","margin":"lg","color":"#FFCDD2"},
                        {"type":"text","text":"📌 เหลือ {} งาน".format(len(pend)),"size":"sm","color":"#666666","align":"center","margin":"md"}
                    ]}}}))
            else:
                reply_msg(tok,aqr({"type":"flex","altText":"ไม่พบงานนี้","contents":{"type":"bubble","size":"kilo",
                    "body":{"type":"box","layout":"vertical","backgroundColor":"#FFF3E0","cornerRadius":"lg","paddingAll":"lg","contents":[
                        {"type":"text","text":"⚠️ ไม่พบงานนี้","weight":"bold","size":"sm","color":"#E65100","align":"center"}
                    ]}}}))
        except Exception as e:
            app.logger.error("delete_refresh err: %s",e)
            reply_msg(tok,aqr("❌ error: {}".format(str(e))))

    # ── ยืนยันก่อนลบ ──
    elif act=="confirm_delete" and tid:
        t=get_task(int(tid))
        if t:
            reply_msg(tok, aqr({"type":"flex","altText":"ยืนยันลบ?","contents":{"type":"bubble","size":"kilo",
                "body":{"type":"box","layout":"vertical","contents":[
                    {"type":"text","text":"⚠️ ยืนยันลบงานนี้?","weight":"bold","size":"sm","color":"#E53935"},
                    {"type":"text","text":t["title"],"size":"sm","wrap":True,"margin":"md","color":"#333333"},
                    {"type":"text","text":"ลบแล้วกู้คืนไม่ได้!","size":"xs","color":"#999999","margin":"sm"},
                ],"paddingAll":"15px"},
                "footer":{"type":"box","layout":"horizontal","contents":[
                    {"type":"button","action":{"type":"postback","label":"🗑️ ยืนยัน ลบเลย","data":"action=delete&task_id={}".format(tid)},"style":"primary","height":"sm","color":"#E53935"},
                    {"type":"button","action":{"type":"postback","label":"❌ ยกเลิก","data":"action=cancel"},"style":"secondary","height":"sm","margin":"sm"},
                ],"paddingAll":"10px"}}}))
        else: reply_msg(tok, aqr("❌ ไม่พบงานนี้"))

    elif act=="delete" and tid:
        t=delete_task(int(tid),name,uid)
        if t: reply_msg(tok,aqr("🗑️ ลบแล้ว: {}".format(t["title"])))
        else: reply_msg(tok,aqr("❌ ไม่พบงานนี้"))

    elif act=="cancel": reply_msg(tok,aqr("❌ ยกเลิกแล้ว"))

    elif act=="edit_prompt" and tid:
        t=get_task(int(tid))
        if t: set_pending(uid,cid,"waiting_edit",tid); reply_msg(tok,aqr("✏️ แก้ไข: \"{}\"\nพิมพ์ชื่อใหม่เลย".format(t["title"])))
    elif act=="comment_prompt" and tid:
        t=get_task(int(tid))
        if t: set_pending(uid,cid,"waiting_comment",tid); reply_msg(tok,aqr("💬 Comment งาน: \"{}\"\nพิมพ์ข้อความเลย".format(t["title"])))
    elif act=="ask_owner" and tid:
        t=get_task(int(tid))
        if t and t.get("added_by_user_id"):
            owner=t.get("added_by","")
            mention="@{} — มีคนถามเรื่องงาน: \"{}\"".format(owner,t["title"])
            reply_msg(tok,{"type":"text","text":mention,"mention":{"mentionees":[{"index":0,"length":len("@{}".format(owner)),"userId":t["added_by_user_id"]}]},"quickReply":qr()})
        elif t: reply_msg(tok,aqr("❓ งาน: \"{}\"\nสั่งโดย: {}".format(t["title"],t.get("added_by","?"))))
    elif act=="list": reply_msg(tok,build_list_flex(cid))
    elif act=="summary": reply_msg(tok,build_summary(cid))
    elif act=="help": reply_msg(tok,build_help())

# ── Webhook ──────────────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    sig=request.headers.get("X-Line-Signature",""); body=request.get_data(as_text=True)
    app.logger.info("Webhook received: %d bytes", len(body))
    if not verify_sig(body,sig):
        app.logger.error("Signature verification failed")
        abort(400)
    for ev in json.loads(body).get("events",[]):
        try:
            tok=ev.get("replyToken",""); src=ev.get("source",{}); st=src.get("type","")
            cid=src.get("groupId","") if st=="group" else src.get("roomId","") if st=="room" else src.get("userId","")
            uid=src.get("userId",""); name=get_profile(uid) if uid else ""
            if name and cid!=uid: register_member(cid,uid,name)
            if ev.get("type")=="message" and ev.get("message",{}).get("type")=="text":
                msg=ev["message"]; txt=msg.get("text","").strip()
                # ── Parse @mention for task assignment ──
                mention_info=msg.get("mention",{}).get("mentionees",[])
                if mention_info and len(mention_info)>0:
                    m_uid=mention_info[0].get("userId","")
                    m_idx=mention_info[0].get("index",0)
                    m_len=mention_info[0].get("length",0)
                    m_name=txt[m_idx:m_idx+m_len].lstrip("@").strip() if m_idx+m_len<=len(txt) else ""
                    if m_uid and m_name: register_member(cid,m_uid,m_name)
                    rest=txt[m_idx+m_len:].strip()
                    # "@Name เพิ่ม task" → assign task to Name
                    am=re.match(r"^(?:เพิ่ม|add|todo)\s+(.+)",rest,re.I)
                    if am:
                        t=add_task(cid,am.group(1).strip(),by=name,by_uid=uid,assign_to=m_name,assign_to_uid=m_uid)
                        reply_msg(tok,aqr("📌 มอบหมายงาน \"{}\" ให้ {} แล้ว".format(am.group(1).strip(),m_name)))
                        continue
                    # "@Name งาน" → list tasks assigned to Name
                    if rest in ["งาน","tasks","list","ดูงาน",""]:
                        tasks=get_tasks_by_assignee(cid,assignee_uid=m_uid,assignee_name=m_name)
                        if not tasks:
                            reply_msg(tok,aqr("📋 {} ไม่มีงานค้าง 🎉".format(m_name)))
                        elif len(tasks)==1:
                            reply_msg(tok,aqr({"type":"flex","altText":"📋 งานของ {}".format(m_name),"contents":build_mini_card(tasks[0],1)}))
                        else:
                            reply_msg(tok,aqr({"type":"flex","altText":"📋 งานของ {} ({})".format(m_name,len(tasks)),"contents":{"type":"carousel","contents":[build_mini_card(t,i) for i,t in enumerate(tasks[:10],1)]}}))
                        continue
                r=process_text(txt,cid,uid,name)
                if r: reply_msg(tok,r)
            elif ev.get("type")=="postback":
                handle_pb(ev.get("postback",{}).get("data",""),cid,tok,uid,name)
        except Exception as e:
            import traceback
            app.logger.error("Err: %s\n%s", e, traceback.format_exc())
    return "OK"

# ══════════════════════════════════════════════════════════════
# REST API (for LIFF)
# ══════════════════════════════════════════════════════════════
@app.route("/api/task/<int:tid>")
def api_get(tid):
    t=get_task(tid)
    if not t: return jsonify({"error":"not found"}),404
    t["comments"]=get_comments(tid); t["index"]=get_task_index(t["chat_id"],tid)
    t["logs"]=get_activity_log(tid,20)
    return jsonify(t)

@app.route("/api/task/<int:tid>",methods=["PUT"])
def api_edit(tid):
    d=request.get_json() or {}
    r=edit_task(tid,d.get("title",""),d.get("author",""),d.get("author_uid",""))
    if r: return jsonify({"ok":True,"task":get_task(tid)})
    return jsonify({"error":"not found"}),404

@app.route("/api/task/<int:tid>/done",methods=["POST"])
def api_done(tid):
    try:
        d=request.get_json() or {}
        app.logger.info("api_done called: tid=%s, author=%s", tid, d.get("author",""))
        t=complete_task(tid,d.get("author",""),d.get("author_uid",""))
        if t:
            app.logger.info("api_done success: tid=%s", tid)
            return jsonify({"ok":True})
        # check if already done
        existing=get_task(tid)
        if existing and existing.get("status")=="done":
            app.logger.info("api_done already done: tid=%s", tid)
            return jsonify({"ok":True,"already_done":True})
        if existing:
            app.logger.warning("api_done task exists but status=%s: tid=%s", existing.get("status"), tid)
            return jsonify({"error":"task status is '{}', not 'pending'".format(existing.get("status","?"))}),400
        app.logger.warning("api_done task not found: tid=%s", tid)
        return jsonify({"error":"task not found (id={})".format(tid)}),404
    except Exception as e:
        app.logger.error("api_done exception: tid=%s err=%s",tid,e)
        import traceback; traceback.print_exc()
        # check if task got updated despite error
        try:
            existing=get_task(tid)
            if existing and existing.get("status")=="done":
                return jsonify({"ok":True,"recovered":True})
        except: pass
        return jsonify({"error":"server error: {}".format(str(e))}),500

@app.route("/api/task/<int:tid>/delete",methods=["DELETE"])
def api_del(tid):
    try:
        d=request.get_json() or {}
        t=delete_task(tid,d.get("author",""),d.get("author_uid",""))
        if t: return jsonify({"ok":True})
        return jsonify({"error":"not found"}),404
    except Exception as e:
        app.logger.error("api_del err: %s",e)
        return jsonify({"error":str(e)}),500

@app.route("/api/task/<int:tid>/comment",methods=["POST"])
def api_comment(tid):
    d=request.get_json() or {}; t=get_task(tid)
    if not t: return jsonify({"error":"not found"}),404
    add_comment(tid,t["chat_id"],d.get("author",""),d.get("author_uid",""),d.get("content",""))
    return jsonify({"ok":True,"comments":get_comments(tid)})

@app.route("/api/task/<int:tid>/ask-owner",methods=["POST"])
def api_ask(tid):
    t=get_task(tid)
    if not t or not t.get("added_by_user_id"): return jsonify({"error":"no owner"}),400
    owner=t.get("added_by",""); mention="@{} — มีคนถามเรื่องงาน: \"{}\"".format(owner,t["title"])
    push_msg(t["chat_id"],{"type":"text","text":mention,"mention":{"mentionees":[{"index":0,"length":len("@{}".format(owner)),"userId":t["added_by_user_id"]}]}})
    return jsonify({"ok":True})

@app.route("/api/task/<int:tid>/log")
def api_log(tid):
    return jsonify(get_activity_log(tid, 50))

@app.route("/api/upload", methods=["POST"])
def api_upload():
    import uuid
    f = request.files.get("file")
    if not f: return jsonify({"error":"no file"}), 400
    ext = f.filename.rsplit(".",1)[-1] if "." in f.filename else "jpg"
    fname = "{}.{}".format(uuid.uuid4().hex[:12], ext)
    upload_dir = os.path.join(os.path.dirname(DATABASE_PATH) or ".", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    fpath = os.path.join(upload_dir, fname)
    f.save(fpath)
    return jsonify({"url":"/uploads/"+fname})

@app.route("/api/members/<path:cid>")
def api_members(cid):
    with get_db() as c:
        rows = db_fetchall(c, "SELECT user_id,display_name FROM chat_members WHERE chat_id=?",(cid,))
    return jsonify([{"uid":r["user_id"],"name":r["display_name"]} for r in rows])

# ══════════════════════════════════════════════════════════════
# Task Detail Page (No LIFF SDK required)
# ══════════════════════════════════════════════════════════════
TASK_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Task Detail</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#333}
.loading{display:flex;justify-content:center;align-items:center;height:100vh;font-size:18px;color:#1DB446}
.app{display:none;padding-bottom:70px}
.namebox{background:#FFF3CD;padding:12px 16px;text-align:center;display:none}
.namebox input{padding:8px 12px;border:2px solid #1DB446;border-radius:8px;font-size:15px;width:60%}
.namebox button{padding:8px 16px;border:none;border-radius:8px;background:#1DB446;color:#fff;font-weight:bold;font-size:14px;margin-left:6px;cursor:pointer}
.head{background:linear-gradient(135deg,#1DB446,#17a03d);color:#fff;padding:18px 16px;position:sticky;top:0;z-index:10}
.head .idx{font-size:12px;opacity:.7}.head .title{font-size:19px;font-weight:bold;margin:5px 0;cursor:pointer}
.head .title:hover{text-decoration:underline}.head .hint{font-size:10px;opacity:.5}
.head .meta{font-size:11px;opacity:.7;display:flex;gap:12px;margin-top:5px}
.ebox{display:none;margin:8px 0}.ebox input{width:100%;padding:9px;border:2px solid #fff;border-radius:8px;font-size:15px}
.ebox .ebtns{display:flex;gap:6px;margin-top:6px}.ebox button{flex:1;padding:7px;border:none;border-radius:8px;font-weight:bold;font-size:12px;cursor:pointer}
.esave{background:#fff;color:#1DB446}.ecancel{background:rgba(255,255,255,.3);color:#fff}
.section{padding:14px 16px}
.stitle{font-size:14px;font-weight:bold;color:#1DB446;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
.tab-bar{display:flex;gap:0;margin-bottom:12px;border-radius:8px;overflow:hidden;border:1.5px solid #1DB446}
.tab{flex:1;padding:8px;text-align:center;font-size:12px;font-weight:bold;cursor:pointer;background:#fff;color:#1DB446}
.tab.active{background:#1DB446;color:#fff}
.cmt{background:#fff;border-radius:10px;padding:10px;margin-bottom:6px;box-shadow:0 1px 3px rgba(0,0,0,.05);animation:fi .3s ease}
@keyframes fi{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.cmt .ct{display:flex;justify-content:space-between;margin-bottom:3px}.cmt .cn{font-size:11px;font-weight:bold;color:#1DB446}
.cmt .ctm{font-size:10px;color:#aaa}.cmt .cb{font-size:13px;line-height:1.4}
.nocmt{text-align:center;color:#bbb;padding:15px;font-size:13px}
.log-item{background:#fff;border-radius:8px;padding:8px 10px;margin-bottom:4px;font-size:12px;display:flex;gap:8px;align-items:flex-start}
.log-icon{font-size:16px;flex-shrink:0}.log-body{flex:1}.log-user{font-weight:bold;color:#333}.log-detail{color:#666;margin-top:2px}
.log-time{font-size:10px;color:#aaa;flex-shrink:0}
.done-banner{display:none;background:#E8F5E9;padding:16px;text-align:center;border-radius:12px;margin:12px 16px}
.done-banner .done-icon{font-size:42px;margin-bottom:6px}
.done-banner .done-text{font-size:16px;font-weight:bold;color:#1DB446}
.done-banner .done-sub{font-size:12px;color:#666;margin-top:4px}
.actions{padding:8px 16px;display:flex;flex-direction:column;gap:6px}
.arow{display:flex;gap:6px}
.abtn{flex:1;padding:11px;border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;text-align:center}
.a-done{background:#E8F5E9;color:#1DB446}.a-ask{background:#FFF3E0;color:#E65100;border:1px solid #FFB74D}.a-del{background:#FFEBEE;color:#E53935}
.cbar{position:fixed;bottom:0;left:0;right:0;border-top:1px solid #eee;padding:8px 12px;display:flex;gap:8px;background:#fff;z-index:20}
.cbar input{flex:1;padding:9px 14px;border:1.5px solid #ddd;border-radius:22px;font-size:13px;outline:none}.cbar input:focus{border-color:#1DB446}
.cbar button{background:#1DB446;color:#fff;border:none;border-radius:50%;width:38px;height:38px;font-size:16px;cursor:pointer}
.cbar .attach-btn{background:#FF9800;font-size:14px}
.cmt .cb a{color:#1DB446;text-decoration:underline;word-break:break-all}
.cmt .cb img{max-width:100%;border-radius:8px;margin-top:6px;cursor:pointer}
.img-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.9);z-index:200;display:none;justify-content:center;align-items:center;touch-action:pan-x}
.img-overlay img{max-width:95vw;max-height:90vh;object-fit:contain}
.img-overlay .close-x{position:fixed;top:12px;right:16px;color:#fff;font-size:28px;cursor:pointer;z-index:201}
.toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:8px 20px;border-radius:20px;font-size:13px;z-index:100;display:none}
.confirm-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);z-index:50;display:none;justify-content:center;align-items:center}
.confirm-box{background:#fff;border-radius:16px;padding:24px;max-width:300px;text-align:center}
.confirm-box h3{margin-bottom:8px}.confirm-box p{font-size:13px;color:#666;margin-bottom:16px}
.confirm-box .cbtns{display:flex;gap:8px}
.confirm-box .cbtns button{flex:1;padding:10px;border:none;border-radius:10px;font-weight:bold;cursor:pointer;font-size:13px}
</style></head><body>
<div class="loading" id="loading">⏳ กำลังโหลด...</div>
<div class="toast" id="toast"></div>
<div class="confirm-overlay" id="confirmOverlay">
  <div class="confirm-box" id="confirmBox"><h3 id="confirmTitle"></h3><p id="confirmMsg"></p>
    <div class="cbtns"><button id="confirmYes" style="background:#1DB446;color:#fff">ยืนยัน</button>
    <button onclick="hideConfirm()" style="background:#f0f0f0;color:#333">ยกเลิก</button></div>
  </div>
</div>
<div class="app" id="app">
  <div class="namebox" id="nameBox">
    <select id="nameSelect" style="padding:8px;border:2px solid #1DB446;border-radius:8px;font-size:14px;width:65%"><option value="">-- เลือกชื่อ --</option></select>
    <button onclick="pickName()">OK</button>
  </div>
  <div class="head">
    <div class="idx" id="tidx">#1 ⬜</div>
    <div class="title" id="ttitle" onclick="showEdit()">...</div>
    <div class="hint">👆 แตะชื่อเพื่อแก้ไข</div>
    <div class="meta"><span id="tby">สั่งโดย: -</span><span id="tdate">เมื่อ: -</span></div>
    <div class="ebox" id="ebox"><input id="einput"><div class="ebtns"><button class="esave" onclick="saveEdit()">💾 บันทึก</button><button class="ecancel" onclick="hideEdit()">✖ ยกเลิก</button></div></div>
  </div>
  <div class="section">
    <div class="tab-bar"><div class="tab active" onclick="showTab('comments')">💬 Comments</div><div class="tab" onclick="showTab('log')">📋 Activity Log</div></div>
    <div id="commentsTab"></div>
    <div id="logTab" style="display:none"></div>
  </div>
  <div class="done-banner" id="doneBanner">
    <div class="done-icon">✅</div>
    <div class="done-text">งานนี้เสร็จแล้ว!</div>
    <div class="done-sub">ดู comments และ activity log ด้านบน</div>
  </div>
  <div class="actions">
    <div class="arow"><button class="abtn a-done" onclick="confirmDone()">✅ เสร็จแล้ว</button></div>
    <div class="arow"><button class="abtn a-ask" id="askBtn" onclick="askOwner()" style="display:none">🙋 ถามคนสั่ง</button>
    <button class="abtn a-del" onclick="confirmDelete()">🗑️ ลบงาน</button></div>
  </div>
  <div class="cbar"><input id="cinput" placeholder="พิมพ์ comment / วาง link..." onkeypress="if(event.key==='Enter')sendCmt()"><button class="attach-btn" onclick="document.getElementById('fup').click()">📎</button><button onclick="sendCmt()">➤</button></div>
  <input type="file" id="fup" accept="image/*" style="display:none" onchange="uploadImg(this)">
</div>
<div class="img-overlay" id="imgOverlay" onclick="this.style.display='none'">
  <div class="close-x" onclick="document.getElementById('imgOverlay').style.display='none'">✕</div>
  <img id="imgFull" src="">
</div>
<script>
var API="",taskId,task,userName="",members=[];
async function init(){
  var el=document.getElementById("loading");
  taskId=new URLSearchParams(location.search).get("task_id");
  userName=decodeURIComponent(new URLSearchParams(location.search).get("name")||"");
  if(!taskId){el.textContent="ไม่มี task_id";return}
  try{
    await load();
    el.style.display="none";document.getElementById("app").style.display="block";
    if(!userName&&task.chat_id){
      try{var mr=await fetch(API+"/api/members/"+encodeURIComponent(task.chat_id));
      if(mr.ok){members=await mr.json();
        if(members.length>0){var sel=document.getElementById("nameSelect");
          members.forEach(function(m){var o=document.createElement("option");o.value=m.name;o.textContent=m.name;sel.appendChild(o)});
          document.getElementById("nameBox").style.display="block"}
        else{userName="ผู้ใช้"}}}catch(e){userName="ผู้ใช้"}}
    if(userName)document.getElementById("nameBox").style.display="none";
  }catch(e){el.textContent="โหลดไม่ได้: "+e.message}}
function pickName(){var v=document.getElementById("nameSelect").value;
  if(!v)return;userName=v;document.getElementById("nameBox").style.display="none";toast("สวัสดี "+v+" !")}
function gn(){return userName||"ผู้ใช้"}
async function load(){
  var r=await fetch(API+"/api/task/"+taskId);if(!r.ok)throw new Error("Task not found");task=await r.json();render()}
function render(){
  document.getElementById("tidx").textContent="#"+(task.index||task.id)+" "+(task.status==="pending"?"⬜":"✅");
  document.getElementById("ttitle").textContent=task.title;
  document.getElementById("tby").textContent="สั่งโดย: "+(task.added_by||"-");
  var dt="";if(task.created_at){try{var d=new Date(task.created_at);dt=d.toLocaleDateString("th-TH",{day:"2-digit",month:"2-digit"})+" "+d.toLocaleTimeString("th-TH",{hour:"2-digit",minute:"2-digit"})}catch(e){}}
  document.getElementById("tdate").textContent="เมื่อ: "+(dt||"-");
  var b=document.getElementById("askBtn");if(task.added_by_user_id){b.style.display="flex";b.textContent="🙋 ถามคนสั่ง ("+(task.added_by||"?").substring(0,10)+")"}
  var acts=document.querySelector(".actions");
  var doneBar=document.getElementById("doneBanner");
  if(task.status!=="pending"){acts.style.display="none";doneBar.style.display="block"}
  else{acts.style.display="flex";doneBar.style.display="none"}
  renderComments();renderLog()}
function fmtContent(raw){
  var s=esc(raw);
  s=s.replace(/(https?:\/\/[^\s<]+)/g,'<a href="$1" target="_blank" rel="noopener">$1</a>');
  s=s.replace(/\[img:(\/uploads\/[^\]]+)\]/g,'<img src="$1" onclick="viewImg(this.src)">');
  return s}
function viewImg(src){var o=document.getElementById("imgOverlay");document.getElementById("imgFull").src=src;o.style.display="flex"}
function renderComments(){
  var el=document.getElementById("commentsTab"),c=task.comments||[];
  if(!c.length){el.innerHTML='<div class="nocmt">ยังไม่มี comment<br>พิมพ์ด้านล่าง 👇</div>';return}
  el.innerHTML=c.map(function(x){var t="";if(x.created_at){try{t=new Date(x.created_at).toLocaleTimeString("th-TH",{hour:"2-digit",minute:"2-digit"})}catch(e){}}
  return'<div class="cmt"><div class="ct"><span class="cn">'+esc(x.author||"?")+'</span><span class="ctm">'+(t||"-")+'</span></div><div class="cb">'+fmtContent(x.content)+'</div></div>'}).join("")}
function renderLog(){
  var el=document.getElementById("logTab"),logs=task.logs||[];
  if(!logs.length){el.innerHTML='<div class="nocmt">ยังไม่มี activity log</div>';return}
  var icons={"created":"🆕","edited":"✏️","commented":"💬","completed":"✅","deleted":"🗑️"};
  el.innerHTML=logs.map(function(l){var t="";if(l.created_at){try{var d=new Date(l.created_at);t=d.toLocaleDateString("th-TH",{day:"2-digit",month:"2-digit"})+" "+d.toLocaleTimeString("th-TH",{hour:"2-digit",minute:"2-digit"})}catch(e){}}
  return'<div class="log-item"><div class="log-icon">'+(icons[l.action]||"📌")+'</div><div class="log-body"><div class="log-user">'+esc(l.user_name||"?")+'</div><div class="log-detail">'+esc(l.detail||"")+'</div></div><div class="log-time">'+(t||"-")+'</div></div>'}).join("")}
function showTab(tab){
  document.querySelectorAll(".tab").forEach(function(t,i){t.classList.toggle("active",i===(tab==="comments"?0:1))});
  document.getElementById("commentsTab").style.display=tab==="comments"?"block":"none";
  document.getElementById("logTab").style.display=tab==="log"?"block":"none"}
function esc(s){var d=document.createElement("div");d.textContent=s;return d.innerHTML}
function showEdit(){document.getElementById("einput").value=task.title;document.getElementById("ebox").style.display="block";document.getElementById("einput").focus()}
function hideEdit(){document.getElementById("ebox").style.display="none"}
async function saveEdit(){var v=document.getElementById("einput").value.trim();if(!v)return;
  await fetch(API+"/api/task/"+taskId,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:v,author:gn(),author_uid:""})});
  await load();hideEdit();toast("✏️ แก้ไขแล้ว!")}
async function sendCmt(){var inp=document.getElementById("cinput"),v=inp.value.trim();if(!v)return;inp.value="";
  await fetch(API+"/api/task/"+taskId+"/comment",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:v,author:gn(),author_uid:""})});
  await load();toast("💬 เพิ่ม comment แล้ว!")}
var DONE_HTML='<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:80vh;background:#E8F5E9;border-radius:16px;margin:12px;padding:24px"><div style="font-size:64px;animation:pop 0.4s">✅</div><div style="font-size:22px;font-weight:bold;color:#2E7D32;margin-top:16px">เสร็จแล้ว!</div><div style="font-size:14px;color:#666;margin-top:8px">กำลังกลับไปแชท...</div><a href="https://line.me/R/" style="display:inline-block;margin-top:24px;padding:14px 40px;background:#06C755;color:#fff;border-radius:50px;text-decoration:none;font-size:16px;font-weight:bold">กลับไปแชท LINE</a></div><style>@keyframes pop{0%{transform:scale(0)}50%{transform:scale(1.3)}100%{transform:scale(1)}}</style>';
function showDone(){document.getElementById("app").innerHTML=DONE_HTML;setTimeout(function(){location.href="https://line.me/R/"},1500)}
function confirmDone(){showConfirm("✅ ยืนยันเสร็จ?","งาน: "+task.title,async function(){
  try{
    var r=await fetch(API+"/api/task/"+taskId+"/done",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({author:gn(),author_uid:""})});
    if(r.ok){showDone();return}
    var errData=null;try{errData=await r.json()}catch(e){}
    // API returned error - check if task is actually done
    try{var chk=await fetch(API+"/api/task/"+taskId);if(chk.ok){var d=await chk.json();if(d.status==="done"){showDone();return}}}catch(e2){}
    toast("ทำไม่ได้: "+(errData&&errData.error?errData.error:"status "+r.status))
  }catch(e){
    // Network error - still check if task got done
    try{var chk=await fetch(API+"/api/task/"+taskId);if(chk.ok){var d=await chk.json();if(d.status==="done"){showDone();return}}}catch(e2){}
    toast("เชื่อมต่อไม่ได้: "+e.message)}})}
var DEL_HTML='<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:80vh;background:#FFEBEE;border-radius:16px;margin:12px;padding:24px"><div style="font-size:64px;animation:pop 0.4s">🗑️</div><div style="font-size:22px;font-weight:bold;color:#C62828;margin-top:16px">ลบงานแล้ว</div><div style="font-size:14px;color:#666;margin-top:8px">กำลังกลับไปแชท...</div><a href="https://line.me/R/" style="display:inline-block;margin-top:24px;padding:14px 40px;background:#06C755;color:#fff;border-radius:50px;text-decoration:none;font-size:16px;font-weight:bold">กลับไปแชท LINE</a></div><style>@keyframes pop{0%{transform:scale(0)}50%{transform:scale(1.3)}100%{transform:scale(1)}}</style>';
function showDel(){document.getElementById("app").innerHTML=DEL_HTML;setTimeout(function(){location.href="https://line.me/R/"},1500)}
function confirmDelete(){showConfirm("⚠️ ยืนยันลบ?","ลบแล้วกู้คืนไม่ได้!",async function(){
  try{
    var r=await fetch(API+"/api/task/"+taskId+"/delete",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({author:gn(),author_uid:""})});
    if(r.ok){showDel();return}
    var errData=null;try{errData=await r.json()}catch(e){}
    toast("ลบไม่ได้: "+(errData&&errData.error?errData.error:"status "+r.status))
  }catch(e){toast("เชื่อมต่อไม่ได้: "+e.message)}})}
async function uploadImg(input){
  if(!input.files||!input.files[0])return;
  var fd=new FormData();fd.append("file",input.files[0]);
  toast("📤 กำลังอัพโหลด...");
  try{var r=await fetch(API+"/api/upload",{method:"POST",body:fd});
    if(r.ok){var d=await r.json();
      await fetch(API+"/api/task/"+taskId+"/comment",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:"[img:"+d.url+"]",author:gn(),author_uid:""})});
      await load();toast("📷 เพิ่มรูปแล้ว!")}
    else toast("อัพโหลดไม่ได้")}catch(e){toast("อัพโหลดไม่ได้: "+e.message)}
  input.value=""}
async function askOwner(){
  var r=await fetch(API+"/api/task/"+taskId+"/ask-owner",{method:"POST"});
  if(r.ok)toast("🙋 tag คนสั่งแล้ว!");else toast("ไม่สามารถ tag ได้")}
function showConfirm(title,msg,onYes){document.getElementById("confirmTitle").textContent=title;document.getElementById("confirmMsg").textContent=msg;
  document.getElementById("confirmYes").onclick=function(){hideConfirm();onYes()};document.getElementById("confirmOverlay").style.display="flex"}
function hideConfirm(){document.getElementById("confirmOverlay").style.display="none"}
function toast(m){var t=document.getElementById("toast");t.textContent=m;t.style.display="block";setTimeout(function(){t.style.display="none"},2500)}
init();
</script></body></html>"""

@app.route("/liff/task")
def task_page():
    return TASK_PAGE_HTML

# ── Scheduled Summary ────────────────────────────────────────
def send_daily():
    for cid in get_active_chats():
        try: push_msg(cid, build_summary(cid))
        except Exception as e: app.logger.error("Sum err %s: %s",cid,e)

@app.route("/uploads/<path:fname>")
def serve_upload(fname):
    from flask import send_from_directory
    upload_dir = os.path.join(os.path.dirname(DATABASE_PATH) or ".", "uploads")
    return send_from_directory(upload_dir, fname)

@app.route("/", methods=["GET"])
def health():
    info = {"status":"ok","db":"pg" if USE_PG else "sqlite","version":"v6.2",
            "has_token": bool(LINE_CHANNEL_ACCESS_TOKEN), "has_secret": bool(LINE_CHANNEL_SECRET),
            "app_url": APP_URL or "(not set)"}
    # Quick DB check
    try:
        with get_db() as c:
            db_fetchone(c, "SELECT COUNT(*) as cnt FROM tasks")
        info["db_ok"] = True
    except Exception as e:
        info["db_ok"] = False; info["db_err"] = str(e)
    return jsonify(info)

@app.route("/debug/tasks")
def debug_tasks():
    try:
        with get_db() as c:
            tasks = db_fetchall(c, "SELECT id, title, status, chat_id, created_at, completed_at FROM tasks ORDER BY id DESC LIMIT 20")
        return jsonify({"db":"pg" if USE_PG else "sqlite", "tasks":tasks, "count":len(tasks)})
    except Exception as e:
        return jsonify({"error":str(e)}),500

try:
    init_db()
    app.logger.info("DB initialized OK (mode=%s)", "pg" if USE_PG else "sqlite")
except Exception as e:
    app.logger.error("DB init FAILED: %s", e)
    import traceback; traceback.print_exc()
    # Fallback to SQLite if PG fails
    if USE_PG:
        app.logger.warning("Falling back to SQLite")
        USE_PG = False
        DATABASE_PATH = os.environ.get("DATABASE_PATH", "todo.db")
        try:
            init_db()
            app.logger.info("SQLite fallback OK")
        except Exception as e2:
            app.logger.error("SQLite fallback also failed: %s", e2)

app.logger.info("Bot v6.3 started — webhook at /callback")

# ── Health & Debug endpoints ────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status":"ok","version":"6.3","db":"pg" if USE_PG else "sqlite"})

@app.route("/debug/status")
def debug_status():
    """Check LINE API token, DB, and env vars"""
    result = {"version":"6.3","db_type":"pg" if USE_PG else "sqlite"}
    # Check LINE token
    try:
        r = requests.get("https://api.line.me/v2/bot/info", headers=lh(), timeout=5)
        result["line_api_status"] = r.status_code
        if r.status_code == 200:
            info = r.json()
            result["bot_name"] = info.get("displayName","?")
            result["bot_id"] = info.get("userId","?")[:10]+"..."
        else:
            result["line_api_error"] = r.text[:200]
    except Exception as e:
        result["line_api_error"] = str(e)
    # Check message quota
    try:
        r = requests.get("https://api.line.me/v2/bot/message/quota/consumption", headers=lh(), timeout=5)
        if r.status_code == 200:
            result["messages_used_this_month"] = r.json().get("totalUsage",0)
    except: pass
    try:
        r = requests.get("https://api.line.me/v2/bot/message/quota", headers=lh(), timeout=5)
        if r.status_code == 200:
            result["message_quota"] = r.json()
    except: pass
    # Check DB
    try:
        with get_db() as c:
            if USE_PG:
                cur = c.cursor(); cur.execute("SELECT COUNT(*) as cnt FROM tasks"); cnt = cur.fetchone()["cnt"]
            else:
                cnt = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            result["db_ok"] = True
            result["total_tasks"] = cnt
    except Exception as e:
        result["db_ok"] = False
        result["db_error"] = str(e)
    # Env vars check
    result["has_token"] = bool(LINE_CHANNEL_ACCESS_TOKEN)
    result["has_secret"] = bool(LINE_CHANNEL_SECRET)
    result["has_app_url"] = bool(APP_URL)
    result["app_url"] = APP_URL[:50] if APP_URL else ""
    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
