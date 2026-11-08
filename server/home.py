"""家里的东西：清单、日历、便条、动态、心愿、日记、共读、身体……
每样东西一个 json 文件，网页和他（工具 / 命令行）改的是同一份。"""
import os
import re
import time

from core import (Locked, P, day_of, inbox_add, jload, jsave, nowi, tload, tsave, today, conf,
                  now_local)

def _nid(d, key='next_id', start=1):
    d[key] = d.get(key, start) + 1
    return d[key] - 1

# ---------- 清单 ----------
TODO0 = {'hers': [], 'mine': [], 'next_id': 1}

def _todo_out(t):
    t = dict(t)
    if t.get('fixed'):                       # 每天的事：今天勾过才算做完，过了零点自己复原
        t['done'] = t.get('done_day') == today()
    return t

def todos_view():
    d = jload('todos.json', TODO0)
    return {'ok': True, 'hers': [_todo_out(t) for t in d.get('hers', [])],
            'mine': [_todo_out(t) for t in d.get('mine', [])]}

def todo_act(b):
    lst = b.get('list') if b.get('list') in ('hers', 'mine') else 'hers'
    with Locked('todos.json', TODO0) as d:
        L = d.setdefault(lst, [])
        a = b.get('action')
        if a == 'add' and str(b.get('text', '')).strip():
            L.append({'id': _nid(d), 'text': str(b['text']).strip()[:200], 'at': b.get('at') or '',
                      'by': b.get('by') or 'her', 'fixed': bool(b.get('fixed')), 'done': False, 'created': nowi()})
        else:
            t = next((x for x in L if x.get('id') == b.get('id')), None)
            if t and a == 'toggle':
                if t.get('fixed'):
                    t['done_day'] = '' if t.get('done_day') == today() else today()
                else:
                    t['done'] = not t.get('done')
                    t['done_at'] = nowi() if t['done'] else None
                if lst == 'hers' and (t.get('done') or t.get('done_day') == today()):
                    inbox_add('她勾掉了清单上的「' + t['text'] + '」')
            elif t and a == 'del':
                L.remove(t)
        # 做完超过三天的普通项自己收走，清单别越攒越长
        for k in ('hers', 'mine'):
            d[k] = [x for x in d.get(k, []) if x.get('fixed') or not x.get('done') or nowi() - (x.get('done_at') or nowi()) < 3 * 86400]
    return todos_view()

# ---------- 日历 + 心情 ----------
CAL0 = {'events': [], 'period': {'days': {}}, 'next_id': 1}

def cal_view():
    d = jload('cal.json', CAL0)
    return {'ok': True, 'cal': {'events': d.get('events', []), 'period': d.get('period', {'days': {}})}, 'predict': None}

def cal_act(b):
    with Locked('cal.json', CAL0) as d:
        a = b.get('action')
        if a == 'add_event' and str(b.get('text', '')).strip() and re.match(r'^\d{4}-\d{2}-\d{2}$', str(b.get('date', ''))):
            d['events'].append({'id': _nid(d), 'date': b['date'], 'time': b.get('time') or '',
                                'text': str(b['text']).strip()[:200], 'yearly': bool(b.get('yearly')),
                                'type': 'special' if b.get('special') or b.get('type') == 'special' else 'event'})
        elif a == 'del_event':
            d['events'] = [e for e in d['events'] if e.get('id') != b.get('id')]
        elif a == 'day_record' and b.get('date'):
            rec = {k: b.get(k) for k in ('flow', 'pain', 'mood', 'note', 'private') if b.get(k)}
            days = d.setdefault('period', {}).setdefault('days', {})
            old = days.get(b['date'], {})
            if rec.get('mood') and rec.get('mood') != old.get('mood') and b['date'] == today():
                inbox_add('她把今天的心情记成了「' + rec['mood'] + '」')
            if rec:
                days[b['date']] = rec
            else:
                days.pop(b['date'], None)
    return cal_view()

