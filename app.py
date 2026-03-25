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
from flask import Flask, request, abort, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
LIFF_ID                   = os.environ.get("LIFF_ID", "")
LINE_API_URL              = "https://api.line.me/v2/bot"
DAILY_SUMMARY_HOUR        = int(os.environ.get("DAILY_SUMMARY_HOUR", "18"))
DAILY_SUMMARY_MINUTE      = int(os.environ.get("DAILY_SUMMARY_MINUTE", "0"))
DATABASE_PATH             = os.environ.get("DATABASE_PATH", "todo.db")

def lh():
    return {"Content-Type":"application/json","Authorization":"Bearer "+LINE_CHANNEL_ACCESS_TOKEN}
def verify_sig(body, sig):
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(h).decode(), sig)
def reply_msg(tok, msgs):
    if isinstance(msgs, str): msgs = [{"type":"text","text":msgs}]
    elif isinstance(msgs, dict): msgs = [msgs]
    r = requests.post(LINE_API_URL+"/message/reply", headers=lh(), json={"replyToken":tok,"messages":msgs})
    if r.status_code!=200: app.logger.error("Reply err: %s %s", r.status_code, r.text)
def push_msg(to, msgs):
    if isinstance(msgs, str): msgs = [{"type":"text","text":msgs}]
    elif isinstance(msgs, dict): msgs = [msgs]
    requests.post(LINE_API_URL+"/message/push", headers=lh(), json={"to":to,"messages":msgs})
def get_profile(uid):
    try:
        r = requests.get(LINE_API_URL+"/profile/"+uid, headers=lh())
        if r.status_code==200: return r.json().get("displayName","")
    except: pass
    return ""

# ── Database ─────────────────────────────────────────────────
@contextmanager
def get_db():
    c = sqlite3.connect(DATABASE_PATH); c.row_factory = sqlite3.Row
    try: yield c; c.commit()
    finally: c.close()

def init_db():
    with get_db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
            title TEXT NOT NULL, added_by TEXT DEFAULT '', added_by_user_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending', created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME, due_date DATE)""")
        for col in ["added_by_user_id"]:
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

# ── Activity Log ─────────────────────────────────────────────
def log_activity(task_id, chat_id, user_name, user_id, action, detail=""):
    with get_db() as c:
        c.execute("INSERT INTO activity_log(task_id,chat_id,user_name,user_id,action,detail) VALUES(?,?,?,?,?,?)",
                  (task_id, chat_id, user_name, user_id, action, detail))

def get_activity_log(task_id, limit=20):
    with get_db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM activity_log WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit)).fetchall()]

# ── Pending Actions ──────────────────────────────────────────
def set_pending(uid, cid, act, data=""):
    with get_db() as c:
        c.execute("INSERT OR REPLACE INTO pending_actions VALUES(?,?,?,?)",("{}:{}".format(uid,cid),act,data,datetime.now().isoformat()))
def get_pending(uid, cid):
    with get_db() as c:
        r = c.execute("SELECT action,data FROM pending_actions WHERE user_chat_key=?",("{}:{}".format(uid,cid),)).fetchone()
    return {"action":r["action"],"data":r["data"]} if r else None
def clear_pending(uid, cid):
    with get_db() as c: c.execute("DELETE FROM pending_actions WHERE user_chat_key=?",("{}:{}".format(uid,cid),))

# ── Task CRUD ────────────────────────────────────────────────
def add_task(cid, title, by="", by_uid=""):
    with get_db() as c:
        cur = c.execute("INSERT INTO tasks(chat_id,title,added_by,added_by_user_id) VALUES(?,?,?,?)",(cid,title.strip(),by,by_uid))
    tid = cur.lastrowid
    log_activity(tid, cid, by, by_uid, "created", "สร้างงาน: {}".format(title.strip()))
    return get_task(tid)

def get_task(tid):
    with get_db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
    return dict(r) if r else None

