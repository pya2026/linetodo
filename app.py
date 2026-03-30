"""
LINE Todo Bot v6 — LIFF Hybrid + Activity Log
- Messaging API: Flex cards, Quick Reply, auto สรุป 18:00
- LIFF: หน้าเว็บจัดการงาน (แก้ไข/comment/ถามคนสั่ง/log)
- Activity Log: บันทึกทุก action (เปิด/แก้/comment/เสร็จ/ลบ)
- สรุป: dropdown เลือกงานเสร็จ + ถังขยะลบ + ยืนยัน
"""

import os, re, json, hashlib, hmac, base64
from datetime import datetime
from contextlib import contextmanager

import requests, psycopg2, psycopg2.extras
from flask import Flask, request, abort, jsonify

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
LIFF_ID                   = os.environ.get("LIFF_ID", "")
APP_URL                   = os.environ.get("APP_URL", "")
LINE_API_URL              = "https://api.line.me/v2/bot"
DATABASE_URL              = os.environ.get("DATABASE_URL", "")

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

# ── Database (PostgreSQL) ─────────────────────────────────────
@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try: yield conn; conn.commit()
    except: conn.rollback(); raise
    finally: conn.close()

def db_exec(conn, sql, params=None):
    """Execute SQL — conn is the connection, uses cursor internally"""
    cur = conn.cursor()
    cur.execute(sql, params or ())
    return cur