def events_on(ds):
    return [e for e in jload('cal.json', CAL0).get('events', [])
            if e.get('date') == ds or (e.get('yearly') and e.get('date', '')[5:] == ds[5:])]

# ---------- 便条 / 悄悄话 ----------
NOTES0 = {'gu': [], 'her': [], 'next_id': 1}

def notes_view():
    d = jload('notes.json', NOTES0)
    return {'gu': d.get('gu', []), 'her': d.get('her', [])}

def notes_act(b):
    who = 'gu' if b.get('who') == 'gu' else 'her'
    with Locked('notes.json', NOTES0) as d:
        L = d.setdefault(who, [])
        a = b.get('action')
        if a == 'add' and str(b.get('text', '')).strip():
            L.insert(0, {'id': 'n' + str(_nid(d)), 'text': str(b['text']).strip()[:1000], 'at': nowi()})
            if who == 'her':
                inbox_add('她在留言板贴了一张：' + str(b['text']).strip()[:200])
        else:
            n = next((x for x in L if x.get('id') == b.get('id')), None)
            if n and a == 'box':
                n['boxed'] = not n.get('boxed')
            elif n and a == 'del':
                L.remove(n)
    return notes_view()

def whisper_view():
    return {'items': jload('whisper.json', {'items': []}).get('items', [])}

def whisper_add(text, who='her'):
    text = str(text or '').strip()[:2000]
    if text:
        with Locked('whisper.json', {'items': []}) as d:
            d['items'].append({'who': who, 'text': text, 'at': nowi()})
    return whisper_view()

# ---------- 日记那一页 ----------
def night_view():
    d = jload('night.json', {'days': {}}).get('days', {})
    return {'days': [{'date': k, 'items': d[k]} for k in sorted(d, reverse=True)[:30]]}

def night_add(text, t=None):
    text = str(text or '').strip()
    if not text:
        return
    with Locked('night.json', {'days': {}}) as d:
        n = now_local()
        night = day_of(time.time() - 6 * 3600)      # 凌晨写的算前一天"夜里"
        d['days'].setdefault(night, []).append({'t': t or n.strftime('%H:%M'), 'text': text[:2000]})

def wall_view(lite=False):
    bricks = jload('wall.json', {'bricks': []}).get('bricks', [])
    if lite:
        bricks = [{k: v for k, v in b.items() if k != 'text'} for b in bricks]
    return {'ok': True, 'bricks': bricks}

def wall_add(date, title, kw='', text='', s=3, v=0, a=3):
    clamp = lambda x, lo, hi, dv: max(lo, min(hi, int(x))) if str(x).lstrip('+-').isdigit() else dv
    with Locked('wall.json', {'bricks': []}) as d:
        d['bricks'].append({'date': date or today(), 'title': str(title)[:60], 'kw': str(kw)[:60], 'text': str(text)[:3000],
                            's': clamp(s, 1, 5, 3), 'v': clamp(v, -5, 5, 0), 'a': clamp(a, 1, 5, 3)})
        d['bricks'].sort(key=lambda b: b['date'])

def herdiary_view():
    return {'items': jload('herdiary.json', {'items': []}).get('items', [])}

def herdiary_act(b):
    with Locked('herdiary.json', {'items': [], 'next_id': 1}) as d:
        if b.get('action') == 'add' and str(b.get('text', '')).strip():
            d['items'].insert(0, {'id': _nid(d), 'text': str(b['text']).strip()[:5000], 'at': nowi()})
        elif b.get('action') == 'del':
            d['items'] = [x for x in d['items'] if x.get('id') != b.get('id')]
    return herdiary_view()

def favline_add(quote, note=''):
    quote = str(quote or '').strip()
    if not quote:
        return
    cur = tload('favlines.md', '# 摘下来的话\n')
    cur = cur.rstrip() + '\n\n---\n\n**' + today() + '**\n> "' + quote.replace('\n', ' ') + '"\n' + ('\n' + str(note).strip() + '\n' if note else '')
    tsave('favlines.md', cur)

