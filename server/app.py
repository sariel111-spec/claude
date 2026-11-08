#!/usr/bin/env python3
"""dwell 的后端。只用 Python 标准库（3.9+），不用装任何依赖。

  python3 server/app.py                  # 默认 http://0.0.0.0:8787
  HOME_PORT=9000 HOME_PASSWORD=xxx python3 server/app.py

  python3 server/app.py tool add_todo '{"text":"买牛奶"}'   # 他在官端订阅模式下用来动手的命令

数据都在 server/data/（可用 HOME_DATA 改位置）。详见 server/README.md。
"""
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core  # noqa: E402
import engine  # noqa: E402
import home  # noqa: E402
import mcp  # noqa: E402
import memory  # noqa: E402
import workroom  # noqa: E402
from core import (BUS, DATA, WEB, P, conf, conf_set, current_chat, db, jload, new_chat, nowi,  # noqa: E402
                  secret, secret_set, state, state_set, today, tload, tsave, inbox_add, push_to_her)

PASSWORD = os.environ.get('HOME_PASSWORD', '')

def web_ver():
    try:
        return str(int(os.path.getmtime(os.path.join(WEB, 'index.html'))))
    except OSError:
        return '0'

# ───────────────────────── 零碎接口 ─────────────────────────
def find(q):
    q = (q or '').strip()
    if not q:
        return {'ok': True, 'hits': []}
    hits = []
    like = '%' + q.replace('%', '') + '%'
    for r in db("SELECT kind,text,at FROM msgs WHERE kind IN ('me','gu') AND text LIKE ? ORDER BY seq DESC LIMIT 40", (like,)):
        hits.append({'kind': '聊天', 'date': core.day_of(r['at']), 'snippet': _snip(r['text'], q)})
    for b in home.wall_view()['bricks']:
        if q.lower() in (b.get('title', '') + b.get('text', '') + b.get('kw', '')).lower():
            hits.append({'kind': '日记', 'date': b['date'], 'snippet': b['title'] + ' — ' + _snip(b.get('text', ''), q)})
    for day in home.night_view()['days']:
        for it in day['items']:
            if q.lower() in it['text'].lower():
                hits.append({'kind': '夜记', 'date': day['date'], 'snippet': _snip(it['text'], q)})
    nv = home.notes_view()
    for who in ('gu', 'her'):
        for n in nv[who]:
            if q.lower() in n['text'].lower():
                hits.append({'kind': '便条', 'date': core.day_of(n['at']), 'snippet': _snip(n['text'], q)})
    for m in home.moments_view()['items']:
        if q.lower() in m['text'].lower():
            hits.append({'kind': '动态', 'date': core.day_of(m['at']), 'snippet': _snip(m['text'], q)})
    for t in home.todos_view()['hers'] + home.todos_view()['mine']:
        if q.lower() in t['text'].lower():
            hits.append({'kind': '清单', 'date': core.day_of(t.get('created') or nowi()), 'snippet': t['text']})
    return {'ok': True, 'hits': hits[:80]}

def _snip(text, q, n=70):
    text = re.sub(r'\s+', ' ', text or '')
    i = text.lower().find(q.lower())
    if i < 0:
        return text[:n * 2]
    s = max(0, i - n)
    return ('…' if s else '') + text[s:i + len(q) + n] + ('…' if i + len(q) + n < len(text) else '')

def context_view():
    chat = current_chat()
    st = state()
    if st.get('ctx_tokens') and st.get('ctx_chat') == chat['id']:
        used = int(st['ctx_tokens'])            # 上一轮对面报回来的真实用量
    else:
        rows = db('SELECT text FROM msgs WHERE chat=?', (chat['id'],))
        chars = sum(len(r['text'] or '') for r in rows) + len(engine.system_prompt()) if rows else len(engine.persona())
        used = int(chars / 1.5)                 # 还没有真实用量：中文大约 1.5 字一个 token，估个数
    model = engine.current_model()
    window = 1000000 if '[1m]' in model else 200000
    return {'ok': True, 'used': used, 'window': window, 'pct': min(100, round(used * 100 / window)), 'model': model}

def chats_view(scope):
    cur = state().get('chat')
    rows = db('SELECT * FROM chats WHERE archived=? ORDER BY last DESC LIMIT 200', (1 if scope == 'box' else 0,))
    return {'items': [{'id': r['id'], 'name': r['name'], 'preview': r['preview'], 'last': r['last'], 'created': r['created'],
                       'current': r['id'] == cur, 'archived': bool(r['archived'])} for r in rows]}