def get_pending_tasks(cid):
    with get_db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks WHERE chat_id=? AND status='pending' ORDER BY created_at",(cid,)).fetchall()]

def get_completed_today(cid):
    with get_db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks WHERE chat_id=? AND status='done' AND DATE(completed_at)=? ORDER BY completed_at",
            (cid,datetime.now().strftime("%Y-%m-%d"))).fetchall()]

def complete_task(tid, by_name="", by_uid=""):
    with get_db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
        if r and r["status"]=="pending":
            c.execute("UPDATE tasks SET status='done',completed_at=? WHERE id=?",(datetime.now().isoformat(),tid))
            log_activity(tid, r["chat_id"], by_name, by_uid, "completed", "ทำเสร็จ: {}".format(r["title"]))
            return dict(r)
    return None

def edit_task(tid, new_title, by_name="", by_uid=""):
    with get_db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
        if r:
            c.execute("UPDATE tasks SET title=? WHERE id=?",(new_title.strip(),tid))
            log_activity(tid, r["chat_id"], by_name, by_uid, "edited", "แก้ไข: {} → {}".format(r["title"], new_title.strip()))
            return {"id":tid,"old":r["title"],"new":new_title.strip()}
    return None

def delete_task(tid, by_name="", by_uid=""):
    with get_db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
        if r:
            log_activity(tid, r["chat_id"], by_name, by_uid, "deleted", "ลบงาน: {}".format(r["title"]))
            c.execute("DELETE FROM comments WHERE task_id=?",(tid,))
            c.execute("DELETE FROM tasks WHERE id=?",(tid,))
            return dict(r)
    return None

def get_active_chats():
    with get_db() as c:
        return [r["chat_id"] for r in c.execute("SELECT DISTINCT chat_id FROM tasks WHERE status='pending'").fetchall()]

def register_member(cid, uid, name=""):
    with get_db() as c: c.execute("INSERT OR REPLACE INTO chat_members VALUES(?,?,?)",(cid,uid,name))

def get_task_index(cid, tid):
    for i,t in enumerate(get_pending_tasks(cid),1):
        if t["id"]==tid: return i
    return 0

# ── Comments ─────────────────────────────────────────────────
def add_comment(tid, cid, author, author_uid, content):
    with get_db() as c:
        c.execute("INSERT INTO comments(task_id,chat_id,author,author_user_id,content) VALUES(?,?,?,?,?)",(tid,cid,author,author_uid,content))
    log_activity(tid, cid, author, author_uid, "commented", content[:50])

def get_comments(tid):
    with get_db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM comments WHERE task_id=? ORDER BY created_at",(tid,)).fetchall()]

# ── Quick Reply ──────────────────────────────────────────────
def qr():
    return {"items":[
        {"type":"action","action":{"type":"postback","label":"➕ เพิ่มงาน","data":"action=add_prompt","displayText":"➕ เพิ่มงาน"}},
        {"type":"action","action":{"type":"postback","label":"📋 ดูงาน","data":"action=list","displayText":"📋 ดูงาน"}},
        {"type":"action","action":{"type":"postback","label":"📊 สรุป","data":"action=summary","displayText":"📊 สรุป"}},
        {"type":"action","action":{"type":"message","label":"🌅 เข้างาน","text":"เข้างาน"}},
        {"type":"action","action":{"type":"postback","label":"❓ วิธีใช้","data":"action=help","displayText":"❓ วิธีใช้"}},
    ]}
def aqr(msg):
    if isinstance(msg, str): msg = {"type":"text","text":msg}
    if isinstance(msg, dict) and "quickReply" not in msg: msg["quickReply"] = qr()
    return msg

def liff_url(tid):
    return "https://liff.line.me/{}?task_id={}".format(LIFF_ID, tid) if LIFF_ID else None