# ---------- 动态 ----------
MOM0 = {'items': [], 'next_id': 1}

def moments_view():
    return {'items': jload('moments.json', MOM0).get('items', [])}

def moments_act(b, who='her'):
    with Locked('moments.json', MOM0) as d:
        a = b.get('action')
        m = next((x for x in d['items'] if x.get('id') == b.get('id')), None)
        if a == 'post' and str(b.get('text', '')).strip():
            d['items'].append({'id': 'm' + str(_nid(d)), 'who': who, 'text': str(b['text']).strip()[:2000],
                               'at': nowi(), 'likes': [], 'comments': []})
        elif m and a == 'like':
            m['likes'] = [x for x in m['likes'] if x != who] if who in m['likes'] else m['likes'] + [who]
            if who == 'her' and m['who'] == 'gu' and who in m['likes']:
                inbox_add('她给你的动态点了个赞：「' + m['text'][:40] + '」')
        elif m and a == 'comment' and str(b.get('text', '')).strip():
            m['comments'].append({'who': who, 'text': str(b['text']).strip()[:500], 'at': nowi()})
            if who == 'her':
                inbox_add('她在动态「' + m['text'][:30] + '」下面说：' + str(b['text']).strip()[:200]
                          + '（动态 id ' + m['id'] + '，想回就用 moment_comment）')
        elif m and a == 'del' and (m['who'] == who):
            d['items'].remove(m)
    return moments_view()

# ---------- 心愿：券包 + 愿望单 ----------
WISH0 = {'coupons': [], 'wishes': [], 'next_id': 1}

def wishes_view():
    d = jload('wishes.json', WISH0)
    return {'coupons': d.get('coupons', []), 'wishes': d.get('wishes', [])}

def wishes_act(b, who='her'):
    with Locked('wishes.json', WISH0) as d:
        a = b.get('action')
        t = nowi()
        if a == 'use':
            c = next((x for x in d['coupons'] if x.get('id') == b.get('id')), None)
            if c and not c.get('used_at') and c.get('from') != who:
                c['used_at'] = t
                if who == 'her':
                    inbox_add('她用掉了你送的券「' + c['title'] + '」' + ('（' + c['note'] + '）' if c.get('note') else '') + '——这次要兑现')
        elif a == 'give' and str(b.get('title', '')).strip():
            d['coupons'].append({'id': 'c' + str(_nid(d)), 'from': who, 'title': str(b['title']).strip()[:30],
                                 'note': str(b.get('note') or '').strip()[:120], 'at': t})
            if who == 'her':
                inbox_add('她送了你一张券「' + str(b['title']).strip() + '」')
        elif a == 'wish' and str(b.get('text', '')).strip():
            d['wishes'].insert(0, {'id': 'w' + str(_nid(d)), 'who': who, 'text': str(b['text']).strip()[:200], 'at': t})
            if who == 'her':
                inbox_add('她在愿望单上挂了一条：' + str(b['text']).strip())
        elif a == 'wish_done':
            w = next((x for x in d['wishes'] if x.get('id') == b.get('id')), None)
            if w:
                w['done_at'] = None if w.get('done_at') else t
        elif a == 'del':
            key = 'wishes' if b.get('kind') == 'wish' else 'coupons'
            d[key] = [x for x in d[key] if x.get('id') != b.get('id')]
    return wishes_view()

# ---------- 日报 ----------
def news_view(date=None):
    files = sorted(f[:-3] for f in os.listdir(P('news')) if re.match(r'^\d{4}-\d{2}-\d{2}\.md$', f))
    if not files:
        return {'ok': False}
    date = date if date in files else files[-1]
    return {'ok': True, 'date': date, 'dates': files, 'text': tload(os.path.join('news', date + '.md'))}