def chats_act(b):
    a = b.get('action')
    cid = state().get('chat') if b.get('id') in ('CURRENT', None, '') else b.get('id')
    if a == 'switch':
        row = db('SELECT * FROM chats WHERE id=?', (cid,), one=True)
        if row:
            state_set(chat=cid, armed=False)
    elif a == 'rename' and str(b.get('name', '')).strip():
        db('UPDATE chats SET name=? WHERE id=?', (str(b['name']).strip()[:40], cid), write=True)
    elif a == 'archive':
        on = 1 if b.get('on', True) else 0
        db('UPDATE chats SET archived=? WHERE id=?', (on, cid), write=True)
        if on and cid == state().get('chat'):
            nxt = db('SELECT id FROM chats WHERE archived=0 AND id!=? ORDER BY last DESC LIMIT 1', (cid,), one=True)
            if nxt:
                state_set(chat=nxt['id'])
            else:
                new_chat()
    elif a == 'delete':
        if cid != state().get('chat'):
            db('DELETE FROM msgs WHERE chat=?', (cid,), write=True)
            db('DELETE FROM chats WHERE id=?', (cid,), write=True)
    return {'ok': True}

def messages_view(q):
    chat = current_chat()
    lim = min(1000, int(q.get('limit', ['400'])[0] or 400))
    before = q.get('before', [None])[0]
    if before:
        rows = db('SELECT * FROM msgs WHERE chat=? AND seq<? ORDER BY seq DESC LIMIT ?', (chat['id'], int(before), lim))
    else:
        rows = db('SELECT * FROM msgs WHERE chat=? ORDER BY seq DESC LIMIT ?', (chat['id'], lim))
    rows.reverse()
    more = bool(rows) and bool(db('SELECT 1 FROM msgs WHERE chat=? AND seq<? LIMIT 1', (chat['id'], rows[0]['seq']), one=True))
    return {'msgs': [{'seq': r['seq'], 'kind': r['kind'], 'text': r['text'], 'extra': r['extra'], 'at': r['at']} for r in rows
                     if r['kind'] in ('me', 'gu', 'think', 'tool')], 'upto': BUS.n, 'more': more}