def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY, chat_id TEXT NOT NULL,
            title TEXT NOT NULL, added_by TEXT DEFAULT '', added_by_user_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP, due_date DATE,
            assigned_to TEXT DEFAULT '', assigned_to_uid TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY, task_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL, author TEXT DEFAULT '', author_user_id TEXT DEFAULT '',
            content TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS chat_members (
            chat_id TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT DEFAULT '',
            PRIMARY KEY (chat_id, user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS pending_actions (
            user_chat_key TEXT PRIMARY KEY, action TEXT NOT NULL,
            data TEXT DEFAULT '', created_at TIMESTAMP DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY, task_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL, user_name TEXT DEFAULT '', user_id TEXT DEFAULT '',
            action TEXT NOT NULL, detail TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW())""")

# ── Activity Log ─────────────────────────────────────────────
def log_activity(task_id, chat_id, user_name, user_id, action, detail=""):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO activity_log(task_id,chat_id,user_name,user_id,action,detail) VALUES(%s,%s,%s,%s,%s,%s)",
                  (task_id, chat_id, user_name, user_id, action, detail))

def get_activity_log(task_id, limit=20):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM activity_log WHERE task_id=%s ORDER BY created_at DESC LIMIT %s",
            (task_id, limit))
        return cur.fetchall()

# ── Pending Actions ──────────────────────────────────────────
def set_pending(uid, cid, act, data=""):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO pending_actions VALUES(%s,%s,%s,%s) ON CONFLICT (user_chat_key) DO UPDATE SET action=EXCLUDED.action, data=EXCLUDED.data, created_at=EXCLUDED.created_at",("{}:{}".format(uid,cid),act,data,datetime.now().isoformat()))
def get_pending(uid, cid):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT action,data FROM pending_actions WHERE user_chat_key=%s",("{}:{}".format(uid,cid),))
        r = cur.fetchone()
    return {"action":r["action"],"data":r["data"]} if r else None
def clear_pending(uid, cid):
    with get_db() as conn: cur = conn.cursor(); cur.execute("DELETE FROM pending_actions WHERE user_chat_key=%s",("{}:{}".format(uid,cid),))

# ── Due Date Parser ──────────────────────────────────────────
def parse_due_date(text):
    """แยก due date ออกจาก text — format: วันที่ + งาน เช่น 'พรุ่งนี้ ส่งรายงาน' หรือ '30/03 ส่งเอกสาร'"""
    from datetime import timedelta
    today=datetime.now().date()
    # พรุ่งนี้/มะรืน
    m=re.match(r'^(พรุ่งนี้|มะรืน|วันนี้|tomorrow|today)\s+(.+)',text,re.I)
    if m:
        w=m.group(1).lower()
        if w in ["พรุ่งนี้","tomorrow"]: d=today+timedelta(days=1)
        elif w in ["มะรืน"]: d=today+timedelta(days=2)
        else: d=today
        return d.strftime("%Y-%m-%d"), m.group(2).strip()
    # dd/mm หรือ dd/mm/yyyy
    m=re.match(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s+(.+)',text)
    if m:
        dd,mm=int(m.group(1)),int(m.group(2))
        yy=int(m.group(3)) if m.group(3) else today.year
        if yy<100: yy+=2000
        try:
            from datetime import date
            d=date(yy,mm,dd)
            return d.strftime("%Y-%m-%d"), m.group(4).strip()
        except: pass
    # จันทร์/อังคาร/... หน้า
    days_th={"จันทร์":0,"อังคาร":1,"พุธ":2,"พฤหัส":3,"พฤหัสบดี":3,"ศุกร์":4,"เสาร์":5,"อาทิตย์":6}
    m=re.match(r'^(จันทร์|อังคาร|พุธ|พฤหัส|พฤหัสบดี|ศุกร์|เสาร์|อาทิตย์)\s+(.+)',text)
    if m:
        target=days_th[m.group(1)]
        current=today.weekday()
        diff=(target-current)%7
        if diff==0: diff=7
        d=today+timedelta(days=diff)
        return d.strftime("%Y-%m-%d"), m.group(2).strip()
    return None, text

def parse_date_only(text):
    """Parse date from text (date only, no task title needed). Returns YYYY-MM-DD or None."""
    from datetime import timedelta, date
    today=datetime.now().date()
    t=text.strip().lower()
    if t in ["พรุ่งนี้","tomorrow"]: return (today+timedelta(days=1)).strftime("%Y-%m-%d")
    if t in ["มะรืน"]: return (today+timedelta(days=2)).strftime("%Y-%m-%d")
    if t in ["วันนี้","today"]: return today.strftime("%Y-%m-%d")
    m=re.match(r'^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$',t)
    if m:
        dd,mm=int(m.group(1)),int(m.group(2))
        yy=int(m.group(3)) if m.group(3) else today.year
        if yy<100: yy+=2000
        try: return date(yy,mm,dd).strftime("%Y-%m-%d")
        except: pass
    days_th={"จันทร์":0,"อังคาร":1,"พุธ":2,"พฤหัส":3,"พฤหัสบดี":3,"ศุกร์":4,"เสาร์":5,"อาทิตย์":6}
    for dn,dv in days_th.items():
        if t==dn:
            diff=(dv-today.weekday())%7
            if diff==0: diff=7
            return (today+timedelta(days=diff)).strftime("%Y-%m-%d")
    return None

def query_tasks_by_person(cid, uid=None, name=None, due_date=None):
    """Query pending tasks — ดูจาก assigned_to เท่านั้น (ผู้รับผิดชอบ)"""
    with get_db() as conn:
        cur = conn.cursor()
        if uid:
            sql="SELECT * FROM tasks WHERE chat_id=%s AND status='pending' AND assigned_to_uid=%s"
            params=[cid, uid]
        elif name:
            sql="SELECT * FROM tasks WHERE chat_id=%s AND status='pending' AND assigned_to LIKE %s"
            params=[cid, "%"+name+"%"]
        else:
            sql="SELECT * FROM tasks WHERE chat_id=%s AND status='pending'"
            params=[cid]
        if due_date:
            sql+=" AND due_date=%s"
            params.append(due_date)
        sql+=" ORDER BY created_at"
        cur.execute(sql, params)
        return cur.fetchall()

def set_due_date(tid, due):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE tasks SET due_date=%s WHERE id=%s",(due,tid))

# ── Task CRUD ────────────────────────────────────────────────
def add_task(cid, title, by="", by_uid="", assigned_to="", assigned_to_uid=""):
    # ถ้าไม่ได้ assign ใคร → ผู้รับผิดชอบ = คนสร้างเอง
    if not assigned_to:
        assigned_to = by
        assigned_to_uid = by_uid
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO tasks(chat_id,title,added_by,added_by_user_id,assigned_to,assigned_to_uid) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",(cid,title.strip(),by,by_uid,assigned_to,assigned_to_uid))
        tid = cur.fetchone()["id"]
    detail = "สร้างงาน: {}".format(title.strip())
    if assigned_to and assigned_to != by: detail += " → มอบหมายให้ {}".format(assigned_to)
    log_activity(tid, cid, by, by_uid, "created", detail)
    return get_task(tid)

def get_task(tid):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE id=%s",(tid,))
        r = cur.fetchone()
    return r

def get_pending_tasks(cid):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE chat_id=%s AND status='pending' ORDER BY created_at",(cid,))
        return cur.fetchall()

def get_completed_today(cid):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE chat_id=%s AND status='done' AND completed_at::date=%s ORDER BY completed_at",
            (cid,datetime.now().strftime("%Y-%m-%d")))
        return cur.fetchall()

def complete_task(tid, by_name="", by_uid=""):
    result = None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE id=%s",(tid,))
        r = cur.fetchone()
        if r and r["status"]=="pending":
            cur.execute("UPDATE tasks SET status='done',completed_at=%s WHERE id=%s",(datetime.now().isoformat(),tid))
            result = r
    if result:
        log_activity(tid, result["chat_id"], by_name, by_uid, "completed", "ทำเสร็จ: {}".format(result["title"]))
    return result

def edit_task(tid, new_title, by_name="", by_uid=""):
    result = None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE id=%s",(tid,))
        r = cur.fetchone()
        if r:
            cur.execute("UPDATE tasks SET title=%s WHERE id=%s",(new_title.strip(),tid))
            result = {"id":tid,"old":r["title"],"new":new_title.strip(),"chat_id":r["chat_id"]}
    if result:
        log_activity(tid, result["chat_id"], by_name, by_uid, "edited", "แก้ไข: {} → {}".format(result["old"], result["new"]))
    return result

def delete_task(tid, by_name="", by_uid=""):
    result = None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM tasks WHERE id=%s",(tid,))
        r = cur.fetchone()
        if r:
            result = r
            cur.execute("DELETE FROM comments WHERE task_id=%s",(tid,))
            cur.execute("DELETE FROM tasks WHERE id=%s",(tid,))
    if result:
        log_activity(tid, result["chat_id"], by_name, by_uid, "deleted", "ลบงาน: {}".format(result["title"]))
    return result

def get_active_chats():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT chat_id FROM tasks WHERE status='pending'")
        return [r["chat_id"] for r in cur.fetchall()]

def register_member(cid, uid, name=""):
    with get_db() as conn: cur = conn.cursor(); cur.execute("INSERT INTO chat_members VALUES(%s,%s,%s) ON CONFLICT (chat_id, user_id) DO UPDATE SET display_name=EXCLUDED.display_name",(cid,uid,name))

def get_task_index(cid, tid):
    for i,t in enumerate(get_pending_tasks(cid),1):
        if t["id"]==tid: return i
    return 0

# ── Comments ─────────────────────────────────────────────────
def add_comment(tid, cid, author, author_uid, content):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO comments(task_id,chat_id,author,author_user_id,content) VALUES(%s,%s,%s,%s,%s)",(tid,cid,author,author_uid,content))
    log_activity(tid, cid, author, author_uid, "commented", content[:50])

def get_comments(tid):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM comments WHERE task_id=%s ORDER BY created_at",(tid,))
        return cur.fetchall()

# ── Quick Reply ──────────────────────────────────────────────
def qr():
    return {"items":[
        {"type":"action","action":{"type":"message","label":"🌅 เข้างาน","text":"เข้างาน"}},
        {"type":"action","action":{"type":"message","label":"🌆 เลิกงาน","text":"เลิกงาน"}},
        {"type":"action","action":{"type":"postback","label":"➕ เพิ่มงาน","data":"action=add_prompt","displayText":"➕ เพิ่มงาน"}},
        {"type":"action","action":{"type":"message","label":"📋 ดูงาน","text":"ดูงาน"}},
        {"type":"action","action":{"type":"message","label":"👥 งานทุกคน","text":"งานทุกคน"}},
        {"type":"action","action":{"type":"postback","label":"❓ วิธีใช้","data":"action=help","displayText":"❓ วิธีใช้"}},
    ]}
def aqr(msg):
    if isinstance(msg, str): msg = {"type":"text","text":msg}
    if isinstance(msg, dict) and "quickReply" not in msg: msg["quickReply"] = qr()
    return msg

def task_page_url(tid):
    if LIFF_ID: return "https://liff.line.me/{}?task_id={}".format(LIFF_ID, tid)
    if APP_URL: return APP_URL.rstrip("/")+"/liff/task?task_id={}".format(tid)
    return None

def summary_page_url(cid):
    if APP_URL: return APP_URL.rstrip("/")+"/liff/summary?chat_id={}".format(cid)
    return None

# ── Flex Cards ───────────────────────────────────────────────
def build_mini_card(task, idx):
    tid = task["id"]; cc = len(get_comments(tid)); by = task.get("added_by","") or "-"
    lu = task_page_url(tid)
    if lu:
        # มี LIFF → ปุ่มเดียว เปิด LIFF จัดการทุกอย่างข้างใน
        footer_contents=[{"type":"button","action":{"type":"uri","label":"📖 เปิดดู / จัดการ","uri":lu},"style":"primary","height":"sm","color":"#1DB446"}]
    else:
        footer_contents=[
            {"type":"button","action":{"type":"postback","label":"📖 เปิดดู","data":"action=view_task&task_id={}".format(tid)},"style":"primary","height":"sm","color":"#1DB446"},
            {"type":"button","action":{"type":"postback","label":"✅ เสร็จ","data":"action=done&task_id={}".format(tid)},"style":"secondary","height":"sm","margin":"sm"}]
    # แสดง comment ล่าสุดถ้ามี
    comments=get_comments(tid)
    assigned=task.get("assigned_to","")
    due=task.get("due_date","")
    body_contents=[
        {"type":"text","text":"สั่งโดย: {}".format(by),"size":"xs","color":"#888888"}]
    if assigned:
        body_contents.append({"type":"text","text":"👤 ผู้รับผิดชอบ: {}".format(assigned),"size":"xs","color":"#FF6B35","weight":"bold"})
    if due:
        try:
            dd=datetime.strptime(due,"%Y-%m-%d").strftime("%d/%m/%y")
            body_contents.append({"type":"text","text":"📅 กำหนด: {}".format(dd),"size":"xs","color":"#0066CC"})
        except: pass
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
    assigned=task.get("assigned_to","")
    if assigned:
        body.append({"type":"box","layout":"horizontal","contents":[
            {"type":"text","text":"👤 มอบหมาย:","size":"xs","color":"#888888","flex":2},
            {"type":"text","text":assigned,"size":"xs","color":"#FF6B35","flex":5,"weight":"bold"}],"margin":"sm"})
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

def build_all_persons_flex(cid):
    """งานทุกคน — แยก 1 คน = 1 bubble, carousel"""
    p=get_pending_tasks(cid)
    if not p: return aqr("🎉 ไม่มีงานค้างในกลุ่ม!")
    # Group by ผู้รับผิดชอบ (assigned_to)
    by_person={}
    for t in p:
        person=t.get("assigned_to","") or t.get("added_by","") or "ไม่ระบุ"
        by_person.setdefault(person,[]).append(t)
    bubbles=[]
    colors=["#1DB446","#FF6B35","#5B5EA6","#E91E63","#009688","#FF9800","#795548","#607D8B"]
    for idx,(person,tasks) in enumerate(by_person.items()):
        color=colors[idx % len(colors)]
        task_lines=[]
        for i,t in enumerate(tasks,1):
            due=t.get("due_date","")
            dd=""
            if due:
                try: dd=" [📅{}]".format(datetime.strptime(due,"%Y-%m-%d").strftime("%d/%m"))
                except: pass
            task_lines.append({"type":"text","text":"{}. {}{}".format(i,t["title"],dd),"size":"sm","color":"#333333","wrap":True,"margin":"sm"})
        bubble={"type":"bubble","size":"kilo",
            "header":{"type":"box","layout":"vertical","contents":[
                {"type":"text","text":"👤 {}".format(person),"weight":"bold","size":"md","color":"#FFFFFF"},
                {"type":"text","text":"{} งานค้าง".format(len(tasks)),"size":"xs","color":"#FFFFFFCC"}
            ],"paddingAll":"14px","backgroundColor":color},
            "body":{"type":"box","layout":"vertical","contents":task_lines[:15],"paddingAll":"12px","spacing":"none"}}
        bubbles.append(bubble)
    return aqr({"type":"flex","altText":"📋 งานทุกคน ({} คน, {} งาน)".format(len(by_person),len(p)),
        "contents":{"type":"carousel","contents":bubbles[:10]}})

# ── Summary with interactive link ─────────────────────────────
def build_summary(cid):
    now=datetime.now(); done=get_completed_today(cid); pend=get_pending_tasks(cid)
    di=[{"type":"text","text":"  ✔️ {}".format(t["title"]),"size":"xs","color":"#1DB446","wrap":True} for t in done] or [{"type":"text","text":"  — ยังไม่มี","size":"xs","color":"#999999"}]
    pi=[]
    for i,t in enumerate(pend,1):
        pi.append({"type":"text","text":"  {}. {}".format(i,t["title"]),"size":"xs","color":"#FF6B35","wrap":True})
    if not pi: pi.append({"type":"text","text":"  — ไม่มีงานค้าง! 🎉","size":"xs","color":"#1DB446"})
    if done and not pend: st,sc="🏆 ยอดเยี่ยม!","#1DB446"
    elif done: st,sc="👍 เสร็จ {} ค้าง {}".format(len(done),len(pend)),"#FF8C00"
    else: st,sc="💪 พรุ่งนี้สู้ใหม่!","#FF6B35"
    su=summary_page_url(cid)
    footer_contents=[{"type":"text","text":st,"size":"sm","color":sc,"weight":"bold","align":"center"}]
    if su and pend:
        footer_contents.append({"type":"button","action":{"type":"uri","label":"📋 จัดการงาน — เสร็จ / ลบ","uri":su},"style":"primary","color":"#1DB446","height":"sm","margin":"lg"})
    body_contents=[
        {"type":"text","text":"✅ เสร็จวันนี้ ({})".format(len(done)),"weight":"bold","size":"sm","color":"#1DB446"},
    ]+di+[
        {"type":"separator","margin":"lg"},
        {"type":"text","text":"⏳ งานค้าง ({})".format(len(pend)),"weight":"bold","size":"sm","color":"#FF6B35","margin":"lg"},
    ]+pi
    return aqr({"type":"flex","altText":"📊 สรุป","contents":{"type":"bubble","size":"mega",
        "header":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"📊 สรุปประจำวัน","weight":"bold","size":"lg","color":"#333333"},
            {"type":"text","text":now.strftime("%d/%m/%Y"),"size":"sm","color":"#999999"},
        ],"paddingAll":"15px","backgroundColor":"#FFF9E6"},
        "body":{"type":"box","layout":"vertical","contents":body_contents,"paddingAll":"15px","spacing":"sm"},
        "footer":{"type":"box","layout":"vertical","contents":footer_contents,"paddingAll":"12px"}}})

def get_tasks_by_person(cid):
    """แยกงานตามคน — ดูจาก assigned_to ถ้าไม่มีใช้ added_by"""
    pend=get_pending_tasks(cid)
    by_person={}
    for t in pend:
        person=t.get("assigned_to","") or t.get("added_by","") or "ไม่ระบุ"
        by_person.setdefault(person,[]).append(t)
    return pend, by_person

def get_today_tasks(cid):
    """งานที่ due วันนี้ + งานค้างที่ไม่มี due"""
    today=datetime.now().strftime("%Y-%m-%d")
    pend=get_pending_tasks(cid)
    today_tasks=[t for t in pend if t.get("due_date")==today]
    no_due=[t for t in pend if not t.get("due_date")]
    overdue=[t for t in pend if t.get("due_date") and t["due_date"]<today]
    future=[t for t in pend if t.get("due_date") and t["due_date"]>today]
    return today_tasks, no_due, overdue, future

def build_clockin(cid, uid="", name=""):
    """เข้างาน — แสดงเฉพาะงานของผู้ส่ง (overdue + วันนี้ + ไม่มีกำหนด + กำหนดล่วงหน้า)"""
    now=datetime.now()
    today=now.strftime("%Y-%m-%d")
    my=query_tasks_by_person(cid, uid=uid)
    overdue=[t for t in my if t.get("due_date") and t["due_date"]<today]
    today_tasks=[t for t in my if t.get("due_date")==today]
    no_due=[t for t in my if not t.get("due_date")]
    future=[t for t in my if t.get("due_date") and t["due_date"]>today]
    body=[]
    # งาน overdue
    if overdue:
        body.append({"type":"text","text":"🔴 งานเลยกำหนด ({})".format(len(overdue)),"weight":"bold","size":"sm","color":"#E53935","margin":"md"})
        for t in overdue:
            dd=t.get("due_date","")
            try: dd=datetime.strptime(dd,"%Y-%m-%d").strftime("%d/%m")
            except: pass
            body.append({"type":"text","text":"  ⚠️ {} (📅{})".format(t["title"],dd),"size":"xs","color":"#E53935","wrap":True})
        body.append({"type":"separator","margin":"md"})
    # งานวันนี้ + ไม่มีกำหนด
    all_today=today_tasks+no_due
    body.append({"type":"text","text":"📋 งานวันนี้ ({})".format(len(all_today)),"weight":"bold","size":"sm","color":"#1DB446","margin":"md"})
    if not all_today:
        body.append({"type":"text","text":"  — ไม่มีงานวันนี้ 🎉","size":"xs","color":"#999"})
    for t in all_today:
        body.append({"type":"text","text":"  ⬜ {}".format(t["title"]),"size":"xs","color":"#333","wrap":True})
    # งานกำหนดล่วงหน้า
    if future:
        body.append({"type":"separator","margin":"lg"})
        body.append({"type":"text","text":"📅 กำหนดไว้ ({})".format(len(future)),"weight":"bold","size":"sm","color":"#1976D2","margin":"md"})
        for t in future[:5]:
            dd=t.get("due_date","")
            try: dd=datetime.strptime(dd,"%Y-%m-%d").strftime("%d/%m")
            except: pass
            body.append({"type":"text","text":"  📆 {} — {}".format(dd,t["title"]),"size":"xs","color":"#1976D2","wrap":True})
        if len(future)>5:
            body.append({"type":"text","text":"  ...อีก {} งาน".format(len(future)-5),"size":"xs","color":"#999"})
    # สรุปจำนวน
    total=len(my)
    body.append({"type":"separator","margin":"lg"})
    body.append({"type":"text","text":"📊 รวม {} งานค้าง".format(total),"size":"sm","color":"#FF6B35","weight":"bold","margin":"md","align":"center"})
    su=summary_page_url(cid)
    footer_c=[{"type":"text","text":"💪 สู้ๆ วันนี้!","size":"sm","color":"#1DB446","align":"center","weight":"bold"}]
    if su and my:
        footer_c.append({"type":"button","action":{"type":"uri","label":"📋 จัดการงาน — เสร็จ / ลบ","uri":su},"style":"primary","color":"#1DB446","height":"sm","margin":"lg"})
    dname=name or "คุณ"
    return aqr({"type":"flex","altText":"🌅 เข้างาน","contents":{"type":"bubble","size":"mega",
        "header":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"🌅 เข้างาน — {}".format(dname),"weight":"bold","size":"lg","color":"#1DB446"},
            {"type":"text","text":"📅 {} เวลา {} น.".format(now.strftime("%d/%m/%Y"),now.strftime("%H:%M")),"size":"sm","color":"#666666","margin":"sm"}
        ],"paddingAll":"15px","backgroundColor":"#F5FFF5"},
        "body":{"type":"box","layout":"vertical","contents":body,"paddingAll":"15px","spacing":"xs"},
        "footer":{"type":"box","layout":"vertical","contents":footer_c,"paddingAll":"12px"}}})

def build_clockout(cid, uid="", name=""):
    """เลิกงาน — สรุปเฉพาะงานของผู้ส่ง (เสร็จวันนี้ + งานค้าง)"""
    now=datetime.now()
    # งานที่เสร็จวันนี้ของผู้ส่ง
    all_done=get_completed_today(cid)
    my_done=[t for t in all_done if t.get("assigned_to_uid")==uid] if uid else all_done
    # งานค้างของผู้ส่ง
    my_pend=query_tasks_by_person(cid, uid=uid)
    body=[]
    # งานเสร็จวันนี้
    body.append({"type":"text","text":"✅ เสร็จวันนี้ ({})".format(len(my_done)),"weight":"bold","size":"sm","color":"#1DB446"})
    if my_done:
        for t in my_done:
            body.append({"type":"text","text":"  ✔️ {}".format(t["title"]),"size":"xs","color":"#1DB446","wrap":True})
    else:
        body.append({"type":"text","text":"  — ยังไม่มี","size":"xs","color":"#999"})
    # งานค้าง
    body.append({"type":"separator","margin":"lg"})
    body.append({"type":"text","text":"⏳ งานค้าง ({})".format(len(my_pend)),"weight":"bold","size":"sm","color":"#FF6B35","margin":"md"})
    if my_pend:
        today=now.strftime("%Y-%m-%d")
        for i,t in enumerate(my_pend,1):
            dd=t.get("due_date","")
            tag=""
            if dd and dd<today: tag=" 🔴เลยกำหนด"
            elif dd:
                try: tag=" [📅{}]".format(datetime.strptime(dd,"%Y-%m-%d").strftime("%d/%m"))
                except: pass
            body.append({"type":"text","text":"  {}. {}{}".format(i,t["title"],tag),"size":"xs","color":"#FF6B35","wrap":True})
    else:
        body.append({"type":"text","text":"  — ไม่มีงานค้าง! 🎉","size":"xs","color":"#1DB446"})
    # สถานะ
    if my_done and not my_pend: st,sc="🏆 ยอดเยี่ยม! เคลียร์หมดแล้ว","#1DB446"
    elif my_done: st,sc="👍 เสร็จ {} ค้าง {}".format(len(my_done),len(my_pend)),"#FF8C00"
    else: st,sc="💪 พรุ่งนี้สู้ใหม่!","#FF6B35"
    body.append({"type":"separator","margin":"lg"})
    body.append({"type":"text","text":st,"size":"sm","color":sc,"weight":"bold","margin":"md","align":"center"})
    dname=name or "คุณ"
    return aqr({"type":"flex","altText":"🌆 เลิกงาน","contents":{"type":"bubble","size":"mega",
        "header":{"type":"box","layout":"vertical","contents":[
            {"type":"text","text":"🌆 เลิกงาน — {}".format(dname),"weight":"bold","size":"lg","color":"#FF6B35"},
            {"type":"text","text":"📅 {} เวลา {} น.".format(now.strftime("%d/%m/%Y"),now.strftime("%H:%M")),"size":"sm","color":"#666666","margin":"sm"}
        ],"paddingAll":"15px","backgroundColor":"#FFF3E0"},
        "body":{"type":"box","layout":"vertical","contents":body,"paddingAll":"15px","spacing":"xs"}}})

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
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"📌 @ชื่อ เพิ่ม ชื่องาน → มอบหมาย","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"text","text":"เช่น: @สมชาย เพิ่ม ส่งรายงาน,เตรียมเอกสาร","size":"xs","color":"#666666","margin":"sm"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"📌 @ชื่อ งาน → เช็คงานของคนนั้น","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"text","text":"เช่น: @สมชาย งาน","size":"xs","color":"#666666","margin":"sm"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"➕ เพิ่มหลายงาน + กำหนดวัน","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"text","text":"เช่น: เพิ่ม งาน1,งาน2,งาน3","size":"xs","color":"#666666","margin":"sm"},
            {"type":"text","text":"📅 หลายวัน:","size":"xs","color":"#666666","margin":"sm"},
            {"type":"text","text":"เพิ่ม\\nพรุ่งนี้ งาน1,งาน2\\n30/03 งาน3\\nศุกร์ งาน4","size":"xs","color":"#666666","margin":"sm"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"📋 ดูงาน → งานของฉัน","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"text","text":"📋 งาน@ชื่อ / @ชื่อ งาน → งานคนอื่น","weight":"bold","size":"sm","color":"#1DB446","margin":"sm"},
            {"type":"text","text":"📋 งานทุกคน → ดูงานทุกคนในกลุ่ม","weight":"bold","size":"sm","color":"#1DB446","margin":"sm"},
            {"type":"text","text":"📊 สรุปงาน / สรุปงาน@ชื่อ","weight":"bold","size":"sm","color":"#1DB446","margin":"sm"},
            {"type":"separator","margin":"lg"},
            {"type":"text","text":"🌅 เข้างาน / 🌆 เลิกงาน","weight":"bold","size":"sm","color":"#1DB446","margin":"lg"},
            {"type":"text","text":"เข้างาน → ดูงานวันนี้ / เลิกงาน → สรุปผล","size":"xs","color":"#666666","margin":"sm"},
        ],"paddingAll":"15px"}}})

# ── Multi-date task parser ────────────────────────────────────
DATE_LINE_RE=re.compile(
    r'^(พรุ่งนี้|มะรืน|วันนี้|tomorrow|today'
    r'|\d{1,2}/\d{1,2}(?:/\d{2,4})?'
    r'|จันทร์|อังคาร|พุธ|พฤหัส|พฤหัสบดี|ศุกร์|เสาร์|อาทิตย์)\s+(.+)',
    re.I)

def _parse_and_add_tasks(raw, cid, name, uid, assigned_to="", assigned_to_uid=""):
    """Parse raw text that may contain multi-date blocks and add tasks.
    Supported formats:
      1) Single line: งาน1,งาน2  (no date)
      2) Single line with date: พรุ่งนี้ งาน1,งาน2
      3) Multi-line with date headers:
           พรุ่งนี้ งาน1,งาน2
           30/03 งาน3,งาน4
           ศุกร์ งาน5
    """
    lines=raw.split("\n")
    lines=[l.strip() for l in lines]
    lines=[l for l in lines if l]

    added=[]
    for line in lines:
        dm=DATE_LINE_RE.match(line)
        if dm:
            date_part=dm.group(1)
            tasks_part=dm.group(2)
            due,_=parse_due_date(date_part+" dummy")
            sub_items=re.split(r'[,]+', tasks_part)
            sub_items=[re.sub(r'^\s*\d+[.)]\s*','',x).strip() for x in sub_items]
            sub_items=[x for x in sub_items if x]
            for title in sub_items:
                t=add_task(cid,title,by=name,by_uid=uid,assigned_to=assigned_to,assigned_to_uid=assigned_to_uid)
                if due: set_due_date(t["id"],due)
                added.append(get_task(t["id"]))
        else:
            sub_items=re.split(r'[,]+', line)
            sub_items=[re.sub(r'^\s*\d+[.)]\s*','',x).strip() for x in sub_items]
            sub_items=[x for x in sub_items if x]
            for it in sub_items:
                due,title=parse_due_date(it)
                if not title.strip(): continue
                t=add_task(cid,title,by=name,by_uid=uid,assigned_to=assigned_to,assigned_to_uid=assigned_to_uid)
                if due: set_due_date(t["id"],due)
                added.append(get_task(t["id"]))
    return added

# ── Text Commands ────────────────────────────────────────────
CANCEL=["ยกเลิก","cancel","ไม่","no"]
CMDS=["เพิ่ม","add","todo","เพิ่มงาน","งานค้าง","ดูงาน","list","tasks","สรุป","summary","สรุปงาน","งานทุกคน","เข้างาน","clock in","เลิกงาน","clock out","งานงาน","วิธีใช้","เมนู","menu"]

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

    if tl in ["เข้างาน","clock in","เริ่มงาน"]: return build_clockin(cid, uid, name)
    if tl in ["เลิกงาน","clock out","สรุป","summary"]: return build_clockout(cid, uid, name)
    m=re.match(r"^(?:เพิ่ม|add|todo)\s+(.+)",ts,re.I|re.S)
    if m:
        raw=m.group(1).strip()
        added=_parse_and_add_tasks(raw, cid, name, uid)
        if len(added)==1:
            return build_task_flex(added[0]["id"])
        if len(added)>1:
            cards=[build_mini_card(t,i) for i,t in enumerate(added,1)]
            return aqr({"type":"flex","altText":"➕ เพิ่ม {} งาน".format(len(added)),"contents":{"type":"carousel","contents":cards[:10]}})
        return aqr("❌ ไม่พบชื่องาน")
    if tl in ["เพิ่ม","add","todo","เพิ่มงาน"]:
        set_pending(uid,cid,"waiting_add"); return aqr("📝 พิมพ์ชื่องานเลยครับ\n(พิมพ์ \"ยกเลิก\" เพื่อยกเลิก)")
    # @ชื่อ เพิ่ม งาน1,งาน2 → มอบหมายงานให้คนอื่น (รองรับ multi-date)
    am=re.match(r"^@(\S+)\s+(?:เพิ่ม|add|todo)\s+(.+)",ts,re.I|re.S)
    if am:
        aname=am.group(1).strip(); atitle=am.group(2).strip()
        auid=""
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id,display_name FROM chat_members WHERE chat_id=%s AND display_name LIKE %s", (cid, "%"+aname+"%"))
            mr=cur.fetchone()
            if mr: aname=mr["display_name"]; auid=mr["user_id"]
        added=_parse_and_add_tasks(atitle, cid, name, uid, assigned_to=aname, assigned_to_uid=auid)
        if len(added)==1:
            return aqr("📌 มอบหมายงาน \"{}\" ให้ {} แล้ว".format(added[0]["title"],aname))
        if len(added)>1:
            return aqr("📌 มอบหมาย {} งาน ให้ {} แล้ว\n{}".format(len(added),aname,"\n".join(["  {}. {}".format(i,t["title"]) for i,t in enumerate(added,1)])))
        return aqr("❌ ไม่พบชื่องาน")
    # ── ดูงาน / งาน@ชื่อ / สรุปงาน / สรุปงาน@ชื่อ (+ optional date) ──
    # Helper: resolve @name to uid+display
    def _resolve_name(n):
        rn=n; ru=""
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id,display_name FROM chat_members WHERE chat_id=%s AND display_name LIKE %s", (cid, "%"+n+"%"))
            mr=cur.fetchone()
            if mr: rn=mr["display_name"]; ru=mr["user_id"]
        return rn, ru

    def _show_tasks(tasks, label, date_str=None):
        if not tasks: return aqr("🎉 ไม่มีงานค้าง{}{}".format(" ของ "+label if label!="ฉัน" else "ของคุณ"," ("+date_str+")" if date_str else ""))
        cards=[build_mini_card(dict(t),i) for i,t in enumerate(tasks,1)]
        alt="📋 งาน{} {} งาน".format("ของ "+label if label!="ฉัน" else "ของฉัน",len(tasks))
        return aqr({"type":"flex","altText":alt,"contents":{"type":"carousel","contents":cards[:10]}})

    # Pattern: @ชื่อ งาน [date] — e.g. "@Sun งาน", "@Sun งาน 30/03"
    am2=re.match(r"^@(\S+)\s+(?:งาน|tasks?)\s*(.*)?$",ts,re.I)
    if am2:
        aname,auid=_resolve_name(am2.group(1).strip())
        date_part=(am2.group(2) or "").strip()
        due=parse_date_only(date_part) if date_part else None
        my=query_tasks_by_person(cid, uid=auid, name=aname if not auid else None, due_date=due)
        return _show_tasks(my, aname, date_part if due else None)

    # Pattern: งาน@ชื่อ [date] — e.g. "งาน@Sun", "งาน@Sun 30/03"
    am3=re.match(r"^(?:งาน|tasks?)@(\S+)\s*(.*)?$",ts,re.I)
    if am3:
        aname,auid=_resolve_name(am3.group(1).strip())
        date_part=(am3.group(2) or "").strip()
        due=parse_date_only(date_part) if date_part else None
        my=query_tasks_by_person(cid, uid=auid, name=aname if not auid else None, due_date=due)
        return _show_tasks(my, aname, date_part if due else None)

    # Pattern: @ชื่อ สรุปงาน — e.g. "@Sun สรุปงาน"
    am4=re.match(r"^@(\S+)\s+(?:สรุปงาน|สรุป)\s*$",ts,re.I)
    if am4:
        aname,auid=_resolve_name(am4.group(1).strip())
        my=query_tasks_by_person(cid, uid=auid, name=aname if not auid else None)
        if not my: return aqr("🎉 ไม่มีงานค้างของ {}".format(aname))
        lines=["📊 สรุปงานของ {} ({} งาน)".format(aname,len(my)),""]
        for i,t in enumerate(my,1):
            dd=t.get("due_date","") or "-"
            lines.append("{}. {} [📅{}]".format(i,t["title"],dd))
        return aqr("\n".join(lines))

    # Pattern: สรุปงาน@ชื่อ — e.g. "สรุปงาน@Sun"
    am5=re.match(r"^(?:สรุปงาน|สรุป)@(\S+)\s*$",ts,re.I)
    if am5:
        aname,auid=_resolve_name(am5.group(1).strip())
        my=query_tasks_by_person(cid, uid=auid, name=aname if not auid else None)
        if not my: return aqr("🎉 ไม่มีงานค้างของ {}".format(aname))
        lines=["📊 สรุปงานของ {} ({} งาน)".format(aname,len(my)),""]
        for i,t in enumerate(my,1):
            dd=t.get("due_date","") or "-"
            lines.append("{}. {} [📅{}]".format(i,t["title"],dd))
        return aqr("\n".join(lines))

    # Pattern: ดูงาน [date] — MY tasks, optionally filtered by date
    m_view=re.match(r"^(?:ดูงาน|list|tasks)\s*(.*)?$",ts,re.I)
    if m_view:
        date_part=(m_view.group(1) or "").strip()
        due=parse_date_only(date_part) if date_part else None
        my=query_tasks_by_person(cid, uid=uid, due_date=due)
        return _show_tasks(my, "ฉัน", date_part if due else None)

    # Pattern: งานค้าง / รายการ — all tasks (no filter)
    if tl in ["งานค้าง","รายการ"]: return build_list_flex(cid)

    # Pattern: งานทุกคน — แยก 1 คน = 1 bubble
    if tl in ["งานทุกคน","all tasks","alltasks"]: return build_all_persons_flex(cid)

    # Pattern: สรุปงาน — MY summary
    if tl in ["สรุปงาน"]:
        my=query_tasks_by_person(cid, uid=uid)
        if not my: return aqr("🎉 ไม่มีงานค้างของคุณ!")
        lines=["📊 สรุปงานของคุณ ({} งาน)".format(len(my)),""]
        for i,t in enumerate(my,1):
            dd=t.get("due_date","") or "-"
            lines.append("{}. {} [📅{}]".format(i,t["title"],dd))
        return aqr("\n".join(lines))

    # Pattern: งานฉัน / งานของฉัน — alias for ดูงาน (my tasks)
    if tl in ["งานฉัน","งานของฉัน","my tasks","mytasks"]:
        my=query_tasks_by_person(cid, uid=uid)
        return _show_tasks(my, "ฉัน")

    # Pattern: [date]งาน@ชื่อ — e.g. "30/03งาน@Sun"
    am6=re.match(r"^(\S+?)(?:งาน|tasks?)@(\S+)\s*$",ts,re.I)
    if am6:
        date_part=am6.group(1).strip()
        due=parse_date_only(date_part)
        if due:
            aname,auid=_resolve_name(am6.group(2).strip())
            my=query_tasks_by_person(cid, uid=auid, name=aname if not auid else None, due_date=due)
            return _show_tasks(my, aname, date_part)

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
        except Exception as e:
            import traceback; app.logger.error("Err: %s\n%s",e,traceback.format_exc())
            try: reply_msg(tok,{"type":"text","text":"❌ error: {}".format(str(e)[:100])})
            except: pass
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

@app.route("/api/pending/<path:cid>")
def api_pending(cid):
    pend=get_pending_tasks(cid); done=get_completed_today(cid)
    return jsonify({"pending":[dict(t) for t in pend],"done_today":[dict(t) for t in done]})

@app.route("/api/batch-action",methods=["POST"])
def api_batch():
    d=request.get_json() or {}
    author=d.get("author",""); author_uid=d.get("author_uid","")
    results={"done":[],"deleted":[],"errors":[]}
    for tid in d.get("done_ids",[]):
        t=complete_task(int(tid),author,author_uid)
        if t: results["done"].append(t["title"])
        else: results["errors"].append(str(tid))
    for tid in d.get("delete_ids",[]):
        t=delete_task(int(tid),author,author_uid)
        if t: results["deleted"].append(t["title"])
        else: results["errors"].append(str(tid))
    return jsonify({"ok":True,"results":results})

@app.route("/api/members/<path:cid>")
def api_members(cid):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id,display_name FROM chat_members WHERE chat_id=%s",(cid,))
        rows = cur.fetchall()
    return jsonify([{"uid":r["user_id"],"name":r["display_name"]} for r in rows])

# ══════════════════════════════════════════════════════════════
# Task Detail Page (No LIFF SDK required)
# ══════════════════════════════════════════════════════════════
SUMMARY_PAGE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>สรุปงาน</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f5f5;padding-bottom:80px}
.hdr{background:linear-gradient(135deg,#1DB446,#17a03d);color:#fff;padding:18px 16px;text-align:center}
.hdr h1{font-size:18px;font-weight:700}.hdr p{font-size:13px;opacity:.85;margin-top:4px}
.sec{padding:10px 14px 4px;font-size:14px;font-weight:700;color:#333}
.done-sec{color:#1DB446}.pend-sec{color:#FF6B35}
.card{background:#fff;margin:6px 14px;border-radius:10px;padding:12px 14px;display:flex;align-items:center;gap:10px;box-shadow:0 1px 3px rgba(0,0,0,.08);transition:all .2s}
.card.sel-done{background:#E8F5E9;border-left:4px solid #1DB446}
.card.sel-del{background:#FFEBEE;border-left:4px solid #E53935}
.card .title{flex:1;font-size:14px;color:#333}
.card.sel-del .title{text-decoration:line-through;color:#999}
.card .by{font-size:11px;color:#999;margin-top:2px}
.btn-grp{display:flex;gap:6px}
.btn-grp button{width:36px;height:36px;border-radius:50%;border:2px solid #ddd;background:#fff;font-size:16px;cursor:pointer;transition:all .15s}
.btn-grp button.act-done{border-color:#1DB446;background:#1DB446;color:#fff}
.btn-grp button.act-del{border-color:#E53935;background:#E53935;color:#fff}
.done-item{background:#fff;margin:4px 14px;border-radius:8px;padding:10px 14px;font-size:13px;color:#1DB446}
.done-item span{margin-right:6px}
.bar{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #eee;padding:12px 16px;display:flex;gap:10px;align-items:center;z-index:99}
.bar .info{flex:1;font-size:13px;color:#666}
.bar .info b{color:#333}
.bar button{padding:10px 24px;border:none;border-radius:8px;font-size:15px;font-weight:700;cursor:pointer;color:#fff}
.bar .confirm-btn{background:#1DB446}.bar .confirm-btn:disabled{background:#ccc}
.bar .clear-btn{background:#999;padding:10px 16px}
.empty{text-align:center;padding:30px;color:#999;font-size:14px}
.result{text-align:center;padding:40px 20px}
.result .icon{font-size:48px}.result h2{margin:12px 0 8px;font-size:18px}
.result p{font-size:13px;color:#666}
.toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.8);color:#fff;padding:10px 20px;border-radius:20px;font-size:14px;display:none;z-index:999}
</style></head>
<body>
<div class="hdr"><h1>📊 สรุปประจำวัน</h1><p id="dateText"></p></div>
<div id="main"></div>
<div class="bar" id="actionBar" style="display:none">
  <div class="info" id="barInfo"></div>
  <button class="clear-btn" onclick="clearAll()">ล้าง</button>
  <button class="confirm-btn" id="confirmBtn" onclick="confirmAll()">ยืนยัน</button>
</div>
<div class="toast" id="toast"></div>
<script>
var API="",chatId,selections={},tasks=[];
function init(){
  var sp=new URLSearchParams(location.search);
  chatId=sp.get("chat_id");
  if(!chatId){document.getElementById("main").innerHTML='<div class="empty">chat_id not found</div>';return}
  document.getElementById("dateText").textContent=new Date().toLocaleDateString("th-TH",{day:"2-digit",month:"2-digit",year:"numeric"});
  load();
}
async function load(){
  try{
    var r=await fetch(API+"/api/pending/"+encodeURIComponent(chatId));
    if(!r.ok)throw new Error("load fail");
    var d=await r.json();tasks=d.pending;
    render(d.done_today,d.pending);
  }catch(e){document.getElementById("main").innerHTML='<div class="empty">โหลดไม่ได้: '+e.message+'</div>'}
}
function render(done,pend){
  var h='<div class="sec done-sec">✅ เสร็จวันนี้ ('+done.length+')</div>';
  if(done.length==0)h+='<div class="done-item"><span>—</span> ยังไม่มี</div>';
  else done.forEach(function(t){h+='<div class="done-item"><span>✔️</span>'+esc(t.title)+'</div>'});
  h+='<div class="sec pend-sec">⏳ งานค้าง ('+pend.length+')</div>';
  if(pend.length==0)h+='<div class="empty">🎉 ไม่มีงานค้าง!</div>';
  else pend.forEach(function(t,i){
    var cls="card";var sel=selections[t.id];
    if(sel=="done")cls+=" sel-done";else if(sel=="delete")cls+=" sel-del";
    h+='<div class="'+cls+'" id="card-'+t.id+'">';
    h+='<div style="flex:1"><div class="title">'+(i+1)+'. '+esc(t.title)+'</div><div class="by">สั่งโดย: '+(t.added_by||"-")+'</div></div>';
    h+='<div class="btn-grp">';
    h+='<button onclick="toggle('+t.id+',&quot;done&quot;)" class="'+(sel=="done"?"act-done":"")+'">✓</button>';
    h+='<button onclick="toggle('+t.id+',&quot;delete&quot;)" class="'+(sel=="delete"?"act-del":"")+'">✕</button>';
    h+='</div></div>'});
  document.getElementById("main").innerHTML=h;
  updateBar();
}
function toggle(tid,action){
  if(selections[tid]==action)delete selections[tid];else selections[tid]=action;
  var card=document.getElementById("card-"+tid);
  card.className="card"+(selections[tid]=="done"?" sel-done":selections[tid]=="delete"?" sel-del":"");
  var btns=card.querySelectorAll(".btn-grp button");
  btns[0].className=selections[tid]=="done"?"act-done":"";
  btns[1].className=selections[tid]=="delete"?"act-del":"";
  updateBar();
}
function updateBar(){
  var doneIds=[],delIds=[];
  for(var k in selections){if(selections[k]=="done")doneIds.push(k);else delIds.push(k)}
  var total=doneIds.length+delIds.length;
  var bar=document.getElementById("actionBar");
  if(total==0){bar.style.display="none";return}
  bar.style.display="flex";
  var info="";
  if(doneIds.length>0)info+='<b style="color:#1DB446">✅ เสร็จ '+doneIds.length+'</b> ';
  if(delIds.length>0)info+='<b style="color:#E53935">🗑️ ลบ '+delIds.length+'</b>';
  document.getElementById("barInfo").innerHTML=info;
}
function clearAll(){selections={};load()}
async function confirmAll(){
  var doneIds=[],delIds=[];
  for(var k in selections){if(selections[k]=="done")doneIds.push(parseInt(k));else delIds.push(parseInt(k))}
  if(doneIds.length+delIds.length==0)return;
  document.getElementById("confirmBtn").disabled=true;document.getElementById("confirmBtn").textContent="กำลังทำ...";
  try{
    var r=await fetch(API+"/api/batch-action",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({done_ids:doneIds,delete_ids:delIds,author:"ผู้ใช้",author_uid:""})});
    if(!r.ok)throw new Error("fail");
    var d=await r.json();
    var msg='<div class="result"><div class="icon">✅</div><h2>จัดการเรียบร้อย!</h2>';
    if(d.results.done.length>0)msg+='<p style="color:#1DB446">เสร็จ: '+d.results.done.join(", ")+'</p>';
    if(d.results.deleted.length>0)msg+='<p style="color:#E53935">ลบแล้ว: '+d.results.deleted.join(", ")+'</p>';
    msg+='<p style="margin-top:16px;color:#999">กำลังกลับไปแชท...</p></div>';
    document.getElementById("main").innerHTML=msg;
    document.getElementById("actionBar").style.display="none";
    setTimeout(function(){location.href="https://line.me/R/"},1500);
  }catch(e){toast("ทำไม่ได้ ลองใหม่");document.getElementById("confirmBtn").disabled=false;document.getElementById("confirmBtn").textContent="ยืนยัน"}
}
function esc(s){var d=document.createElement("div");d.textContent=s;return d.innerHTML}
function toast(m){var t=document.getElementById("toast");t.textContent=m;t.style.display="block";setTimeout(function(){t.style.display="none"},2500)}
init();
</script></body></html>"""