def news_save(text, date=None):
    tsave(os.path.join('news', (date or today()) + '.md'), text)

# ---------- 共读 ----------
def _slug(s):
    s = re.sub(r'[^\w一-鿿-]+', '-', str(s)).strip('-').lower()
    return s[:40] or ('book' + str(nowi()))

def split_chapters(text):
    text = text.replace('\r\n', '\n')
    pat = re.compile(r'^(?:#{1,3}\s+.+|第[0-9零一二三四五六七八九十百千]+[章节回卷部].*|Chapter\s+\d+.*)$', re.M | re.I)
    marks = [m for m in pat.finditer(text)]
    chs = []
    if len(marks) >= 2:
        if text[:marks[0].start()].strip():
            chs.append({'title': '开头', 'text': text[:marks[0].start()].strip()})
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            chs.append({'title': m.group(0).lstrip('#').strip()[:40], 'text': text[m.end():end].strip()})
    else:                                   # 没章节标题：按段落凑，每节三千来字
        buf, n = [], 0
        for para in text.split('\n'):
            buf.append(para); n += len(para)
            if n > 3000:
                chs.append({'title': '第 ' + str(len(chs) + 1) + ' 节', 'text': '\n'.join(buf).strip()}); buf, n = [], 0
        if '\n'.join(buf).strip():
            chs.append({'title': '第 ' + str(len(chs) + 1) + ' 节', 'text': '\n'.join(buf).strip()})
    return [c for c in chs if c['text']] or [{'title': '全文', 'text': text.strip()}]

def book_add(title, text):
    slug = _slug(title)
    while os.path.exists(P('books', slug + '.json')):
        slug += '-1'
    jsave(os.path.join('books', slug + '.json'), {'title': str(title)[:60], 'chapters': split_chapters(text)})
    inbox_add('她往共读书架上放了一本《' + str(title) + '》')
    return slug

def books_list():
    out = []
    for f in sorted(os.listdir(P('books'))):
        if f.endswith('.json'):
            b = jload(os.path.join('books', f), {})
            out.append({'slug': f[:-5], 'title': b.get('title', f[:-5]), 'chapters': [c['title'] for c in b.get('chapters', [])]})
        elif f.endswith(('.txt', '.md')) and not os.path.exists(P('books', f.rsplit('.', 1)[0] + '.json')):
            # 直接丢进 books/ 的文本文件：第一次见到时转成 json
            with open(P('books', f), encoding='utf-8', errors='ignore') as fh:
                text = fh.read()
            slug = f.rsplit('.', 1)[0]
            jsave(os.path.join('books', slug + '.json'), {'title': slug, 'chapters': split_chapters(text)})
            out.append({'slug': slug, 'title': slug, 'chapters': [c['title'] for c in split_chapters(text)]})
    return out

def book_chapter(slug, idx):
    b = jload(os.path.join('books', os.path.basename(slug) + '.json'), None)
    if not b:
        return {'error': 'no book'}
    chs = b.get('chapters', [])
    i = max(0, min(int(idx), len(chs) - 1))
    return {'book': b.get('title'), 'index': i, 'total': len(chs), 'title': chs[i]['title'],
            'chapters': [c['title'] for c in chs], 'text': chs[i]['text']}

def annos(slug, ch):
    return jload('annotations.json', {}).get(os.path.basename(slug) + '/' + str(ch), [])

def anno_add(slug, ch, anchor, note, who='user'):
    key = os.path.basename(slug) + '/' + str(ch)
    with Locked('annotations.json', {}) as d:
        L = d.setdefault(key, [])
        a = next((x for x in L if x.get('anchor') == anchor), None)
        if not a:
            a = {'id': 'a' + str(nowi()) + str(len(L)), 'anchor': str(anchor)[:280], 'note': '', 'who': who, 'replies': [], 'at': nowi()}
            L.append(a)
        if note:
            a['replies'].append({'who': 'her' if who == 'user' else who, 'text': str(note)[:1000], 'at': nowi()})
    if who == 'user':
        b = jload(os.path.join('books', os.path.basename(slug) + '.json'), {})
        inbox_add('她在共读《' + b.get('title', slug) + '》里划了一句「' + str(anchor)[:60] + '」' + ('，写着：' + str(note)[:120] if note else '')
                  + '（book=' + slug + ' ch=' + str(ch) + '，想回就用 nook_reply）')
    return annos(slug, ch)

