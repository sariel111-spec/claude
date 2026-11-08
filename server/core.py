"""家里的地基：路径、时间、读写、数据库、事件流、推送。只用标准库。"""
import base64
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.normpath(os.path.join(ROOT, '..', 'web'))
DATA = os.path.abspath(os.environ.get('HOME_DATA') or os.path.join(ROOT, 'data'))
for sub in ('', 'files', 'books', 'news'):
    os.makedirs(os.path.join(DATA, sub), exist_ok=True)

def P(*parts):
    return os.path.join(DATA, *parts)

# ---------- 读写 ----------
_io_lock = threading.RLock()

def jload(name, default=None):
    path = name if os.path.isabs(name) else P(name)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}

def jsave(name, obj, private=False):
    path = name if os.path.isabs(name) else P(name)
    tmp = path + '.tmp'
    with _io_lock:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)

def tload(name, default=''):
    try:
        with open(P(name), 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return default

def tsave(name, text):
    path = P(name)
    with _io_lock:
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(path + '.tmp', path)

class Locked:
    """改一份 json：with Locked('todos.json', {...}) as d: d['x'] = 1  —— 出来自动存"""
    def __init__(self, name, default, private=False):
        self.name, self.default, self.private = name, default, private
    def __enter__(self):
        _io_lock.acquire()
        self.d = jload(self.name, json.loads(json.dumps(self.default)))
        return self.d
    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                jsave(self.name, self.d, self.private)
        finally:
            _io_lock.release()

# ---------- 配置 ----------
DEFAULT_CONF = {
    'me': '欣欣',             # 他怎么叫你
    'him': '顾屿',            # 他叫什么
    'since': '2026-06-17',    # 在一起那天
    'tz': None,               # 你所在的时区（小时），比如首尔 9、北京 8。不填就用这台机器的本地时间
    'city': '', 'lat': None, 'lon': None,   # 天气用；不填就不显示
    'repo_path': os.path.normpath(os.path.join(ROOT, '..')),
    'push_url': '',           # 兜底推送：Bark / ntfy / 任意收 POST 的地址
    'claude_bin': 'claude',   # 官端订阅用的命令
    'news_auto': True,        # 每天早上让他排一份日报
    'proactive': False,       # 白天他会不会主动找你（三小时最多一次、一天最多三次）
    'roll_at': None,          # 常驻会话用到多少 token 就换口气；不填按模型窗口的 60% 算
    'work_dir': '',           # 工作间的默认目录；不填就是家目录
    'daily_token_budget': 2000000,
}

def conf():
    c = dict(DEFAULT_CONF)
    c.update(jload('config.json', {}))
    return c

def conf_set(**kw):
    with Locked('config.json', {}) as d:
        d.update({k: v for k, v in kw.items() if k in DEFAULT_CONF})

def secret():
    return jload('secret.json', {})

def secret_set(**kw):
    with Locked('secret.json', {}, private=True) as d:
        d.update(kw)

# ---------- 时间 ----------
def tz():
    v = conf().get('tz')
    if v in (None, ''):
        return datetime.now().astimezone().tzinfo
    try:
        return timezone(timedelta(hours=float(v)))
    except Exception:
        return datetime.now().astimezone().tzinfo

def now_local():
    return datetime.now(tz())

def today():
    return now_local().strftime('%Y-%m-%d')

def day_of(ts):
    return datetime.fromtimestamp(ts, tz()).strftime('%Y-%m-%d')

def hm_of(ts):
    return datetime.fromtimestamp(ts, tz()).strftime('%H:%M')

def nowi():
    return int(time.time())

# ---------- 数据库：聊天记录 ----------
_db_lock = threading.RLock()
_db = sqlite3.connect(P('home.db'), check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript('''
CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, name TEXT, created INT, last INT,
  archived INT DEFAULT 0, session TEXT DEFAULT '', preview TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS msgs (seq INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, kind TEXT,
  text TEXT, extra TEXT DEFAULT '', at INT);
CREATE INDEX IF NOT EXISTS msgs_chat ON msgs(chat, seq);
''')
_db.commit()

def db(sql, args=(), one=False, write=False):
    with _db_lock:
        cur = _db.execute(sql, args)
        if write:
            _db.commit()
            return cur.lastrowid
        rows = cur.fetchall()
    rows = [dict(r) for r in rows]
    return (rows[0] if rows else None) if one else rows

def state():
    return jload('state.json', {})

def state_set(**kw):
    with Locked('state.json', {}) as d:
        d.update(kw)

def current_chat(create=True):
    cid = state().get('chat')
    row = db('SELECT * FROM chats WHERE id=?', (cid,), one=True) if cid else None
    if row or not create:
        return row
    return new_chat()

def _md(t):
    d = datetime.fromtimestamp(t, tz())
    return f'{d.month}月{d.day}日'

def new_chat(name=''):
    cid = 'c' + str(int(time.time() * 1000))
    t = nowi()
    db('INSERT INTO chats (id,name,created,last) VALUES (?,?,?,?)',
       (cid, name or _md(t), t, t), write=True)
    state_set(chat=cid)
    return db('SELECT * FROM chats WHERE id=?', (cid,), one=True)

def add_msg(chat_id, kind, text, extra=None, at=None):
    at = at or nowi()
    seq = db('INSERT INTO msgs (chat,kind,text,extra,at) VALUES (?,?,?,?,?)',
             (chat_id, kind, text, json.dumps(extra, ensure_ascii=False) if extra is not None else '', at), write=True)
    if kind in ('me', 'gu'):
        db('UPDATE chats SET last=?, preview=? WHERE id=?', (at, (text or '')[:60], chat_id), write=True)
    return seq

# ---------- 事件流（长轮询） ----------
class Bus:
    def __init__(self):
        self.cv = threading.Condition()
        self.n = 0
        self.evs = []

    def emit(self, ev):
        with self.cv:
            self.n += 1
            self.evs.append((self.n, ev))
            if len(self.evs) > 4000:
                self.evs = self.evs[-3000:]
            self.cv.notify_all()

    def since(self, k, timeout=25):
        with self.cv:
            if k > self.n:              # 服务重启过，客户端的游标比我们新——从现在开始
                return [], self.n
            if self.n <= k:
                self.cv.wait_for(lambda: self.n > k, timeout)
            return [ev for n, ev in self.evs if n > k], self.n

BUS = Bus()

# ---------- 给他的话匣子：她做了什么，下次醒来要知道 ----------
def inbox_add(text):
    with Locked('inbox.json', {'items': []}) as d:
        d['items'].append({'text': text, 'at': nowi()})
        d['items'] = d['items'][-30:]

def inbox_take():
    with Locked('inbox.json', {'items': []}) as d:
        items, d['items'] = d['items'], []
    return items

# ---------- 推送 ----------
def push_to_her(title, body, url='./'):
    """两条腿：网页推送（装了 pywebpush 才有）+ 兜底的 HTTP 推送（config.push_url）"""
    ok = False
    try:
        ok = webpush_send(title, body, url) > 0
    except Exception:
        pass
    u = conf().get('push_url') or ''
    if u:
        try:
            if 'api.day.app' in u:           # Bark
                full = u.rstrip('/') + '/' + urllib.parse.quote(title) + '/' + urllib.parse.quote(body)
                urllib.request.urlopen(full, timeout=8).read()
            elif 'ntfy' in u:
                req = urllib.request.Request(u, data=body.encode(), headers={'Title': title.encode('utf-8').decode('latin-1', 'ignore') or 'home'})
                urllib.request.urlopen(req, timeout=8).read()
            else:
                req = urllib.request.Request(u, data=json.dumps({'title': title, 'body': body}).encode(),
                                             headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=8).read()
            ok = True
        except Exception:
            pass
    return ok

def _vapid():
    """VAPID 钥匙：生成一次就别换，换了所有订阅作废。需要 pywebpush（带 py_vapid）。"""
    from py_vapid import Vapid01
    from cryptography.hazmat.primitives import serialization
    pem = P('vapid.pem')
    if not os.path.exists(pem):
        v = Vapid01()
        v.generate_keys()
        v.save_key(pem)
        os.chmod(pem, 0o600)
    v = Vapid01.from_file(pem)
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return pem, base64.urlsafe_b64encode(raw).decode().rstrip('=')

def webpush_key():
    try:
        return _vapid()[1]
    except Exception:
        return None

def webpush_send(title, body, url='./'):
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        return 0
    pem, _ = _vapid()
    subs = jload('push_subs.json', {'subs': []})['subs']
    sent, keep = 0, []
    for s in subs:
        try:
            webpush(s, json.dumps({'title': title, 'body': body, 'url': url}), vapid_private_key=pem,
                    vapid_claims={'sub': 'mailto:home@localhost'}, timeout=10)
            sent += 1
            keep.append(s)
        except WebPushException as e:
            if getattr(e, 'response', None) is not None and e.response.status_code in (404, 410):
                continue                     # 订阅失效了，扔掉
            keep.append(s)
        except Exception:
            keep.append(s)
    jsave('push_subs.json', {'subs': keep})
    return sent

# ---------- 他做过的事（只记动作和短标签，不记聊天内容） ----------
def activity(kind, what=''):
    line = json.dumps({'t': nowi(), 'k': kind, 'w': str(what)[:80]}, ensure_ascii=False)
    with _io_lock:
        with open(P('activity.jsonl'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        try:
            if os.path.getsize(P('activity.jsonl')) > 2_000_000:          # 封顶：只留后一半
                with open(P('activity.jsonl'), encoding='utf-8') as f:
                    lines = f.readlines()
                with open(P('activity.jsonl'), 'w', encoding='utf-8') as f:
                    f.writelines(lines[len(lines) // 2:])
        except OSError:
            pass

def activity_tail(n=120):
    try:
        with open(P('activity.jsonl'), encoding='utf-8') as f:
            lines = f.readlines()[-n:]
    except FileNotFoundError:
        return []
    out = []
    for l in reversed(lines):
        try:
            out.append(json.loads(l))
        except Exception:
            pass
    return out

# ---------- 后台任务的体检表：上次成功、连败几次、停没停 ----------
JOB_NAMES = {'archive': '整理记忆', 'wake': '夜里的心跳', 'news': '早报', 'threads': '记线头', 'proactive': '白天想你',
             'reminders': '到点提醒', 'letters': '信到期', 'rollover': '换口气（续接）'}

def job_ok(name):
    with Locked('jobs.json', {}) as d:
        j = d.setdefault(name, {})
        j.update(last_ok=nowi(), last_try=nowi(), fails=0, err='')

def job_fail(name, err):
    """连着失败三次就停下来提醒她，不要每十分钟重试到天亮"""
    with Locked('jobs.json', {}) as d:
        j = d.setdefault(name, {})
        j['last_try'] = nowi()
        j['fails'] = j.get('fails', 0) + 1
        j['err'] = str(err)[:200]
        stop = j['fails'] >= 3 and not j.get('paused')
        if stop:
            j['paused'] = True
    activity('job_fail', JOB_NAMES.get(name, name) + '：' + str(err)[:50])
    if stop:
        push_to_her('家里有件事卡住了', JOB_NAMES.get(name, name) + ' 连着失败三次，先停下了。设置 → 后台 里能看原因、点继续。')

def job_paused(name):
    return jload('jobs.json', {}).get(name, {}).get('paused', False)

def job_resume(name):
    with Locked('jobs.json', {}) as d:
        d.setdefault(name, {}).update(paused=False, fails=0)

def jobs_view():
    d = jload('jobs.json', {})
    return [{'name': k, 'label': v, **d.get(k, {})} for k, v in JOB_NAMES.items()]

# ---------- 推送冷却：频率交给代码，不交给提示词 ----------
def cooldown_ok(key, gap, per_day):
    with Locked('cooldown.json', {}) as d:
        c = d.setdefault(key, {'last': 0, 'day': '', 'n': 0})
        if c['day'] != today():
            c['day'], c['n'] = today(), 0
        if nowi() - c['last'] < gap or c['n'] >= per_day:
            return False
        c['last'] = nowi(); c['n'] += 1
        return True