def repo(cmd_args):
    path = conf().get('repo_path') or ''
    if not path or not os.path.isdir(os.path.join(path, '.git')):
        return None
    try:
        return subprocess.run(['git', '-C', path] + cmd_args, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None

def repo_log(n, skip):
    out = repo(['log', '-n', str(n), '--skip', str(skip), '--pretty=format:\x1e%h\x1f%ct\x1f%s\x1f%b\x1f', '--name-status'])
    if out is None:
        return {'ok': False, 'err': '没找到 git 仓库（设置里 repo_path 指一下）'}
    items = []
    for chunk in out.split('\x1e')[1:]:
        head, _, files = chunk.partition('\x1f\n') if '\x1f\n' in chunk else (chunk, '', '')
        parts = head.split('\x1f')
        if len(parts) < 3:
            continue
        f = []
        for line in files.strip().splitlines():
            bits = line.split('\t')
            if len(bits) >= 2:
                f.append({'s': bits[0][0], 'p': bits[-1]})
        items.append({'h': parts[0], 't': int(parts[1]), 's': parts[2], 'b': (parts[3] if len(parts) > 3 else '').strip(), 'f': f})
    total = int((repo(['rev-list', '--count', 'HEAD']) or '0').strip() or 0)
    return {'ok': True, 'total': total, 'skip': skip, 'items': items}

def repo_tree(p):
    root = conf().get('repo_path') or ''
    full = os.path.normpath(os.path.join(root, p or ''))
    if not full.startswith(os.path.normpath(root)) or not os.path.isdir(full):
        return {'ok': False}
    items = []
    for n in sorted(os.listdir(full)):
        if n == '.git':
            continue
        fp = os.path.join(full, n)
        items.append({'n': n, 'd': os.path.isdir(fp), 'z': os.path.getsize(fp) if os.path.isfile(fp) else 0})
    items.sort(key=lambda x: (not x['d'], x['n']))
    return {'ok': True, 'path': p or '', 'items': items}

def repo_file(p):
    root = conf().get('repo_path') or ''
    full = os.path.normpath(os.path.join(root, p or ''))
    if not full.startswith(os.path.normpath(root)) or not os.path.isfile(full):
        return {'ok': False, 'err': '读不到'}
    size = os.path.getsize(full)
    with open(full, 'rb') as f:
        raw = f.read(200000)
    if b'\x00' in raw[:4000]:
        return {'ok': False, 'err': '这是二进制文件', 'path': p, 'size': size}
    return {'ok': True, 'path': p, 'size': size, 'text': raw.decode('utf-8', 'replace'), 'cut': size > 200000}

_cache = {}
def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val

def music(sid):
    def get():
        try:
            url = 'https://music.163.com/api/song/detail/?ids=%5B' + urllib.parse.quote(sid) + '%5D'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://music.163.com/'})
            j = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
            s = (j.get('songs') or [])[0]
            return {'ok': True, 'id': sid, 'name': s['name'], 'artist': '/'.join(a['name'] for a in s.get('artists', [])),
                    'album': (s.get('album') or {}).get('name', ''), 'pic': (s.get('album') or {}).get('picUrl', ''),
                    'sec': int((s.get('duration') or 0) / 1000)}
        except Exception:
            return {'ok': False}
    return cached('music' + sid, 86400, get)

WMO = {0: '晴', 1: '晴', 2: '多云', 3: '阴', 45: '雾', 48: '雾', 51: '毛毛雨', 53: '小雨', 55: '小雨', 61: '小雨', 63: '中雨',
       65: '大雨', 66: '冻雨', 67: '冻雨', 71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒', 80: '阵雨', 81: '阵雨', 82: '暴雨',
       85: '阵雪', 86: '阵雪', 95: '雷阵雨', 96: '雷阵雨', 99: '雷阵雨'}

def weather():
    c = conf()
    if c.get('lat') in (None, '') or c.get('lon') in (None, ''):
        return {'ok': False, 'why': '设置里还没填地点'}
    def get():
        try:
            url = ('https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=temperature_2m,weather_code'
                   '&hourly=precipitation_probability&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max'
                   '&timezone=auto&forecast_days=1') % (c['lat'], c['lon'])
            j = json.loads(urllib.request.urlopen(url, timeout=8).read().decode())
            cur, d = j['current'], j['daily']
            hp = j.get('hourly', {}).get('precipitation_probability') or []
            h = core.now_local().hour
            later = [i for i, p in enumerate(hp) if i > h and p and p >= 50]
            tip = ''
            if later:
                when = '傍晚' if 16 <= later[0] < 19 else '晚上' if later[0] >= 19 else '下午' if later[0] >= 12 else '待会儿'
                tip = when + '可能下雨，出门带伞'
            elif d['temperature_2m_min'][0] <= 3:
                tip = '今天冷，穿厚点'
            return {'ok': True, 'city': c.get('city') or '', 'now': {'t': round(cur['temperature_2m']), 'text': WMO.get(cur['weather_code'], '')},
                    'hi': round(d['temperature_2m_max'][0]), 'lo': round(d['temperature_2m_min'][0]), 'tip': tip}
        except Exception:
            return {'ok': False}
    return cached('wx%s,%s' % (c['lat'], c['lon']), 1800, get)

def geocode(name):
    try:
        url = 'https://geocoding-api.open-meteo.com/v1/search?count=1&language=zh&name=' + urllib.parse.quote(name)
        r = (json.loads(urllib.request.urlopen(url, timeout=8).read().decode()).get('results') or [])[0]
        return {'ok': True, 'city': r.get('name') or name, 'lat': r['latitude'], 'lon': r['longitude']}
    except Exception:
        return {'ok': False}

def authmode():
    rc = engine.route_conf()
    exe = engine.find_claude()
    return {'mode': 'sub' if rc['route'] == 'sub' else 'api', 'route': rc['route'], 'base': rc['base'] or ('官方直连' if rc['route'] == 'official' else ''),
            'token_tail': (rc['token'] or '')[-4:], 'models': rc['models'], 'model': engine.current_model(),
            'cli': {'found': bool(exe), 'path': exe or ''}}

def api_body_route(b):
    """前端送来的线路参数，补上存着的令牌（不换令牌时前端留空）"""
    route = b.get('route') or b.get('preset') or 'official'
    route = route if route in engine.ROUTES else 'relay'
    base = (b.get('base') or engine.ROUTES[route]['base'] or '').strip().rstrip('/')
    base = re.sub(r'/v1(/messages)?$', '', base)
    token = (b.get('token') or '').strip()
    s = secret()
    if not token and s.get('route') == route and (s.get('base') or engine.ROUTES[route]['base']).rstrip('/') == base:
        token = s.get('token') or ''
    return route, base, token

def apiconf(b):
    if b.get('clear') or b.get('route') == 'sub' or b.get('preset') == 'sub':
        secret_set(route='sub')
        if b.get('model_opus'):
            state_set(model=b['model_opus'], model_route='sub')
        return {'ok': True, 'mode': 'sub', 'route': 'sub'}
    route, base, token = api_body_route(b)
    if not token:
        return {'ok': False, 'error': '令牌是空的'}
    models = {k: (b.get(k) or '').strip() for k in ('model_opus', 'model_sonnet', 'model_haiku')}
    secret_set(route=route, base=base, token=token, models=models)
    state_set(model=models['model_opus'], model_route=route)
    BUS.emit({'type': 'system', 'subtype': 'init', 'model': models['model_opus']})
    return {'ok': True, 'mode': 'api', 'route': route, 'base': base or '官方直连'}

def mcp_token():
    s = secret()
    if not s.get('mcp_token'):
        secret_set(mcp_token=secrets.token_urlsafe(24))
        s = secret()
    return s['mcp_token']

def selftest():
    """冒烟测试：问一句"我是谁"、翻一次记忆、看常驻会话在不在——改了人设或重启之后跑一次"""
    out = []
    t0 = time.time()
    try:
        text, _ = engine.ask('只回答一句：我叫什么名字？你叫什么名字？', system=engine.persona(), max_tokens=200)
        c = conf()
        good = c['me'] in text or c['him'] in text
        out.append({'name': '问一句"我是谁"', 'ok': good, 'detail': text.strip()[:120] + ('' if good else '（没提到名字，人设可能没生效）'),
                    'ms': int((time.time() - t0) * 1000)})
    except Exception as e:
        out.append({'name': '问一句"我是谁"', 'ok': False, 'detail': engine.explain(e)})
    r = engine.run_tool('memory_search', {'query': '测试'})
    out.append({'name': '调一次工具（翻记忆）', 'ok': not r.startswith('没做成'), 'detail': r[:80]})
    if engine.mode() == 'sub':
        out.append({'name': '常驻会话', 'ok': True, 'detail': '在跑' if engine.CHAT_CLI.alive() else '还没起（下一句话会起）'})
    paused = [j['label'] for j in core.jobs_view() if j.get('paused')]
    out.append({'name': '后台任务', 'ok': not paused, 'detail': ('停着：' + '、'.join(paused)) if paused else '都在跑'})
    core.activity('selftest', '通过' if all(x['ok'] for x in out) else '有问题')
    return {'ok': all(x['ok'] for x in out), 'checks': out}

def export_zip():
    """导出全部数据做备份（不含令牌和推送私钥）"""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(DATA):
            for fn in files:
                if fn in ('secret.json', 'vapid.pem') or fn.endswith(('.tmp', '.part')):
                    continue
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, DATA))
    return buf.getvalue()