def anno_reply(slug, ch, aid, text, who='user'):
    key = os.path.basename(slug) + '/' + str(ch)
    with Locked('annotations.json', {}) as d:
        a = next((x for x in d.get(key, []) if x.get('id') == aid or x.get('anchor') == aid), None)
        if a and str(text).strip():
            a['replies'].append({'who': 'her' if who == 'user' else who, 'text': str(text)[:1000], 'at': nowi()})
    if who == 'user':
        inbox_add('她在共读批注里回了一句：' + str(text)[:150])
    return annos(slug, ch)

# ---------- 身体 ----------
UNITS = {'sleep_hours': '小时', 'sleep_avg7': '小时', 'sleep_in_bed': '小时', 'sleep_deep': '小时', 'sleep_rem': '小时',
         'sleep_core': '小时', 'sleep_awake': '次', 'sleep_heart_rate': 'bpm', 'heart_rate': 'bpm', 'resting_heart_rate': 'bpm',
         'heart_rate_min': 'bpm', 'heart_rate_max': 'bpm', 'walking_heart_rate': 'bpm', 'hrv': 'ms', 'cardio_recovery': 'bpm',
         'blood_oxygen': '%', 'respiratory_rate': '次/分', 'vo2max': '', 'steps': '步', 'distance_km': '公里', 'flights': '层',
         'active_energy': '千卡', 'resting_energy': '千卡', 'exercise_minutes': '分钟', 'stand_hours': '小时',
         'wrist_temp': '°C', 'body_temp': '°C', 'weight': 'kg', 'bmi': ''}
ALIASES = {'resting_hr': 'resting_heart_rate', 'hr': 'heart_rate', 'spo2': 'blood_oxygen', 'sleep': 'sleep_hours'}

def health_take(data, device=''):
    if isinstance(data.get('metrics'), dict):
        data = dict(data['metrics'], device=data.get('device', device))
    with Locked('health.json', {'metrics': {}, 'history': {}}) as d:
        t = nowi()
        for k, v in data.items():
            k = ALIASES.get(k, k)
            if k == 'device':
                d['device'] = str(v)[:40]; continue
            if isinstance(v, dict):
                v = v.get('value')
            try:
                v = round(float(v), 2)
            except (TypeError, ValueError):
                continue
            d['metrics'][k] = {'value': v, 'at': t}
            d['history'].setdefault(today(), {})[k] = v
        d['at'] = t
        for day in sorted(d['history'])[:-30]:
            del d['history'][day]
    return {'ok': True}

def health_view():
    d = jload('health.json', {'metrics': {}, 'history': {}})
    if not d.get('at'):
        return {'connected': False, 'freshness': 'no_data'}
    t = nowi()
    fr = lambda age: 'live' if age < 900 else 'recent' if age < 4 * 3600 else 'old'
    ms = {}
    for k, m in d.get('metrics', {}).items():
        age = t - m.get('at', 0)
        val = m['value']
        if k == 'steps':
            val = f'{int(val):,}'
        elif isinstance(val, float) and val.is_integer():
            val = int(val)
        ms[k] = {'value': val, 'unit': UNITS.get(k, ''), 'freshness': fr(age), 'age_seconds': age}
    age = t - d['at']
    return {'connected': age < 2 * 86400, 'freshness': fr(age), 'age_seconds': age, 'device': d.get('device') or '手表',
            'metrics': ms, 'history': d.get('history', {})}