TASK_PAGE_HTML = """<!DOCTYPE html>
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
.cbar{position:fixed;bottom:0;left:0;right:0;border-top:1px solid #eee;padding:8px 12px;display:flex;gap:8px;background:#fff;z-index:20;transition:bottom .15s}
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
  var sp=new URLSearchParams(location.search);
  taskId=sp.get("task_id");
  if(!taskId){var ls=sp.get("liff.state");if(ls){var lp=new URLSearchParams(ls.replace(/^\?/,""));taskId=lp.get("task_id")}}
  userName=decodeURIComponent(sp.get("name")||"");
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
function confirmDone(){showConfirm("✅ ยืนยันเสร็จ?","งาน: "+task.title,async function(){
  var r=await fetch(API+"/api/task/"+taskId+"/done",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({author:gn(),author_uid:""})});
  if(r.ok){document.getElementById("app").innerHTML='<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:60vh"><div style="font-size:48px">✅</div><div style="font-size:18px;font-weight:bold;color:#43A047;margin-top:12px">เสร็จแล้ว!</div><div style="font-size:13px;color:#999;margin-top:6px">กำลังกลับไปแชท...</div></div>';setTimeout(function(){location.href="https://line.me/R/"},1200)}else{toast("ทำไม่ได้ ลองใหม่")}})}
function confirmDelete(){showConfirm("⚠️ ยืนยันลบ?","ลบแล้วกู้คืนไม่ได้!",async function(){
  var r=await fetch(API+"/api/task/"+taskId+"/delete",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({author:gn(),author_uid:""})});
  if(r.ok){document.getElementById("app").innerHTML='<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:60vh"><div style="font-size:48px">🗑️</div><div style="font-size:18px;font-weight:bold;color:#E53935;margin-top:12px">ลบงานแล้ว</div><div style="font-size:13px;color:#999;margin-top:6px">กำลังกลับไปแชท...</div></div>';setTimeout(function(){location.href="https://line.me/R/"},1200)}
  else{toast("ลบไม่ได้ ลองใหม่")}})}
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
(function(){var ci=document.getElementById("cinput");if(!ci)return;
ci.addEventListener("focus",function(){setTimeout(function(){ci.scrollIntoView({block:"center",behavior:"smooth"});if(window.visualViewport){document.querySelector(".cbar").style.bottom=(window.innerHeight-window.visualViewport.height)+"px"}},300)});
ci.addEventListener("blur",function(){document.querySelector(".cbar").style.bottom="0"});
if(window.visualViewport){window.visualViewport.addEventListener("resize",function(){var cb=document.querySelector(".cbar");if(document.activeElement===ci){cb.style.bottom=(window.innerHeight-window.visualViewport.height)+"px"}else{cb.style.bottom="0"}})}})();
init();
</script></body></html>"""

@app.route("/liff/task")
def task_page():
    from flask import make_response
    resp = make_response(TASK_PAGE_HTML)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

@app.route("/liff/summary")
def summary_page():
    from flask import make_response
    resp = make_response(SUMMARY_PAGE_HTML)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

# (auto summary removed)

@app.route("/uploads/<path:fname>")
def serve_upload(fname):
    from flask import send_from_directory
    upload_dir = os.path.join(os.path.dirname(DATABASE_PATH) or ".", "uploads")
    return send_from_directory(upload_dir, fname)

@app.route("/", methods=["GET"])
def health(): return "LINE Todo Bot v6 running!"

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