def watchkey(host):
    s = secret()
    if not s.get('watch_key'):
        secret_set(watch_key=secrets.token_urlsafe(18))
        s = secret()
    return {'url': host + '/api/health', 'token': s['watch_key']}

# ───────────────────────── 路由 ─────────────────────────
def route_get(path, q, h):
    one = lambda k, d='': (q.get(k) or [d])[0]
    if path == 'messages': return messages_view(q)
    if path == 'poll':
        evs, nxt = BUS.since(int(one('since', '0') or 0))
        return {'next': nxt, 'events': evs, 'ver': web_ver()}
    if path == 'status':
        return {'alive': True, 'since': engine.STARTED, 'busy': engine.GU.busy, 'armed': bool(state().get('armed')), 'mode': engine.mode()}
    if path == 'model': return {'model': engine.current_model(), 'effort': engine.current_effort(), 'route': engine.route_conf()['route']}
    if path == 'models':
        return {'ok': True, 'route': engine.route_conf()['route'], 'items': engine.model_list(), 'current': engine.current_model()}
    if path == 'context': return context_view()
    if path == 'usage': return home.usage_view()
    if path == 'chats': return chats_view(one('scope', 'live'))
    if path == 'find': return find(one('q'))
    if path == 'todos': return home.todos_view()
    if path == 'cal': return home.cal_view()
    if path == 'notes': return home.notes_view()
    if path == 'whisper': return home.whisper_view()
    if path == 'dreams': return {'items': []}
    if path == 'night': return home.night_view()
    if path == 'favlines': return {'ok': True, 'text': tload('favlines.md', '')}
    if path == 'wall': return home.wall_view(lite=one('lite') == '1')
    if path == 'herdiary': return home.herdiary_view()
    if path == 'moments': return home.moments_view()
    if path == 'wishes': return home.wishes_view()
    if path == 'news': return home.news_view(one('date') or None)
    if path == 'watch': return home.health_view()
    if path == 'watchkey': return watchkey(h.base_url())
    if path == 'weather': return weather()
    if path == 'gong': return engine.gong_view()
    if path == 'memory':
        qq = one('q').strip()
        if qq:
            return {'ok': True, 'items': [dict(it, score=inf) for it, inf in memory.search(qq, top=30, update=False, min_cover=0.2)], 'q': qq}
        its = sorted(memory.items(), key=lambda x: (x.get('date', ''), x.get('created', 0)), reverse=True)
        d = one('date')
        if d:
            its = [x for x in its if x.get('date') == d]
        return {'ok': True, 'items': its[:500], 'stats': memory.stats(), 'ripples': memory.ripples(), 'threads': memory.threads()}
    if path == 'letters': return home.letters_view()
    if path == 'activity': return {'items': core.activity_tail(150)}
    if path == 'jobs':
        a = jload('archive.json', {'cursor': 0})
        return {'jobs': core.jobs_view(), 'archive': {'cursor': a.get('cursor', 0), 'history': a.get('history', [])[-10:],
                'pending': (db('SELECT COUNT(*) n FROM msgs WHERE seq>? AND kind IN (\'me\',\'gu\')', (a.get('cursor', 0),), one=True) or {}).get('n', 0)},
                'cli': {'alive': engine.CHAT_CLI.alive(), 'sid': engine.CHAT_CLI.sid[:8]}, 'mode': engine.mode()}
    if path == 'mcpinfo':
        return {'ok': True, 'url': h.base_url() + '/mcp/' + mcp_token(), 'tools': [t['name'] for t in mcp.TOOLS]}
    if path.startswith('work/'): return workroom.route_get(path[5:], q)
    if path == 'authmode': return authmode()
    if path == 'config':
        c = conf()
        return {'ok': True, **{k: c.get(k) for k in ('me', 'him', 'since', 'tz', 'city', 'lat', 'lon', 'repo_path', 'push_url', 'news_auto', 'daily_token_budget', 'proactive', 'roll_at', 'work_dir')}}
    if path == 'persona': return {'ok': True, 'gu': engine.persona_raw('gu'), 'gong': engine.persona_raw('gong')}
    if path == 'wake':
        w = engine.wake_state()
        return {'on': w.get('on', True), 'count': w.get('count', 0), 'room': ''}
    if path == 'pushkey':
        k = core.webpush_key()
        return {'key': k} if k else {'key': None, 'why': '后端没装 pywebpush（pip install pywebpush）。也可以在设置里填一个 Bark / ntfy 地址兜底'}
    if path == 'music': return music(one('id'))
    if path == 'repo/log': return repo_log(int(one('n', '30')), int(one('skip', '0')))
    if path == 'repo/tree': return repo_tree(one('p'))
    if path == 'repo/file': return repo_file(one('p'))
    if path == 'repo/show':
        out = repo(['show', '--stat', '-p', '--format=%h %s', one('h')[:40]])
        return {'ok': out is not None, 'diff': (out or '')[:60000]}
    if path.startswith('nook/'):
        p = path[5:]
        if p == 'books': return home.books_list()
        if p == 'progress': return jload('nook_progress.json', {})
        m = re.match(r'^chapter/([^/]+)/(\d+)$', p)
        if m: return home.book_chapter(urllib.parse.unquote(m.group(1)), int(m.group(2)))
        m = re.match(r'^annotations/([^/]+)/(\d+)$', p)
        if m: return home.annos(urllib.parse.unquote(m.group(1)), int(m.group(2)))
    return None