# ---------- 用量 ----------
def usage_add(inp, out):
    with Locked('usage.json', {'days': {}}) as d:
        day = d['days'].setdefault(today(), {'in': 0, 'out': 0})
        day['in'] += int(inp or 0); day['out'] += int(out or 0)
        for k in sorted(d['days'])[:-62]:
            del d['days'][k]

def usage_view():
    d = jload('usage.json', {'days': {}})['days']
    budget = int(conf().get('daily_token_budget') or 2000000)
    td = d.get(today(), {'in': 0, 'out': 0})
    tt = td['in'] + td['out']
    month = today()[:7]
    mt = sum(v['in'] + v['out'] for k, v in d.items() if k.startswith(month))
    w = lambda n: (str(round(n / 10000, 1)) + ' 万') if n >= 10000 else str(n)
    return {'ok': True, 'sections': [
        {'label': '今天', 'bars': [{'name': '今天用掉 ' + w(tt) + ' token', 'pct': min(100, round(tt * 100 / budget)),
                                   'reset': '按每天 ' + w(budget) + ' 算（设置里能改）· 零点清零'}]},
        {'label': '这个月', 'bars': [{'name': month + ' 一共 ' + w(mt) + ' token', 'pct': min(100, round(mt * 100 / (budget * 30))),
                                    'reset': '订阅走官端时，额度以 claude.ai 上显示的为准'}]}]}

# ---------- 信箱：时光胶囊 / 双人日记 ----------
# 封着的信谁都看不到正文（写信的人自己也看不到），到点一起打开——先各自诚实，再彼此对照
LET0 = {'items': [], 'next_id': 1}

def letters_view(reveal_for=None):
    t = nowi()
    out = []
    for x in jload('letters.json', LET0).get('items', []):
        locked = bool(x.get('lock_until')) and x['lock_until'] > t
        y = {k: v for k, v in x.items() if k != 'text'}
        y['locked'] = locked
        y['text'] = None if locked else x.get('text', '')
        out.append(y)
    out.sort(key=lambda x: -x['at'])
    return {'items': out}

def letter_write(who, title, text, lock_until=0):
    text = str(text or '').strip()
    if not text:
        return letters_view()
    with Locked('letters.json', LET0) as d:
        d['items'].append({'id': 'l' + str(_nid(d)), 'from': who, 'title': str(title or '').strip()[:40] or '一封信',
                           'text': text[:8000], 'at': nowi(), 'lock_until': int(lock_until or 0), 'told': not lock_until})
    if who == 'her':
        inbox_add('她写了一封信' + ('，封到 ' + day_of(lock_until) + ' 才能打开' if lock_until else '：' + str(title or '')[:30]))
    return letters_view()

def letters_due():
    """到期还没通知过的信：返回它们并标记已通知"""
    t = nowi()
    due = []
    with Locked('letters.json', LET0) as d:
        for x in d['items']:
            if x.get('lock_until') and x['lock_until'] <= t and not x.get('told'):
                x['told'] = True
                due.append(dict(x))
    return due

def letter_del(lid, who='her'):
    with Locked('letters.json', LET0) as d:
        d['items'] = [x for x in d['items'] if not (x['id'] == lid and x.get('from') == who)]
    return letters_view()

# ---------- 清单到点提醒 ----------
def _mins(hm):
    try:
        h, m = str(hm).split(':')[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 99999

def todos_due():
    """挂了钟点、到点了、今天还没提醒过、还没做完的"""
    hm = now_local().strftime('%H:%M')
    due = []
    with Locked('todos.json', TODO0) as d:
        for t in d.get('hers', []):
            done = (t.get('done_day') == today()) if t.get('fixed') else t.get('done')
            late = _mins(hm) - _mins(t.get('at') or '99:99')
            if t.get('at') and not done and 0 <= late <= 30 and t.get('reminded') != today():   # 过点半小时以上就不补了
                t['reminded'] = today()
                due.append(dict(t))
    return due