# ── Flex Cards ───────────────────────────────────────────────
def build_mini_card(task, idx):
    tid = task["id"]; cc = len(get_comments(tid)); by = task.get("added_by","") or "-"
    lu = liff_url(tid)
    if lu:
        # มี LIFF → ปุ่มเดียว เปิด LIFF จัดการทุกอย่างข้างใน
        footer_contents=[{"type":"button","action":{"type":"uri","label":"📖 เปิดดู / จัดการ","uri":lu},"style":"primary","height":"sm","color":"#1DB446"}]
    else:
        footer_contents=[
            {"type":"button","action":{"type":"postback","label":"📖 เปิดดู","data":"action=view_task&task_id={}".format(tid)},"style":"primary","height":"sm","color":"#1DB446"},
            {"type":"button","action":{"type":"postback","label":"✅ เสร็จ","data":"action=done&task_id={}".format(tid)},"style":"secondary","height":"sm","margin":"sm"}]
    # แสดง comment ล่าสุดถ้ามี
    comments=get_comments(tid)
    body_contents=[
        {"type":"text","text":"สั่งโดย: {}".format(by),"size":"xs","color":"#888888"},
        {"type":"text","text":"💬 {} comment".format(cc),"size":"xs","color":"#666666","margin":"sm"}]
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
    ca=""
    if task.get("created_at"):
        try: ca=datetime.fromisoformat(task["created_at"]).strftime("%d/%m %H:%M")
        except: pass
    body=[
        {"type":"box","layout":"horizontal","contents":[
            {"type":"text","text":"สั่งโดย:","size":"xs","color":"#888888","flex":2},
            {"type":"text","text":by,"size":"xs","color":"#333333","flex":5,"weight":"bold"}]},
        {"type":"box","layout":"horizontal","contents":[
            {"type":"text","text":"เมื่อ:","size":"xs","color":"#888888","flex":2},
            {"type":"text","text":ca or "-","size":"xs","color":"#333333","flex":5}],"margin":"sm"}]
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

    lu=liff_url(tid)
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
        lu=liff_url(p[0]["id"])
        if lu: return aqr({"type":"flex","altText":"📋 งานค้าง 1 งาน","contents":build_mini_card(p[0],1)})
        return build_task_flex(p[0]["id"])
    return aqr({"type":"flex","altText":"📋 งานค้าง {} งาน".format(len(p)),"contents":{"type":"carousel","contents":[build_mini_card(t,i) for i,t in enumerate(p[:10],1)]}})

# ── Summary with checkboxes ─────────────────────────────────
def build_summary(cid):
    now=datetime.now(); done=get_completed_today(cid); pend=get_pending_tasks(cid)
    di=[{"type":"text","text":"  ✔️ {}".format(t["title"]),"size":"xs","color":"#1DB446","wrap":True} for t in done] or [{"type":"text","text":"  — ยังไม่มี","size":"xs","color":"#999999"}]
    # pending items with done+delete buttons per row
    pi=[]
    for i,t in enumerate(pend,1):
        pi.append({"type":"box","layout":"horizontal","contents":[
            {"type":"button","action":{"type":"postback","label":"☑️","data":"action=confirm_done&task_id={}".format(t["id"])},"style":"secondary","height":"sm","flex":0,"gravity":"center"},
            {"type":"text","text":"{}. {}".format(i,t["title"]),"size":"xs","color":"#FF6B35","wrap":True,"flex":5,"gravity":"center","margin":"sm"},
            {"type":"button","action":{"type":"postback","label":"🗑️","data":"action=confirm_delete&task_id={}".format(t["id"])},"style":"secondary","height":"sm","flex":0,"gravity":"center","margin":"sm"},
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
            {"type":"text","text":"📋 Activity Log ทุกงาน","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"text","text":"ดูย้อนหลังว่าใครทำอะไรเมื่อไหร่","size":"xs","color":"#666666","margin":"sm"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"⏰ auto สรุปทุกวัน 18:00","size":"xs","color":"#999999","margin":"lg","align":"center"},
        ],"paddingAll":"15px"}}})