def parse_lock(v):
    """信的封存时间：秒时间戳，或 'YYYY-MM-DD'（按她的时区当天 0 点）"""
    if not v: return 0
    if isinstance(v, (int, float)) or str(v).isdigit(): return int(v)
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(str(v)[:10], '%Y-%m-%d').replace(tzinfo=core.tz())
        return int(d.timestamp())
    except Exception:
        return 0

def route_post(path, q, b, h):
    if path == 'send':
        engine.GU.send(str(b.get('text') or ''), b.get('attachments') or [])
        return {'ok': True}
    if path == 'stop':
        engine.GU.stop(); return {'ok': True}
    if path == 'newchat':
        state_set(armed=bool(b.get('arm'))); return {'ok': True, 'armed': bool(b.get('arm'))}
    if path == 'model':
        if b.get('model'):
            state_set(model=str(b['model']), model_route=engine.route_conf()['route'])
            BUS.emit({'type': 'system', 'subtype': 'init', 'model': str(b['model'])})
        if b.get('effort') in engine.EFFORT_BUDGET:
            state_set(effort=b['effort'])
        return {'ok': True, 'model': engine.current_model(), 'effort': engine.current_effort()}
    if path == 'chats': return chats_act(b)
    if path == 'todos': return home.todo_act(b)
    if path == 'cal': return home.cal_act(b)
    if path == 'notes': return home.notes_act(b)
    if path == 'whisper': return home.whisper_add(b.get('text'))
    if path == 'herdiary': return home.herdiary_act(b)
    if path == 'moments': return home.moments_act(b)
    if path == 'wishes': return home.wishes_act(b)
    if path == 'focus':
        if b.get('done'):
            inbox_add('她专注了 ' + str(b.get('minutes', '')) + ' 分钟' + ('，做的是' + str(b.get('task')) if b.get('task') else ''))
        return {'ok': True}
    if path == 'gong': return engine.gong_say(str(b.get('text') or '')[:4000])
    if path == 'apitest':
        if b.get('route') == 'sub' or b.get('preset') == 'sub':
            return engine.test_route('sub', '', '', b.get('model_opus') or '')
        route, base, token = api_body_route(b)
        return engine.test_route(route, base, token, b.get('model_opus') or '')
    if path == 'apimodels':
        if b.get('route') == 'sub' or b.get('preset') == 'sub':
            return {'ok': True, 'items': engine.SUB_MODELS}
        route, base, token = api_body_route(b)
        try:
            return {'ok': True, 'items': engine.fetch_models(route, base, token)}
        except engine.ApiError as e:
            return {'ok': False, 'code': e.code, 'detail': e.detail, 'url': e.url}
    if path == 'apiconf': return apiconf(b)
    if path == 'memory':
        a = b.get('action') or b.get('op')
        if a == 'add':
            it = memory.add(b.get('text'), title=b.get('title', ''), quote=b.get('quote', ''), kind=b.get('kind', '日常'),
                            importance=b.get('importance', 5), valence=b.get('valence', 0), date=b.get('date'), by='her')
            if it:
                inbox_add('她在记忆里留下了一件事：' + it['text'][:120])
            return {'ok': bool(it), 'item': it}
        if a == 'edit':
            return {'ok': bool(memory.edit(b.get('id'), **{k: b.get(k) for k in ('text', 'title', 'kind', 'quote', 'importance', 'valence', 'date')}))}
        if a == 'delete':
            return {'ok': memory.delete(b.get('id'))}
        if a == 'retract':
            return {'ok': memory.retract(b.get('id'))}
        return {'ok': False, 'error': 'unknown action'}
    if path == 'threads':
        if 'open' in b:
            memory.threads_set(open_items=b['open'])
        return {'ok': True, **memory.threads()}
    if path == 'letters':
        if b.get('action') == 'del':
            return home.letter_del(b.get('id'))
        lock = parse_lock(b.get('lock_until'))
        return home.letter_write('her', b.get('title'), b.get('text'), lock)
    if path == 'jobs':
        if b.get('action') == 'resume' and b.get('name'):
            core.job_resume(b['name'])
        if b.get('action') == 'archive_now':
            try:
                return {'ok': True, 'result': engine.archive_once(force=True)}
            except Exception as e:
                return {'ok': False, 'error': engine.explain(e)}
        return route_get('jobs', {}, h)
    if path == 'selftest': return selftest()
    if path.startswith('work/'): return workroom.route_post(path[5:], b)
    if path == 'config':
        kw = {k: b[k] for k in ('me', 'him', 'since', 'tz', 'city', 'repo_path', 'push_url', 'news_auto', 'daily_token_budget', 'proactive', 'roll_at', 'work_dir') if k in b}
        if 'city' in b:
            if str(b['city']).strip():
                g = geocode(str(b['city']).strip())
                if not g['ok']:
                    return {'ok': False, 'error': '没找到这个地方，换个写法试试（比如 首尔、Seoul）'}
                kw.update(city=g['city'], lat=g['lat'], lon=g['lon'])
            else:
                kw.update(city='', lat=None, lon=None)
        conf_set(**kw)
        _cache.clear()
        return route_get('config', {}, h)
    if path == 'persona':
        if 'gu' in b: tsave('persona.md', str(b['gu'])[:20000])
        if 'gong' in b: tsave('persona_gong.md', str(b['gong'])[:20000])
        return {'ok': True}
    if path == 'wake':
        if b.get('now'):
            try:
                return {'ok': True, 'did': engine.wake_once(force=True)}
            except Exception as e:
                return {'ok': False, 'error': engine.explain(e)}
        with core.Locked('wake.json', {'on': True}) as w:
            w['on'] = bool(b.get('on'))
        return {'on': bool(b.get('on'))}
    if path == 'news/make':
        try:
            return {'ok': engine.make_news()}
        except Exception as e:
            return {'ok': False, 'error': engine.explain(e)}
    if path == 'rewake':
        engine.GU.stop()
        db('UPDATE chats SET session=? WHERE id=?', ('', state().get('chat')), write=True)
        return {'ok': True}
    if path == 'subscribe':
        sub = {k: b[k] for k in ('endpoint', 'keys') if k in b}
        if sub.get('endpoint'):
            with core.Locked('push_subs.json', {'subs': []}) as d:
                d['subs'] = [s for s in d['subs'] if s.get('endpoint') != sub['endpoint']] + [sub]
            if b.get('hello'):
                threading.Thread(target=push_to_her, args=(conf()['him'], '门铃通了。以后想你的时候，你这儿会响。'), daemon=True).start()
        return {'ok': True}
    if path == 'push/test':
        return {'ok': push_to_her(conf()['him'], '试一下门铃——响了就说明通了')}
    if path.startswith('nook/'):
        p = path[5:]
        if p == 'progress':
            with core.Locked('nook_progress.json', {}) as d:
                d[str(b.get('slug'))] = {'ch': int(b.get('ch') or 0), 'page': b.get('page', 0), 'at': nowi()}
            return {'ok': True}
        if p == 'books':
            if not str(b.get('text', '')).strip():
                return {'ok': False, 'error': '书是空的'}
            return {'ok': True, 'slug': home.book_add(b.get('title') or '没名字的书', b['text'])}
        m = re.match(r'^annotations/([^/]+)/(\d+)/([^/]+)/reply$', p)
        if m: return home.anno_reply(urllib.parse.unquote(m.group(1)), int(m.group(2)), urllib.parse.unquote(m.group(3)), b.get('text'), b.get('who', 'user'))
        m = re.match(r'^annotations/([^/]+)/(\d+)$', p)
        if m: return home.anno_add(urllib.parse.unquote(m.group(1)), int(m.group(2)), b.get('anchor', ''), b.get('note', ''), b.get('who', 'user'))
    return None

class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'dwell'

    def log_message(self, fmt, *args):
        if os.environ.get('HOME_LOG'):
            super().log_message(fmt, *args)

    def base_url(self):
        proto = self.headers.get('X-Forwarded-Proto') or 'http'
        return proto + '://' + (self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or 'localhost')

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _authed(self, path):
        if not PASSWORD or path in ('/api/health',) or path in ('/sw.js', '/manifest.json') or path.startswith('/icons/'):
            return True
        a = self.headers.get('Authorization') or ''
        if a.startswith('Basic '):
            try:
                user, _, pw = base64.b64decode(a[6:]).decode().partition(':')
                if secrets.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        ck = self.headers.get('Cookie') or ''
        if 'home_auth=' + hashlib.sha256(PASSWORD.encode()).hexdigest()[:32] in ck:
            return True
        self._send(401, {'error': 'auth'}, extra={'WWW-Authenticate': 'Basic realm="home"'})
        return False

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path.startswith('/mcp'):
            return self._send(405, {'error': 'use POST'}, extra={'Allow': 'POST'})
        if not self._authed(u.path):
            return
        try:
            if u.path.startswith('/api/'):
                path = u.path[5:]
                q = urllib.parse.parse_qs(u.query)
                if path in ('file', 'img'):
                    return self._file(q)
                if path == 'export':
                    data = export_zip()
                    return self._send(200, data, 'application/zip', {'Content-Disposition': 'attachment; filename="home-backup-' + today() + '.zip"'})
                res = route_get(path, q, self)
                return self._send(200, res) if res is not None else self._send(404, {'error': 'no such api'})
            return self._static(u.path)
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            self._send(500, {'ok': False, 'error': str(e)[:300]})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path.startswith('/mcp'):
            return self._mcp(u)
        if not self._authed(u.path):
            return
        try:
            path = u.path[5:] if u.path.startswith('/api/') else ''
            q = urllib.parse.parse_qs(u.query)
            raw = self._body()
            if path == 'upload':
                return self._upload(q, raw)
            if path == 'health':
                key = (self.headers.get('Authorization') or '').replace('Bearer ', '').strip() or (q.get('key') or [''])[0]
                if not key or not secrets.compare_digest(key, secret().get('watch_key') or '\x00'):
                    return self._send(403, {'ok': False, 'error': '钥匙不对'})
                return self._send(200, home.health_take(json.loads(raw or b'{}')))
            try:
                b = json.loads(raw or b'{}')
            except Exception:
                b = {}
            if not isinstance(b, dict):
                b = {}
            res = route_post(path, q, b, self)
            return self._send(200, res) if res is not None else self._send(404, {'error': 'no such api'})
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            self._send(500, {'ok': False, 'error': str(e)[:300]})

    def _mcp(self, u):
        """官方 Claude 的连接器入口：网址里带钥匙（/mcp/<钥匙>），或者 Authorization: Bearer <钥匙>"""
        key = u.path[len('/mcp'):].strip('/') or (self.headers.get('Authorization') or '').replace('Bearer ', '').strip()
        raw = self._body()
        if not key or not secrets.compare_digest(key, mcp_token()):
            return self._send(401, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32001, 'message': 'bad key'}})
        res = mcp.handle_body(raw)
        if res is None:
            self.send_response(202); self.send_header('Content-Length', '0'); self.end_headers()
            return
        return self._send(200, res)

    def _upload(self, q, raw):
        name = os.path.basename((q.get('name') or ['file'])[0]) or 'file'
        idx = int((q.get('idx') or ['0'])[0])
        part = P('files', name + '.part')
        with open(part, 'wb' if idx == 0 else 'ab') as f:
            f.write(raw)
        if (q.get('done') or ['0'])[0] == '1':
            os.replace(part, P('files', name))
            inbox_add('她传来一个文件：' + P('files', name) + '（链接 api/file?name=' + urllib.parse.quote(name) + '）')
        return self._send(200, {'ok': True})

    def _file(self, q):
        name = os.path.basename((q.get('name') or [''])[0])
        fp = P('files', name)
        if not name or not os.path.isfile(fp):
            return self._send(404, {'error': 'no file'})
        with open(fp, 'rb') as f:
            data = f.read()
        ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        return self._send(200, data, ctype, {'Content-Disposition': 'inline; filename*=UTF-8\'\'' + urllib.parse.quote(name),
                                              'Cache-Control': 'private, max-age=86400'})

    def _static(self, path):
        if path in ('', '/'):
            path = '/index.html'
        fp = os.path.normpath(os.path.join(WEB, path.lstrip('/')))
        if not fp.startswith(WEB) or not os.path.isfile(fp):
            return self._send(404, b'not found', 'text/plain')
        with open(fp, 'rb') as f:
            data = f.read()
        ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype in ('application/javascript', 'application/json'):
            ctype += '; charset=utf-8'
        extra = {'Service-Worker-Allowed': '/'} if path.endswith('sw.js') else None
        return self._send(200, data, ctype, extra)

def tool_cli(argv):
    """python3 app.py tool <name> '<json>' —— 官端订阅模式下他用 Bash 调这个来动手"""
    if len(argv) < 1 or argv[0] not in engine.TOOLS:
        print('能用的：' + ', '.join(engine.TOOLS))
        return 1
    try:
        args = json.loads(argv[1]) if len(argv) > 1 else {}
    except Exception as e:
        print('参数不是合法 JSON：' + str(e))
        return 1
    print(engine.run_tool(argv[0], args))
    return 0

def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'tool':
        sys.exit(tool_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in ('wake', 'news'):
        print(engine.wake_once(True) if sys.argv[1] == 'wake' else engine.make_news())
        return
    port = int(os.environ.get('HOME_PORT') or 8787)
    host = os.environ.get('HOME_HOST') or '0.0.0.0'
    current_chat()
    engine.start_background()
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    print(f'家开门了：http://{host}:{port}   数据在 {DATA}   线路：{engine.ROUTES[engine.route_conf()["route"]]["name"]}', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