# ── Text Commands ────────────────────────────────────────────
CANCEL=["ยกเลิก","cancel","ไม่","no"]
CMDS=["เพิ่ม","add","todo","เพิ่มงาน","งานค้าง","ดูงาน","list","tasks","สรุป","summary","เข้างาน","clock in","help","วิธีใช้","เมนู","menu"]

def process_text(text, cid, uid="", name=""):
    ts=text.strip(); tl=ts.lower()
    pa=get_pending(uid,cid)
    if pa:
        clear_pending(uid,cid)
        if tl in CANCEL: return aqr("❌ ยกเลิกแล้ว")
        is_cmd=any(tl==c or tl.startswith(c+" ") for c in CMDS) or any(tl.startswith(p) for p in ["note ","แก้ ","เสร็จ","ลบ","log "])
        if not is_cmd:
            if pa["action"]=="waiting_add":
                t=add_task(cid,ts,by=name,by_uid=uid); return build_task_flex(t["id"])
            elif pa["action"]=="waiting_edit" and pa["data"]:
                edit_task(int(pa["data"]),ts,name,uid); return build_task_flex(int(pa["data"]))
            elif pa["action"]=="waiting_comment" and pa["data"]:
                tid=int(pa["data"]); t=get_task(tid)
                if t and t["chat_id"]==cid: add_comment(tid,cid,name,uid,ts); return build_task_flex(tid)
                return aqr("❌ ไม่พบงานนี้")

    if tl in ["เข้างาน","clock in","เริ่มงาน"]: return build_clockin(cid)
    m=re.match(r"^(?:เพิ่ม|add|todo)\s+(.+)",ts,re.I)
    if m: t=add_task(cid,m.group(1).strip(),by=name,by_uid=uid); return build_task_flex(t["id"])
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
    if tl in ["help","วิธีใช้","ช่วย","คำสั่ง","?","เมนู","menu"]: return build_help()
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
    if not verify_sig(body,sig): abort(400)
    for ev in json.loads(body).get("events",[]):
        try:
            tok=ev.get("replyToken",""); src=ev.get("source",{}); st=src.get("type","")
            cid=src.get("groupId","") if st=="group" else src.get("roomId","") if st=="room" else src.get("userId","")
            uid=src.get("userId",""); name=get_profile(uid) if uid else ""
            if name and cid!=uid: register_member(cid,uid,name)
            if ev.get("type")=="message" and ev.get("message",{}).get("type")=="text":
                r=process_text(ev["message"]["text"].strip(),cid,uid,name)
                if r: reply_msg(tok,r)
            elif ev.get("type")=="postback":
                handle_pb(ev.get("postback",{}).get("data",""),cid,tok,uid,name)
        except Exception as e: app.logger.error("Err: %s",e)
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
    d=request.get_json() or {}
    t=complete_task(tid,d.get("author",""),d.get("author_uid",""))
    if t: return jsonify({"ok":True})
    return jsonify({"error":"fail"}),400

@app.route("/api/task/<int:tid>/delete",methods=["DELETE"])
def api_del(tid):
    d=request.get_json() or {}
    t=delete_task(tid,d.get("author",""),d.get("author_uid",""))
    if t: return jsonify({"ok":True})
    return jsonify({"error":"fail"}),404

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

# ══════════════════════════════════════════════════════════════
# LIFF Page
# ══════════════════════════════════════════════════════════════
LIFF_HTML = r"""<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Task Detail</title>
<script src="https://static.line-sdn.net/liff/edge/2/sdk.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#333}
.loading{display:flex;justify-content:center;align-items:center;height:100vh;font-size:18px;color:#1DB446}
.app{display:none;padding-bottom:70px}
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
.actions{padding:8px 16px;display:flex;flex-direction:column;gap:6px}
.arow{display:flex;gap:6px}
.abtn{flex:1;padding:11px;border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;text-align:center}
.a-done{background:#E8F5E9;color:#1DB446}.a-ask{background:#FFF3E0;color:#E65100;border:1px solid #FFB74D}.a-del{background:#FFEBEE;color:#E53935}
.cbar{position:fixed;bottom:0;left:0;right:0;border-top:1px solid #eee;padding:8px 12px;display:flex;gap:8px;background:#fff;z-index:20}
.cbar input{flex:1;padding:9px 14px;border:1.5px solid #ddd;border-radius:22px;font-size:13px;outline:none}.cbar input:focus{border-color:#1DB446}
.cbar button{background:#1DB446;color:#fff;border:none;border-radius:50%;width:38px;height:38px;font-size:16px;cursor:pointer}
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
  <div class="actions">
    <div class="arow"><button class="abtn a-done" onclick="confirmDone()">✅ เสร็จแล้ว</button></div>
    <div class="arow"><button class="abtn a-ask" id="askBtn" onclick="askOwner()" style="display:none">🙋 ถามคนสั่ง</button>
    <button class="abtn a-del" onclick="confirmDelete()">🗑️ ลบงาน</button></div>
  </div>
  <div class="cbar"><input id="cinput" placeholder="พิมพ์ comment..." onkeypress="if(event.key==='Enter')sendCmt()"><button onclick="sendCmt()">➤</button></div>
</div>
<script>
const LIFF_ID="{{liff_id}}",API="";let taskId,task,profile;
async function init(){
  try{await liff.init({liffId:LIFF_ID});if(!liff.isLoggedIn()){liff.login();return}
  profile=await liff.getProfile();taskId=new URLSearchParams(location.search).get("task_id");
  if(!taskId){document.getElementById("loading").textContent="❌ ไม่มี task_id";return}
  await load();document.getElementById("loading").style.display="none";document.getElementById("app").style.display="block"}
  catch(e){document.getElementById("loading").textContent="❌ "+e.message}}
async function load(){
  const r=await fetch(API+"/api/task/"+taskId);if(!r.ok)return;task=await r.json();render()}
function render(){
  document.getElementById("tidx").textContent="#"+(task.index||task.id)+" "+(task.status==="pending"?"⬜":"✅");
  document.getElementById("ttitle").textContent=task.title;
  document.getElementById("tby").textContent="สั่งโดย: "+(task.added_by||"-");
  let dt="";if(task.created_at){try{const d=new Date(task.created_at);dt=d.toLocaleDateString("th-TH",{day:"2-digit",month:"2-digit"})+" "+d.toLocaleTimeString("th-TH",{hour:"2-digit",minute:"2-digit"})}catch(e){}}
  document.getElementById("tdate").textContent="เมื่อ: "+(dt||"-");
  if(task.added_by_user_id&&task.added_by_user_id!==profile.userId){const b=document.getElementById("askBtn");b.style.display="flex";b.textContent="🙋 ถามคนสั่ง ("+(task.added_by||"?").substring(0,10)+")"}
  renderComments();renderLog()}
function renderComments(){
  const el=document.getElementById("commentsTab"),c=task.comments||[];
  if(!c.length){el.innerHTML='<div class="nocmt">ยังไม่มี comment<br>พิมพ์ด้านล่าง 👇</div>';return}
  el.innerHTML=c.map(x=>{let t="";if(x.created_at){try{t=new Date(x.created_at).toLocaleTimeString("th-TH",{hour:"2-digit",minute:"2-digit"})}catch(e){}}
  return'<div class="cmt"><div class="ct"><span class="cn">'+esc(x.author||"?")+'</span><span class="ctm">'+(t||"-")+'</span></div><div class="cb">'+esc(x.content)+'</div></div>'}).join("")}
function renderLog(){
  const el=document.getElementById("logTab"),logs=task.logs||[];
  if(!logs.length){el.innerHTML='<div class="nocmt">ยังไม่มี activity log</div>';return}
  const icons={"created":"🆕","edited":"✏️","commented":"💬","completed":"✅","deleted":"🗑️"};
  el.innerHTML=logs.map(l=>{let t="";if(l.created_at){try{const d=new Date(l.created_at);t=d.toLocaleDateString("th-TH",{day:"2-digit",month:"2-digit"})+" "+d.toLocaleTimeString("th-TH",{hour:"2-digit",minute:"2-digit"})}catch(e){}}
  return'<div class="log-item"><div class="log-icon">'+(icons[l.action]||"📌")+'</div><div class="log-body"><div class="log-user">'+esc(l.user_name||"?")+'</div><div class="log-detail">'+esc(l.detail||"")+'</div></div><div class="log-time">'+(t||"-")+'</div></div>'}).join("")}
function showTab(tab){
  document.querySelectorAll(".tab").forEach((t,i)=>t.classList.toggle("active",i===(tab==="comments"?0:1)));
  document.getElementById("commentsTab").style.display=tab==="comments"?"block":"none";
  document.getElementById("logTab").style.display=tab==="log"?"block":"none"}
function esc(s){const d=document.createElement("div");d.textContent=s;return d.innerHTML}
function showEdit(){document.getElementById("einput").value=task.title;document.getElementById("ebox").style.display="block";document.getElementById("einput").focus()}
function hideEdit(){document.getElementById("ebox").style.display="none"}
async function saveEdit(){const v=document.getElementById("einput").value.trim();if(!v)return;
  await fetch(API+"/api/task/"+taskId,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:v,author:profile.displayName,author_uid:profile.userId})});
  await load();hideEdit();toast("✏️ แก้ไขแล้ว!")}
async function sendCmt(){const inp=document.getElementById("cinput"),v=inp.value.trim();if(!v)return;inp.value="";
  await fetch(API+"/api/task/"+taskId+"/comment",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:v,author:profile.displayName,author_uid:profile.userId})});
  await load();toast("💬 เพิ่ม comment แล้ว!")}
function confirmDone(){showConfirm("✅ ยืนยันเสร็จ?","งาน: "+task.title,async()=>{
  await fetch(API+"/api/task/"+taskId+"/done",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({author:profile.displayName,author_uid:profile.userId})});
  toast("✅ เสร็จแล้ว!");setTimeout(()=>{if(liff.isInClient())liff.closeWindow();else location.reload()},1000)})}
function confirmDelete(){showConfirm("⚠️ ยืนยันลบ?","ลบแล้วกู้คืนไม่ได้!",async()=>{
  await fetch(API+"/api/task/"+taskId+"/delete",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({author:profile.displayName,author_uid:profile.userId})});
  toast("🗑️ ลบแล้ว!");setTimeout(()=>{if(liff.isInClient())liff.closeWindow();else location.reload()},1000)})}
async function askOwner(){
  const r=await fetch(API+"/api/task/"+taskId+"/ask-owner",{method:"POST"});
  if(r.ok){toast("🙋 tag คนสั่งแล้ว!");setTimeout(()=>{if(liff.isInClient())liff.closeWindow()},1500)}else toast("❌ ไม่สามารถ tag ได้")}
function showConfirm(title,msg,onYes){document.getElementById("confirmTitle").textContent=title;document.getElementById("confirmMsg").textContent=msg;
  document.getElementById("confirmYes").onclick=()=>{hideConfirm();onYes()};document.getElementById("confirmOverlay").style.display="flex"}
function hideConfirm(){document.getElementById("confirmOverlay").style.display="none"}
function toast(m){const t=document.getElementById("toast");t.textContent=m;t.style.display="block";setTimeout(()=>t.style.display="none",2500)}
init();
</script></body></html>"""

@app.route("/liff/task")
def liff_page():
    return render_template_string(LIFF_HTML, liff_id=LIFF_ID)

# ── Scheduled Summary ────────────────────────────────────────
def send_daily():
    for cid in get_active_chats():
        try: push_msg(cid, build_summary(cid))
        except Exception as e: app.logger.error("Sum err %s: %s",cid,e)

@app.route("/", methods=["GET"])
def health(): return "LINE Todo Bot v6 running!"

init_db()
sched = BackgroundScheduler(timezone="Asia/Bangkok")
sched.add_job(send_daily, "cron", hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE)
sched.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
