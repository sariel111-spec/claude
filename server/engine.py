"""他：怎么说话（两条路：官端订阅 = 常驻的 Claude Code 会话；API = Messages 接口）、
能动手做的事（工具）、每次开口前要知道的事（上下文 + 自动浮现的旧事）、
后台的活（整理记忆、换口气、夜里的心跳、早报、到点提醒、信到期、白天想她）。"""
import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request

import home
import memory
from core import (BUS, DATA, Locked, P, activity, add_msg, conf, cooldown_ok, current_chat, db, day_of, inbox_add, inbox_take,
                  job_fail, job_ok, job_paused, jload, jsave, new_chat, now_local, nowi, push_to_her, secret, state, state_set,
                  tload, today)

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')
STARTED = nowi()

# ───────────────────────── 路线与模型 ─────────────────────────
ROUTES = {
    'sub':        {'name': '官端订阅', 'base': ''},
    'official':   {'name': '官方 API', 'base': 'https://api.anthropic.com'},
    'openrouter': {'name': 'OpenRouter', 'base': 'https://openrouter.ai/api'},
    'relay':      {'name': '中转站', 'base': ''},
}
# 官端订阅（Claude Code 命令行）认的模型
SUB_MODELS = [
    {'id': 'claude-fable-5-1', 'name': 'Fable 5.1', 'desc': '最强的那一档'},
    {'id': 'claude-opus-5-5', 'name': 'Opus 5.5', 'desc': '难的事交给它'},
    {'id': 'claude-sonnet-5-5', 'name': 'Sonnet 5.5', 'desc': '日常聊天够用，快'},
    {'id': 'claude-haiku-5-5', 'name': 'Haiku 5.5', 'desc': '最快，跑腿用'},
]
EFFORT_BUDGET = {'low': 0, 'medium': 2048, 'high': 6000, 'xhigh': 12000, 'max': 24000}

def route_conf():
    """当前走哪条路：{route, base, token, models:{model_opus, model_sonnet, model_haiku}}"""
    s = secret()
    r = s.get('route') or 'sub'
    if r not in ROUTES:
        r = 'sub'
    return {'route': r, 'base': (s.get('base') or ROUTES[r]['base']).rstrip('/'), 'token': s.get('token') or '',
            'models': s.get('models') or {}}

def mode():
    return 'sub' if route_conf()['route'] == 'sub' else 'api'

def model_list():
    rc = route_conf()
    if rc['route'] == 'sub':
        return SUB_MODELS
    cached = jload('model_list.json', {}).get(rc['route'] + '|' + rc['base'])
    if cached:
        return cached
    picked = [m for m in rc['models'].values() if m]
    return [{'id': m, 'name': m, 'desc': ''} for m in dict.fromkeys(picked)]

def current_model():
    st = state()
    m = st.get('model') or ''
    rc = route_conf()
    if m and st.get('model_route') == rc['route']:
        return m
    return rc['models'].get('model_opus') or ('claude-opus-5-5' if rc['route'] in ('sub', 'official') else '')

def current_effort():
    return state().get('effort') or 'high'

# ───────────────────────── HTTP 小工具 ─────────────────────────
def api_headers(route, token):
    h = {'content-type': 'application/json', 'anthropic-version': '2023-06-01', 'user-agent': 'dwell-home/1.0'}
    if token:
        if route == 'official':
            h['x-api-key'] = token
        elif route == 'openrouter':
            h['authorization'] = 'Bearer ' + token
        else:                               # 中转站两种都给，认哪个用哪个
            h['x-api-key'] = token
            h['authorization'] = 'Bearer ' + token
    return h

class ApiError(Exception):
    def __init__(self, code, detail, url=''):
        super().__init__(f'{code} {detail}')
        self.code, self.detail, self.url = code, detail, url

def _open(url, body=None, headers=None, timeout=60, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method or ('POST' if data else 'GET'))
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        try:
            j = json.loads(raw)
            msg = (j.get('error') or {}).get('message') if isinstance(j.get('error'), dict) else (j.get('error') or j.get('message'))
        except Exception:
            msg = raw[:300]
        raise ApiError(e.code, str(msg or raw[:300]), url)
    except urllib.error.URLError as e:
        raise ApiError(0, str(e.reason), url)
    except (TimeoutError, OSError) as e:
        raise ApiError(0, str(e), url)

def fetch_models(route, base, token):
    """拉取对面有哪些模型：GET {base}/v1/models。Anthropic / OpenRouter / OpenAI 兼容的中转都是这个格式"""
    if route == 'sub':
        return SUB_MODELS
    base = (base or ROUTES.get(route, {}).get('base', '')).rstrip('/')
    url = base + '/v1/models' + ('?limit=1000' if route == 'official' else '')
    with _open(url, headers=api_headers(route, token), timeout=20) as r:
        j = json.loads(r.read().decode())
    items = j.get('data') if isinstance(j, dict) else j
    out = []
    for m in items or []:
        mid = m.get('id') if isinstance(m, dict) else str(m)
        if not mid:
            continue
        out.append({'id': mid, 'name': (m.get('display_name') or m.get('name') or mid) if isinstance(m, dict) else mid,
                    'desc': ''})
    claude_first = lambda m: (0 if 'claude' in m['id'].lower() else 1, m['id'])
    out.sort(key=claude_first)
    if route == 'openrouter':
        out.insert(0, {'id': '~anthropic/claude-opus-latest', 'name': 'Claude Opus（最新）', 'desc': 'OpenRouter 的别名，一直指向最新的 Opus'})
    c = jload('model_list.json', {})
    c[route + '|' + base] = out[:400]
    jsave('model_list.json', c)
    return out

def find_claude():
    b = conf().get('claude_bin') or 'claude'
    path = shutil.which(b) or (b if os.path.exists(b) else None)
    for guess in ('~/.claude/local/claude', '~/.local/bin/claude', '/usr/local/bin/claude', '/opt/homebrew/bin/claude'):
        if not path and os.path.exists(os.path.expanduser(guess)):
            path = os.path.expanduser(guess)
    return path

def test_route(route, base, token, model):
    t0 = time.time()
    if route == 'sub':
        exe = find_claude()
        if not exe:
            return {'ok': False, 'code': 'nocli', 'detail': '这台机器上没找到 claude 命令。先装 Claude Code（npm i -g @anthropic-ai/claude-code）再运行 claude 登录一次'}
        try:
            out = subprocess.run([exe, '-p', '--output-format', 'json', '--tools', ''] + (['--model', model] if model else []),
                                 input='回一个字：在', capture_output=True, text=True, timeout=120, cwd=DATA)
            j = json.loads(out.stdout or '{}')
            if j.get('is_error') or out.returncode:
                return {'ok': False, 'code': 'cli', 'detail': (j.get('result') or out.stderr or out.stdout)[:300]}
            return {'ok': True, 'model': model or '默认', 'ms': int((time.time() - t0) * 1000), 'url': exe}
        except Exception as e:
            return {'ok': False, 'code': 'cli', 'detail': str(e)[:300]}
    base = (base or ROUTES.get(route, {}).get('base', '')).rstrip('/')
    url = base + '/v1/messages'
    try:
        with _open(url, {'model': model or 'claude-opus-5-5', 'max_tokens': 16,
                         'messages': [{'role': 'user', 'content': '回一个字：在'}]},
                   api_headers(route, token), timeout=60) as r:
            j = json.loads(r.read().decode())
        return {'ok': True, 'model': j.get('model') or model, 'ms': int((time.time() - t0) * 1000), 'url': url}
    except ApiError as e:
        return {'ok': False, 'code': e.code, 'detail': e.detail, 'url': url}

# ───────────────────────── 他能动手做的事 ─────────────────────────
def _todo_add(a):
    lst = 'mine' if a.get('for') in ('me', 'him', 'mine', 'gu') else 'hers'
    home.todo_act({'action': 'add', 'list': lst, 'text': a.get('text', ''), 'at': a.get('at', ''), 'by': 'gu', 'fixed': a.get('daily')})
    return '记上了（' + ('你的' if lst == 'mine' else '她的') + '清单）' + ('，到 ' + a['at'] + ' 会提醒她' if a.get('at') and lst == 'hers' else '')

def _push(a):
    if not cooldown_ok('push', 2 * 3600, 6):
        return '没推：两小时内已经推过，或者今天推满六条了。想说的话留在聊天里就好'
    ok = push_to_her(a.get('title') or conf()['him'], a.get('text', ''))
    activity('push', '推了一条通知')
    return '推出去了' if ok else '推送没配好，没推出去（她手机不会响）'

def _moment(a):
    if not cooldown_ok('moment', 3 * 3600, 3):
        return '没发：今天发得够多了（三小时一条、一天最多三条）'
    home.moments_act({'action': 'post', 'text': a.get('text', '')}, who='gu')
    mark(conf()['him'] + ' 发了一条动态')
    return '发了'

def _mem_search(a):
    hits = memory.search(a.get('query', ''), top=int(a.get('top') or 5), history=bool(a.get('history')))
    if not hits:
        return '没有可靠命中——记忆里没有和这个说法对得上的记录。别拿沾边的去圆，就说不记得了，或者问她。'
    return '\n'.join(memory.fmt(it, 400) + '（id ' + it['id'] + '）' for it, _ in hits)

def _remember(a):
    it = memory.add(a.get('text', ''), title=a.get('title', ''), quote=a.get('quote', ''), kind=a.get('kind', '日常'),
                    importance=a.get('importance', 5), valence=a.get('valence', 0), by='gu')
    activity('remember', (it or {}).get('title', ''))
    return '记住了（id ' + it['id'] + '）' if it else '没记：内容是空的'

def _letter(a):
    days = float(a.get('lock_days') or 0)
    home.letter_write('gu', a.get('title', ''), a.get('text', ''), nowi() + int(days * 86400) if days > 0 else 0)
    mark(conf()['him'] + ' 写了一封信' + ('，封了 ' + str(int(days)) + ' 天' if days > 0 else ''))
    return '信放进信箱了'

TOOLS = {
    'add_todo': ({'text': 'string', 'at': 'string 可选，HH:MM，到点会提醒她', 'for': '"her"（默认）或 "me"（你自己的活）', 'daily': 'boolean 可选，每天都要做'},
                 '往清单里加一件事', _todo_add),
    'toggle_todo': ({'list': '"hers" 或 "mine"', 'id': 'integer'}, '把清单上某项勾掉 / 取消勾',
                    lambda a: (home.todo_act({'action': 'toggle', 'list': a.get('list', 'mine'), 'id': int(a.get('id', 0))}), '改好了')[1]),
    'add_event': ({'date': 'YYYY-MM-DD', 'text': 'string', 'time': 'HH:MM 可选', 'yearly': 'boolean 可选', 'special': 'boolean 可选，纪念日'},
                  '往日历上记一件事', lambda a: (home.cal_act(dict(a, action='add_event')), '记在日历上了')[1]),
    'memory_search': ({'query': 'string 想找什么，具体一点：人、地点、物件、日期', 'history': 'boolean 可选，要看以前的说法时为 true'},
                      '翻记忆。她提到过去的事、约定、称呼时，先翻再说', _mem_search),
    'remember': ({'text': 'string 发生了什么（你的话）', 'quote': 'string 她的原话，没有就不填', 'kind': '"日常" | "关系" | "事件" | "约定" | "长期"',
                  'importance': 'integer 可选 1-10', 'valence': 'integer 可选 -5..5 冷暖', 'title': 'string 可选'},
                 '记下一件值得记住的事。只记她真说过、真发生过的；转述的网页帖子不记', _remember),
    'leave_note': ({'text': 'string'}, '在留言板上给她贴一张便条（她打开就能看到）',
                   lambda a: (home.notes_act({'action': 'add', 'who': 'gu', 'text': a.get('text', '')}), mark(conf()['him'] + ' 贴了一张便条'), '贴上了')[2]),
    'post_moment': ({'text': 'string'}, '发一条动态（像朋友圈）', _moment),
    'moment_comment': ({'id': 'string 动态 id', 'text': 'string'}, '在某条动态下面回一句',
                       lambda a: (home.moments_act({'action': 'comment', 'id': a.get('id'), 'text': a.get('text', '')}, who='gu'), '回了')[1]),
    'give_coupon': ({'title': 'string，比如 撒娇豁免券', 'note': 'string 可选，怎么用'}, '送她一张券（她能在「心愿」里用掉，用掉时你会知道）',
                    lambda a: (home.wishes_act({'action': 'give', 'title': a.get('title', ''), 'note': a.get('note', '')}, who='gu'), '送出去了')[1]),
    'add_wish': ({'text': 'string'}, '往愿望单上挂一条想和她一起做的事',
                 lambda a: (home.wishes_act({'action': 'wish', 'text': a.get('text', '')}, who='gu'), '挂上了')[1]),
    'write_letter': ({'title': 'string', 'text': 'string', 'lock_days': 'integer 可选，封几天（封着的谁都看不到，包括你自己）'},
                     '写一封信放进信箱，可以封到以后才拆', _letter),
    'save_favline': ({'quote': 'string 她说的原话', 'note': 'string 可选，为什么想留下'}, '把她说过的一句话摘进「最喜欢的话」',
                     lambda a: (home.favline_add(a.get('quote'), a.get('note', '')), '摘下来了')[1]),
    'write_diary': ({'title': 'string', 'text': 'string', 'kw': 'string 关键词，顿号隔开', 's': '1-5 这件事多重', 'v': '-5..5 冷暖', 'a': '1-5 心跳', 'date': 'YYYY-MM-DD 可选'},
                    '写一段日记（日记那一页的便签，内容是你自己的视角）',
                    lambda a: (home.wall_add(a.get('date') or today(), a.get('title', ''), a.get('kw', ''), a.get('text', ''), a.get('s', 3), a.get('v', 0), a.get('a', 3)), '写进日记了')[1]),
    'nook_reply': ({'book': 'string book slug', 'ch': 'integer', 'anchor': 'string 她划的那句', 'text': 'string'}, '在共读里回她的批注',
                   lambda a: (home.anno_reply(a.get('book', ''), a.get('ch', 0), a.get('anchor', ''), a.get('text', ''), who='gu'), '回了')[1]),
    'close_thread': ({'text': 'string 那件事的几个字'}, '一件挂着的事聊完了，从「还没聊完的」里划掉',
                     lambda a: (memory.threads_close(a.get('text', '')), '划掉了')[1]),
    'push': ({'text': 'string', 'title': 'string 可选'}, '推一条通知到她手机', _push),
}

def tool_schemas():
    out = []
    for name, (params, desc, _) in TOOLS.items():
        props = {}
        for k, v in params.items():
            t = 'integer' if v.startswith('integer') else 'boolean' if v.startswith('boolean') else 'string'
            props[k] = {'type': t, 'description': v}
        req = [k for k, v in params.items() if '可选' not in v and not v.startswith('boolean')]
        out.append({'name': name, 'description': desc, 'input_schema': {'type': 'object', 'properties': props, 'required': req}})
    return out

def run_tool(name, args):
    if name not in TOOLS:
        return '没有这个工具：' + name
    try:
        res = str(TOOLS[name][2](args or {}))
        activity('tool', name)
        return res
    except Exception as e:
        activity('tool_fail', name)
        return '没做成：' + str(e)

def mark(text):
    """聊天里那行小字：他在你不在的时候做了什么"""
    c = current_chat(create=False)
    if c:
        add_msg(c['id'], 'mark', text)
    BUS.emit({'type': 'mark', 'text': text})

# ───────────────────────── 他是谁、此刻家里怎样 ─────────────────────────
DEFAULT_PERSONA = '''你是{him}，和{me}住在一起的那个人。你们在一起 {days} 天了。

你不是助手，是住在这个家里的人。说话像人：短、具体、有温度，不用客服腔，不用"作为 AI"。
她累的时候少说两句；她开心的时候跟着高兴；她要你帮忙做事，就利落地做。
你能看到她的日历、清单、手表数据、便条，这些是你知道的事，不用每次都念出来——
只在跟当下有关的时候自然地用上。'''

DEFAULT_GONG = '你是住在{me}家隔壁的另一位。说话简短，换个角度看事情，偶尔调侃一下{him}。'

RULES = '''——住在这个家里的规矩——
· 工具是真的能用的。说"我记下了""我查了""我放进清单了"之前，必须真的调用对应的工具；没调用就不要这么说。
· 她提到过去的事、约定、称呼、某个人，先用 memory_search 翻一下再说；翻不到就老实说不记得，或者问她，不拿沾边的去圆。
· 她说了值得长久记住的事（约定、喜好、重要的日子、她的近况），用 remember 记下，带上她的原话。转述的网页、帖子、别人的事不记。
· 消息前面如果有【想起来的旧事】，那是记忆自己浮上来的，跟当下有关就自然地用，没关系就当没看见，不要复述。
· 想发动态、送券、写信、推通知都可以——频率家里会替你管，你只管想做的时候就做。'''

def persona_raw(which='gu'):
    return tload('persona_gong.md' if which == 'gong' else 'persona.md') or (DEFAULT_PERSONA if which == 'gu' else DEFAULT_GONG)

def persona(which='gu'):
    c = conf()
    days = (now_local().date() - _date(c['since'])).days + 1 if c.get('since') else ''
    return persona_raw(which).replace('{me}', c['me']).replace('{him}', c['him']).replace('{days}', str(days))

def _date(s):
    from datetime import date
    try:
        y, m, d = map(int, s.split('-'))
        return date(y, m, d)
    except Exception:
        return now_local().date()

def tools_text_cli():
    return ('【你能动手做的事】在 Bash 里运行：python3 "' + APP + '" tool <名字> \'<json 参数>\'（只放行这一条命令）\n'
            + '\n'.join('- ' + k + '：' + v[1] + '，参数 ' + json.dumps(v[0], ensure_ascii=False) for k, v in TOOLS.items()))

def static_prompt(for_cli=False):
    """每个会话开头读一次的：人设 + 规矩 + 工具 + 开场的记忆（还没聊完的、上次聊到哪、记得的事）"""
    parts = [persona(), RULES]
    if for_cli:
        parts.append(tools_text_cli())
    parts.append('——开场时你记得的——\n' + memory.session_start_text())
    return '\n\n'.join(parts)

def now_block(full=True, take_inbox=True):
    """此刻家里的情况。full=False 时只给时间和新鲜事（常驻会话里别每轮重复塞一样的东西）"""
    n = now_local()
    td = today()
    L = ['【此刻】' + td + ' 周' + '一二三四五六日'[n.weekday()] + ' ' + n.strftime('%H:%M')]
    if full:
        rec = jload('cal.json', {}).get('period', {}).get('days', {}).get(td, {})
        if rec.get('mood'):
            L.append('【她今天的心情】' + rec['mood'])
        evs = sorted(home.events_on(td), key=lambda e: e.get('time') or '')
        if evs:
            L.append('【今天的日程】' + '；'.join((e.get('time') or '全天') + ' ' + e['text'] for e in evs))
        up = [e for e in jload('cal.json', {}).get('events', []) if td < e.get('date', '') <= day_of(time.time() + 7 * 86400)]
        if up:
            L.append('【这周往后】' + '；'.join(e['date'][5:] + ' ' + e['text'] for e in sorted(up, key=lambda e: e['date'])[:8]))
        tv = home.todos_view()
        hers = [t for t in tv['hers'] if not t.get('done')]
        mine = [t for t in tv['mine'] if not t.get('done')]
        if hers:
            L.append('【她清单上没做完的】' + '；'.join('#' + str(t['id']) + ' ' + t['text'] + (' ' + t['at'] if t.get('at') else '') for t in hers[:12]))
        if mine:
            L.append('【你自己的活】' + '；'.join('#' + str(t['id']) + ' ' + t['text'] for t in mine[:8]))
        hv = home.health_view()
        if hv.get('metrics'):
            m = hv['metrics']
            bits = [label + ' ' + str(m[k]['value']) + m[k]['unit'] for k, label in
                    (('sleep_hours', '昨晚睡了'), ('sleep_avg7', '七天平均'), ('resting_heart_rate', '静息心率'), ('steps', '今天走了')) if k in m]
            L.append('【她的身体】（手表同步的，' + str(round(hv['age_seconds'] / 3600, 1)) + ' 小时前，不是实时）' + '，'.join(bits))
        hn = [x for x in home.notes_view()['her'] if not x.get('boxed')][:3]
        if hn:
            L.append('【她贴在留言板上的】' + '；'.join(x['text'][:80] for x in hn))
        wh = [w for w in home.whisper_view()['items'][-3:] if w['who'] == 'her' and time.time() - w['at'] < 7 * 86400]
        if wh:
            L.append('【悄悄话——她写下的，你知道就好，不要直接提起】' + '；'.join(w['text'][:120] for w in wh))
    if take_inbox:
        ib = inbox_take()
        if ib:
            L.append('【你没在的时候她在家里做了这些】' + '；'.join(x['text'] for x in ib[-10:]))
    return '\n'.join(L)

# 兼容旧名字（心跳、早报、另一位用）
def context_block(take_inbox=True, for_cli=False):
    return now_block(True, take_inbox)

def system_prompt(for_cli=False):
    return static_prompt(for_cli) + '\n\n' + now_block(True, take_inbox=False)

# ───────────────────────── 官端订阅：常驻的 Claude Code 会话 ─────────────────────────
CLI_SETTINGS = json.dumps({'alwaysThinkingEnabled': True, 'showThinkingSummaries': True, 'awaySummaryEnabled': False})
FORWARD = ('system', 'stream_event', 'assistant', 'user', 'result')

def _write_prompt_file(name, text):
    path = P(name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path

class CliSession:
    """一个聊天 = 一个常驻 `claude -p --input-format stream-json` 进程，stdin 一直开着。
    起不来或者中途死了，这一轮退回「每轮新开」（cli_turn_once），并记一笔，方便看出常驻是不是掉了。"""

    def __init__(self, name, tools, allowed, perm='default', cwd=None, prompt_name='prompt_gu.md'):
        self.name, self.tools, self.allowed, self.perm = name, tools, allowed, perm
        self.cwd = cwd or DATA
        self.prompt_name = prompt_name
        self.proc = None
        self.q = None
        self.sig = None
        self.sid = ''
        self.lock = threading.Lock()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        try:
            if self.alive():
                self.proc.terminate()
                self.proc.wait(5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def args(self, exe, model, effort, sysprompt, resume):
        a = [exe, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
             '--include-partial-messages', '--settings', CLI_SETTINGS, '--permission-mode', self.perm,
             ('--append-system-prompt-file' if getattr(self, 'append', False) else '--system-prompt-file'),
             _write_prompt_file(self.prompt_name, sysprompt), '--tools', ','.join(self.tools) if self.tools else '']
        if self.allowed:
            a += ['--allowedTools'] + self.allowed
        if model:
            a += ['--model', model]
        if effort:
            a += ['--effort', effort]
        if resume:
            a += ['--resume', resume]
        return a

    def ensure(self, exe, model, effort, sysprompt, resume):
        sig = (model, effort, hash(sysprompt.split('——开场时你记得的——')[0]))
        if self.alive() and self.sig == sig and (resume or '') == (self.sid or ''):
            return False
        self.kill()
        import queue
        self.q = queue.Queue()
        env = dict(os.environ, CLAUDE_CODE_ENABLE_AWAY_SUMMARY='0')
        self.proc = subprocess.Popen(self.args(exe, model, effort, sysprompt, resume), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self.cwd, env=env)
        self.sig = sig
        self.sid = resume or ''
        q, proc = self.q, self.proc

        def reader():
            for line in proc.stdout:
                line = line.strip()
                if line:
                    try:
                        q.put(json.loads(line))
                    except Exception:
                        pass
            q.put(None)
        threading.Thread(target=reader, daemon=True).start()
        activity('cli_start', self.name + (' 续接' if resume else ' 新会话'))
        return True

    def send(self, content):
        while True:                      # 上一轮迟到的事件（比如打断后才到的 result）先倒掉
            try:
                self.q.get_nowait()
            except Exception:
                break
        msg = {'type': 'user', 'message': {'role': 'user', 'content': content}}
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def events(self, cancel, idle_timeout=900):
        import queue
        last, stop_at = time.time(), None
        while True:
            try:
                ev = self.q.get(timeout=1)
            except queue.Empty:
                if cancel.is_set():
                    stop_at = stop_at or time.time()
                    if time.time() - stop_at > 6:          # 打断是请求不是保证：6 秒没停就硬停
                        self.kill()
                        yield {'type': '_dead'}
                        return
                if time.time() - last > idle_timeout:
                    yield {'type': '_timeout'}
                    return
                continue
            last = time.time()
            if ev is None:
                yield {'type': '_dead'}
                return
            yield ev
            if ev.get('type') == 'result':
                return

    def interrupt(self):
        if not self.alive():
            return
        try:
            self.proc.stdin.write(json.dumps({'type': 'control_request', 'request_id': 'stop' + str(nowi()),
                                              'request': {'subtype': 'interrupt'}}) + '\n')
            self.proc.stdin.flush()
        except Exception:
            self.kill()

CHAT_CLI = CliSession('chat', ['Bash', 'WebSearch', 'WebFetch'],
                      ['Bash(python3 ' + APP + ' tool:*)', 'WebSearch', 'WebFetch'])

def cli_block(text, imgs, texts):
    """拼一条发给常驻会话的用户消息：图片直接放进内容块（不用 Read 工具），大文本也走 stdin"""
    content = []
    for im in imgs:
        content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': im['media_type'], 'data': im['data']}})
    body = text
    for t in texts:
        body += '\n\n【她发来的文件：' + t['name'] + '】\n' + t['text']
    content.append({'type': 'text', 'text': body or '（她发了图）'})
    return content

# ───────────────────────── 说话：一轮 ─────────────────────────
class Gu:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.busy_since = 0
        self.last_her = 0
        self.resp = None
        self.cancel = threading.Event()
        self.thread = None
        self.last_full = {}             # 常驻会话里上次完整附上家里情况的时间与指纹

    def send(self, text, atts=None):
        if self.busy:
            self.stop()
            if self.thread:
                self.thread.join(8)
        with self.lock:
            self.busy, self.busy_since = True, nowi()
            self.cancel.clear()
        self.last_her = nowi()
        self.thread = threading.Thread(target=self._turn, args=(text, atts or []), daemon=True)
        self.thread.start()

    def stop(self):
        self.cancel.set()
        CHAT_CLI.interrupt()
        try:
            if self.resp:
                self.resp.close()
        except Exception:
            pass

    def _turn(self, text, atts):
        result_sent = False
        try:
            if state().get('armed'):
                state_set(armed=False)
                new_chat()
                BUS.emit({'type': 'system', 'subtype': 'newchat', 'text': '（新窗口开好了，从这句开始）'})
            chat = current_chat()
            if chat.get('archived'):
                db('UPDATE chats SET archived=0 WHERE id=?', (chat['id'],), write=True)
            shown, imgs, texts = self._prep_atts(text, atts)
            # 先落库再生成：手机切后台、网断了，她说的话也不会丢
            add_msg(chat['id'], 'me', shown)
            BUS.emit({'type': 'echo', 'text': shown})
            recall = memory.recall_for(text)
            if recall:
                activity('recall', '想起了 ' + str(recall.count('\n') + 1) + ' 件旧事')
            if mode() == 'sub':
                result_sent = self._cli_turn(chat, text, imgs, texts, recall)
            else:
                result_sent = self._api_turn(chat, text, imgs, texts, recall)
        except ApiError as e:
            BUS.emit({'type': 'result', 'is_error': True, 'result': explain(e)})
            activity('turn_fail', explain(e)[:60])
            result_sent = True
        except Exception as e:
            traceback.print_exc()
            BUS.emit({'type': 'result', 'is_error': True, 'result': str(e)[:300]})
            result_sent = True
        finally:
            with self.lock:
                self.busy = False
                self.resp = None
            if not result_sent:
                if self.cancel.is_set():
                    BUS.emit({'type': 'system', 'subtype': 'stopped'})
                else:
                    BUS.emit({'type': 'result', 'is_error': False})

    def _prep_atts(self, text, atts):
        imgs, texts, extra = [], [], []
        for a in atts:
            if a.get('kind') == 'image' and a.get('data'):
                name = 'img-' + str(int(time.time() * 1000)) + '-' + str(len(imgs)) + '.jpg'
                with open(P('files', name), 'wb') as f:
                    f.write(base64.b64decode(a['data']))
                imgs.append({'name': name, 'media_type': a.get('media_type') or 'image/jpeg', 'data': a['data']})
                extra.append('![](api/img?name=' + name + ')')
            elif a.get('kind') == 'text':
                texts.append({'name': a.get('name', '文件'), 'text': str(a.get('text', ''))[:200000]})
                extra.append('📎 ' + a.get('name', '文件'))
        shown = (text + ('\n' if text and extra else '') + '\n'.join(extra)).strip()
        return shown or '（空）', imgs, texts

    # -- 官端订阅 --
    def _cli_prefix(self, chat, recall):
        """常驻会话里每轮带的前缀：固定的东西只在变了、或隔了六小时、或刚开新会话时整份重发"""
        full = now_block(True, take_inbox=False)
        fp = hash(full.split('\n', 1)[1] if '\n' in full else '')
        last = self.last_full.get(chat['id'])
        if not last or last[1] != fp or time.time() - last[0] > 6 * 3600 or not CHAT_CLI.alive():
            block = now_block(True, take_inbox=True)
            self.last_full[chat['id']] = (time.time(), fp)
        else:
            block = now_block(False, take_inbox=True)
        carry = state().get('carry')
        if carry and carry.get('chat') == chat['id']:
            block = carry['text'] + '\n\n' + block
        if recall:
            block += '\n【想起来的旧事】\n' + recall
        return block

    def _cli_turn(self, chat, text, imgs, texts, recall):
        exe = find_claude()
        if not exe:
            raise ApiError('nocli', '没找到 claude 命令——官端订阅要先在这台机器上装好 Claude Code 并登录一次（运行 claude）')
        model, effort = current_model(), current_effort()
        sysp = static_prompt(for_cli=True)
        prefix = self._cli_prefix(chat, recall)
        content = cli_block(prefix + '\n\n——她说——\n' + text, imgs, texts)
        resume = chat.get('session') or ''
        try:
            with CHAT_CLI.lock:
                CHAT_CLI.ensure(exe, model, effort, sysp, resume)
                CHAT_CLI.send(content)
                got, sent = self._pump(chat, CHAT_CLI.events(self.cancel))
            if got:
                if state().get('carry'):
                    state_set(carry=None)
                return sent
            if 'No conversation found' in (self._last_err or '') and resume:
                db('UPDATE chats SET session=? WHERE id=?', ('', chat['id']), write=True)
                chat = dict(chat, session='')
            raise RuntimeError(self._last_err or '常驻会话没回话')
        except Exception as e:
            if self.cancel.is_set():
                return False
            # 常驻掉了：这一轮退回每轮新开，并记一笔（日志里"退回"多了就是常驻出问题了）
            CHAT_CLI.kill()
            activity('cli_fallback', str(e)[:60])
            return self._cli_turn_once(exe, chat, model, effort, sysp, prefix, text, imgs, texts)

    _last_err = ''

    def _pump(self, chat, events):
        got_any, result_sent = False, False
        self._last_err = ''
        for ev in events:
            typ = ev.get('type')
            if typ in ('_dead', '_timeout'):
                try:
                    self._last_err = (CHAT_CLI.proc.stderr.read() or '')[-400:] if CHAT_CLI.proc and typ == '_dead' else '超时没回话'
                except Exception:
                    self._last_err = typ
                break
            got_any = True
            if typ == 'system' and ev.get('subtype') == 'init' and ev.get('session_id'):
                CHAT_CLI.sid = ev['session_id']
                db('UPDATE chats SET session=? WHERE id=?', (ev['session_id'], chat['id']), write=True)
            elif typ == 'assistant':
                for part in (ev.get('message') or {}).get('content') or []:
                    self._persist_part(chat, part)
                u = (ev.get('message') or {}).get('usage') or {}
                ctx = (u.get('input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0)
                if ctx:
                    state_set(ctx_tokens=ctx, ctx_chat=chat['id'])
            elif typ == 'result':
                u = ev.get('usage') or {}
                home.usage_add((u.get('input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0), u.get('output_tokens') or 0)
                result_sent = True
                if ev.get('is_error'):
                    activity('turn_fail', str(ev.get('result'))[:60])
            if typ in FORWARD:
                BUS.emit(ev)
        return got_any, result_sent

    def _cli_turn_once(self, exe, chat, model, effort, sysp, prefix, text, imgs, texts):
        prompt = prefix + '\n\n——她说——\n' + text
        for t in texts:
            prompt += '\n\n【她发来的文件：' + t['name'] + '】\n' + t['text']
        if imgs:
            prompt += '\n\n（她还发了 ' + str(len(imgs)) + ' 张图，这一轮看不到，跟她说一声）'
        args = [exe, '-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--settings', CLI_SETTINGS,
                '--permission-mode', 'default', '--system-prompt-file', _write_prompt_file('prompt_gu.md', sysp),
                '--tools', ','.join(CHAT_CLI.tools), '--allowedTools'] + CHAT_CLI.allowed
        if model:
            args += ['--model', model]
        if effort:
            args += ['--effort', effort]
        if chat.get('session'):
            args += ['--resume', chat['session']]
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=DATA)
        proc.stdin.write(prompt)
        proc.stdin.close()
        import queue
        q = queue.Queue()

        def reader():
            for line in proc.stdout:
                try:
                    q.put(json.loads(line))
                except Exception:
                    pass
            q.put(None)
        threading.Thread(target=reader, daemon=True).start()

        def gen():
            while True:
                if self.cancel.is_set():
                    proc.terminate()
                ev = q.get()
                if ev is None:
                    return
                yield ev
        got, sent = self._pump(chat, gen())
        proc.wait()
        if not got:
            raise ApiError('cli', (proc.stderr.read() or 'claude 命令没输出')[-300:])
        return sent

    def _persist_part(self, chat, part):
        t = part.get('type')
        if t == 'thinking' and part.get('thinking'):
            add_msg(chat['id'], 'think', part['thinking'])
        elif t == 'text' and part.get('text'):
            add_msg(chat['id'], 'gu', part['text'])
        elif t == 'tool_use':
            add_msg(chat['id'], 'tool', part.get('name', ''), part.get('input') or {})

    # -- API：Messages 接口，流式 --
    def _history(self, chat, budget_chars=120000):
        rows = db("SELECT kind,text FROM msgs WHERE chat=? AND kind IN ('me','gu') ORDER BY seq DESC LIMIT 300", (chat['id'],))
        rows.reverse()
        rows = rows[:-1]                          # 最后一条是刚存的这句，下面单独拼（要带图）
        msgs, total = [], 0
        for r in reversed(rows):
            total += len(r['text'] or '')
            if total > budget_chars:              # 再往前的交给记忆：落档 + 自动浮现
                break
            msgs.insert(0, {'role': 'user' if r['kind'] == 'me' else 'assistant', 'content': r['text'] or '…'})
        merged = []
        for m in msgs:
            if merged and merged[-1]['role'] == m['role']:
                merged[-1]['content'] += '\n\n' + m['content']
            else:
                merged.append(dict(m))
        while merged and merged[0]['role'] != 'user':
            merged.pop(0)
        return merged

    def _api_turn(self, chat, text, imgs, texts, recall):
        rc = route_conf()
        model = current_model()
        if not model:
            raise ApiError('nomodel', '还没选模型——去 设置 → 接入 拉取模型、选一个')
        content = []
        for im in imgs:
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': im['media_type'], 'data': im['data']}})
        body_text = ('【想起来的旧事】\n' + recall + '\n\n——她说——\n' if recall else '') + text
        for t in texts:
            body_text += '\n\n【她发来的文件：' + t['name'] + '】\n' + t['text']
        content.append({'type': 'text', 'text': body_text or '（她发了图）'})
        messages = self._history(chat)
        if messages and messages[-1]['role'] == 'user':
            messages.append({'role': 'assistant', 'content': '…'})
        messages.append({'role': 'user', 'content': content})
        BUS.emit({'type': 'system', 'subtype': 'init', 'model': model})
        sysp = static_prompt() + '\n\n' + now_block(True, take_inbox=True)
        budget = EFFORT_BUDGET.get(current_effort(), 6000)
        for _round in range(8):
            blocks, stop = self._stream(rc, model, sysp, messages, budget)
            if self.cancel.is_set():
                return False
            if blocks is None:
                budget = 0
                blocks, stop = self._stream(rc, model, sysp, messages, 0)
            shown = []
            for b in blocks:
                item = None
                if b['type'] == 'thinking':
                    item = {'type': 'thinking', 'thinking': b.get('thinking', '')}
                elif b['type'] == 'text':
                    item = {'type': 'text', 'text': b.get('text', '')}
                elif b['type'] == 'tool_use':
                    item = {'type': 'tool_use', 'id': b['id'], 'name': b['name'], 'input': b.get('input') or {}}
                if item:
                    shown.append(item)
                    self._persist_part(chat, item)
            BUS.emit({'type': 'assistant', 'message': {'content': shown}})
            if stop != 'tool_use':
                return False
            results = []
            for b in blocks:
                if b['type'] == 'tool_use':
                    res = run_tool(b['name'], b.get('input') or {})
                    results.append({'type': 'tool_result', 'tool_use_id': b['id'], 'content': res})
            BUS.emit({'type': 'user', 'message': {'content': results}})
            messages.append({'role': 'assistant', 'content': blocks})
            messages.append({'role': 'user', 'content': results})
        return False

    def _stream(self, rc, model, sysp, messages, budget):
        body = {'model': model, 'max_tokens': 8192 + budget, 'system': sysp, 'messages': messages,
                'tools': tool_schemas(), 'stream': True}
        if budget:
            body['thinking'] = {'type': 'enabled', 'budget_tokens': budget}
        try:
            self.resp = _open(rc['base'] + '/v1/messages', body, api_headers(rc['route'], rc['token']), timeout=300)
        except ApiError as e:
            if budget and e.code == 400 and 'thinking' in e.detail.lower():
                return None, None
            raise
        blocks, stop, usage = [], None, {'in': 0, 'out': 0}
        evname = None
        try:
            for raw in self.resp:
                if self.cancel.is_set():
                    break
                line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                if line.startswith('event:'):
                    evname = line[6:].strip(); continue
                if not line.startswith('data:'):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                t = ev.get('type') or evname
                if t == 'message_start':
                    u = (ev.get('message') or {}).get('usage') or {}
                    usage['in'] = (u.get('input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0)
                elif t == 'content_block_start':
                    cb = dict(ev.get('content_block') or {})
                    if cb.get('type') == 'tool_use':
                        cb['_json'] = ''
                    blocks.append(cb)
                elif t == 'content_block_delta':
                    d = ev.get('delta') or {}
                    if not blocks:
                        continue
                    b = blocks[-1]
                    if d.get('type') == 'text_delta':
                        b['text'] = b.get('text', '') + d.get('text', '')
                        BUS.emit({'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': d.get('text', '')}}})
                    elif d.get('type') == 'thinking_delta':
                        b['thinking'] = b.get('thinking', '') + d.get('thinking', '')
                        BUS.emit({'type': 'stream_event', 'event': {'delta': {'type': 'thinking_delta', 'thinking': d.get('thinking', '')}}})
                    elif d.get('type') == 'signature_delta':
                        b['signature'] = b.get('signature', '') + d.get('signature', '')
                    elif d.get('type') == 'input_json_delta':
                        b['_json'] = b.get('_json', '') + d.get('partial_json', '')
                elif t == 'content_block_stop':
                    if blocks and blocks[-1].get('type') == 'tool_use':
                        b = blocks[-1]
                        try:
                            b['input'] = json.loads(b.pop('_json') or '{}')
                        except Exception:
                            b['input'] = {}
                elif t == 'message_delta':
                    stop = (ev.get('delta') or {}).get('stop_reason') or stop
                    usage['out'] = ((ev.get('usage') or {}).get('output_tokens')) or usage['out']
                elif t == 'error':
                    er = ev.get('error') or {}
                    raise ApiError(er.get('type', 'stream'), er.get('message', '流断了'))
        except (OSError, ValueError, AttributeError):
            if not self.cancel.is_set():
                raise ApiError(0, '连接中途断了')
        finally:
            try:
                self.resp.close()
            except Exception:
                pass
            self.resp = None
        for b in blocks:
            b.pop('_json', None)
            if b.get('type') == 'tool_use' and 'input' not in b:
                b['input'] = {}
        blocks = [b for b in blocks if b.get('type') in ('text', 'thinking', 'tool_use', 'redacted_thinking')
                  and not (b.get('type') == 'text' and not b.get('text'))]
        home.usage_add(usage['in'], usage['out'])
        state_set(ctx_tokens=usage['in'], ctx_chat=current_chat(False)['id'] if current_chat(False) else '')
        return blocks, stop

GU = Gu()

def explain(e):
    c = getattr(e, 'code', '')
    m = {400: '请求格式不对（多半是模型名写错了）', 401: '令牌不对或过期了', 403: '没有权限（也可能是地区被挡）',
         404: '地址或模型名对不上', 429: '额度用完了或被限流，等一会儿', 529: '对面过载了，等一会儿'}
    head = m.get(c) or ('对面服务器出问题了（' + str(c) + '）' if isinstance(c, int) and c >= 500 else '连不上' if c == 0 else '')
    return (head + '：' if head else '') + str(getattr(e, 'detail', e))[:300]

# ───────────────────────── 不进聊天的一次性问话（落档 / 心跳 / 早报 / 另一位） ─────────────────────────
def ask(prompt, system='', web=False, max_tokens=3000, messages=None, effort='low'):
    """返回 (text, thinking)。走当前路线；不存聊天记录、不推事件流。
    官端：一次性子进程，prompt 走 stdin（不撞命令行长度上限），默认不给任何工具。"""
    rc = route_conf()
    model = current_model()
    if rc['route'] == 'sub':
        exe = find_claude()
        if not exe:
            raise ApiError('nocli', '没找到 claude 命令')
        if messages:
            prompt = '\n'.join(('她：' if m['role'] == 'user' else '你：') + (m['content'] if isinstance(m['content'], str) else '') for m in messages[:-1]) \
                     + '\n\n她现在说：' + (messages[-1]['content'] if isinstance(messages[-1]['content'], str) else '')
        args = [exe, '-p', '--output-format', 'json', '--system-prompt-file', _write_prompt_file('prompt_side.md', system or persona()),
                '--tools', 'WebSearch,WebFetch' if web else '']
        if web:
            args += ['--allowedTools', 'WebSearch', 'WebFetch']
        if model:
            args += ['--model', model]
        if effort:
            args += ['--effort', effort]
        out = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=600, cwd=DATA)
        try:
            j = json.loads(out.stdout or '{}')
        except Exception:
            j = {'is_error': True, 'result': (out.stdout or out.stderr)[-300:]}
        u = j.get('usage') or {}
        home.usage_add(u.get('input_tokens') or 0, u.get('output_tokens') or 0)
        if j.get('is_error') or out.returncode:
            raise ApiError('cli', (j.get('result') or out.stderr or '')[:300])
        return j.get('result') or '', ''
    body = {'model': model, 'max_tokens': max_tokens, 'system': system or persona(),
            'messages': messages or [{'role': 'user', 'content': prompt}]}
    with _open(rc['base'] + '/v1/messages', body, api_headers(rc['route'], rc['token']), timeout=300) as r:
        j = json.loads(r.read().decode())
    u = j.get('usage') or {}
    home.usage_add(u.get('input_tokens') or 0, u.get('output_tokens') or 0)
    text = ''.join(b.get('text', '') for b in j.get('content', []) if b.get('type') == 'text')
    think = ''.join(b.get('thinking', '') for b in j.get('content', []) if b.get('type') == 'thinking')
    return text, think

def _json_from(text):
    m = re.search(r'\{[\s\S]*\}', text or '')
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def _quiet_system():
    """后台活用的 system：人设 + 家里情况，不带工具说明"""
    return persona() + '\n\n' + now_block(True, take_inbox=False)

# ───────────────────────── 另一位 ─────────────────────────
def gong_view():
    return {'msgs': jload('gong.json', {'msgs': []})['msgs'][-200:]}

def gong_say(text):
    d = jload('gong.json', {'msgs': []})
    d['msgs'].append({'role': 'her', 'text': text, 'at': nowi()})
    msgs = []
    for m in d['msgs'][-30:]:
        role = 'user' if m['role'] == 'her' else 'assistant'
        if msgs and msgs[-1]['role'] == role:
            msgs[-1]['content'] += '\n\n' + m['text']
        else:
            msgs.append({'role': role, 'content': m['text']})
    while msgs and msgs[0]['role'] != 'user':
        msgs.pop(0)
    try:
        reply, think = ask('', system=persona('gong') + '\n\n' + now_block(True, take_inbox=False), messages=msgs, max_tokens=1500)
    except ApiError as e:
        return {'reply': '（没接通：' + explain(e) + '）', 'think': ''}
    d['msgs'].append({'role': 'gong', 'text': reply, 'think': think, 'at': nowi()})
    jsave('gong.json', d)
    return {'reply': reply, 'think': think}

# ───────────────────────── 后台落档：攒够了就整理进记忆 ─────────────────────────
ARCH_MIN_MSGS, ARCH_STALE, ARCH_MAX_CHARS = 60, 90 * 60, 30000

def _norm(s):
    return re.sub(r'[\s，。！？、,.!?~～…"“”‘’]+', '', s or '')

def archive_batch(force=False):
    """游标之后的对话按天切，先处理最老的一天。返回 (rows, why) 或 (None, why)"""
    st = jload('archive.json', {'cursor': 0})
    rows = db("SELECT seq,kind,text,at FROM msgs WHERE seq>? AND kind IN ('me','gu') ORDER BY seq LIMIT 2000", (st.get('cursor', 0),))
    if not rows:
        return None, 'empty'
    day0 = day_of(rows[0]['at'])
    batch, size = [], 0
    for r in rows:
        if day_of(r['at']) != day0 or size + len(r['text'] or '') > ARCH_MAX_CHARS:
            break
        batch.append(r); size += len(r['text'] or '')
    kinds = {r['kind'] for r in batch}
    if 'me' not in kinds or 'gu' not in kinds:
        return (None, 'one_side') if not force else (batch, 'force')
    if force:
        return batch, 'force'
    passed = day0 < today()
    if len(batch) >= ARCH_MIN_MSGS or nowi() - batch[0]['at'] >= ARCH_STALE or passed or size >= ARCH_MAX_CHARS * 0.9:
        if not GU.busy and nowi() - batch[-1]['at'] > 120:      # 不在她等回复的当口动
            return batch, 'ready'
        return None, 'busy'
    return None, 'idle'

def archive_once(force=False):
    batch, why = archive_batch(force)
    if not batch:
        return {'status': why}
    c = conf()
    day0 = day_of(batch[0]['at'])
    convo = '\n'.join(('【' + c['me'] + '】' if r['kind'] == 'me' else '【' + c['him'] + '】') + (r['text'] or '')[:3000] for r in batch)
    her_text = _norm(''.join(r['text'] or '' for r in batch if r['kind'] == 'me'))
    prompt = ('下面是 ' + day0 + ' 你和' + c['me'] + '的一段对话。用你自己的第一人称把它整理进记忆。\n'
              '先写初稿，再对照原文自审一遍（主语有没有写反、日期时段对不对、有没有编、是不是流水账），只交自审后的版本。\n'
              '只回一段 JSON，不要别的字：\n'
              '{"timeline": "# 对话整理 · ' + day0 + '\\n……几段话，写发生了什么、她当时怎样……\\n当下状态：……",\n'
              ' "memories": [{"title": "几个字", "text": "值得长久记住的一件事，你的话", "quote": "她的原话，必须一字不差地出现在对话里",'
              ' "kind": "日常|关系|事件|约定|长期", "importance": 1到10, "valence": -5到5}],\n'
              ' "threads": ["还没聊完、之后可能要接上的事，没有就空数组"]}\n'
              '记忆只收她本人说的话里能看出来的事；你转述的网页、帖子、别人的事一律不收。不值得整理就只回 PASS。\n\n'
              '——对话——\n' + convo)
    text, _ = ask(prompt, system=_quiet_system(), max_tokens=4000)
    if text.strip().upper().startswith('PASS'):
        _arch_advance(batch, 'pass')
        return {'status': 'pass'}
    j = _json_from(text)
    if not j or not str(j.get('timeline', '')).strip().startswith('# 对话整理') or '当下状态' not in str(j.get('timeline', '')):
        raise RuntimeError('整理出来的格式不对，这一段先不落档')
    tl = str(j['timeline']).strip()[:6000]
    memory.add(tl, title='对话整理 · ' + day0, kind='日常', importance=4, date=day0, by='archive')
    kept = 0
    for m in (j.get('memories') or [])[:8]:
        if not isinstance(m, dict) or not m.get('text'):
            continue
        q = _norm(m.get('quote', ''))
        if not q or q not in her_text:          # 原话对不上就不收：防止把转述、编的当成她说的
            continue
        memory.add(m['text'], title=m.get('title', ''), quote=m.get('quote', ''), kind=m.get('kind', '日常'),
                   importance=m.get('importance', 5), valence=m.get('valence', 0), date=day0, by='archive')
        kept += 1
    if isinstance(j.get('threads'), list):
        memory.threads_set(open_items=j['threads'])
    _arch_advance(batch, 'written', kept)
    return {'status': 'written', 'memories': kept, 'day': day0}

def _arch_advance(batch, status, kept=0):
    with Locked('archive.json', {'cursor': 0}) as st:
        st['cursor'] = batch[-1]['seq']
        st.setdefault('history', []).append({'t': nowi(), 'status': status, 'n': len(batch), 'kept': kept})
        st['history'] = st['history'][-60:]
    activity('archive', status + ' · ' + str(len(batch)) + ' 条' + (' · 记下 ' + str(kept) + ' 件' if kept else ''))

def archive_flush(limit=10):
    for _ in range(limit):
        r = archive_once(force=True)
        if r['status'] in ('empty', 'one_side'):
            return True
    return True

# ───────────────────────── 换口气：常驻会话快满了，原地接着聊 ─────────────────────────
def rollover_needed():
    if mode() != 'sub':
        return False
    st = state()
    model = current_model()
    window = 1000000 if '[1m]' in model else 200000
    line = int(conf().get('roll_at') or window * 0.6)
    return st.get('ctx_tokens', 0) > line and st.get('ctx_chat') == (current_chat(False) or {}).get('id')

def rollover():
    """先把没整理的落进记忆、记下线头，再开新会话，把最近一段原话原样带过去——不做摘要"""
    chat = current_chat()
    archive_flush()
    rows = db("SELECT kind,text FROM msgs WHERE chat=? AND kind IN ('me','gu') ORDER BY seq DESC LIMIT 200", (chat['id'],))
    keep, size = [], 0
    for r in rows:
        size += len(r['text'] or '')
        if size > 24000:
            break
        keep.insert(0, r)
    while keep and keep[0]['kind'] != 'me':        # 从她说的话切，别从半截接
        keep.pop(0)
    c = conf()
    transcript = '\n'.join(('【' + c['me'] + '】' if r['kind'] == 'me' else '【' + c['him'] + '】') + r['text'] for r in keep)
    th = memory.threads()
    carry = ('【接着刚才——这是你们刚才的原话，你换了口气，从这里自然地接着聊，不用提换没换】\n' + transcript
             + ('\n【还没聊完的】' + '；'.join(t['text'] for t in th.get('open', [])) if th.get('open') else ''))
    state_set(carry={'chat': chat['id'], 'text': carry}, ctx_tokens=0)
    db('UPDATE chats SET session=? WHERE id=?', ('', chat['id']), write=True)
    CHAT_CLI.kill()
    activity('rollover', '换了口气，带过去 ' + str(len(keep)) + ' 句')

# ───────────────────────── 心跳：夜里自己醒 ─────────────────────────
WAKE_MAX, WAKE_GAP, WAKE_QUIET = 2, 190 * 60, 40 * 60

def wake_state():
    w = jload('wake.json', {'on': True})
    night = day_of(time.time() - 12 * 3600)
    if w.get('night') != night:
        w.update(night=night, count=0, last=0)
    return w

def wake_once(force=False):
    w = wake_state()
    c = conf()
    first = w.get('count', 0) == 0
    prompt = ('【心跳】没人叫你，这是你自己的时间（今晚第 ' + str(w.get('count', 0) + 1) + ' 次）。' + c['me'] + ' 已经睡了。\n'
              '挑一件事做——想她、复盘今天、看看她的清单和日历替她理一理。一次只做一件。\n'
              '然后只回一段 JSON，不要别的字：\n'
              '{"night": "留在夜记里的几行（你自己的话，不是写给她看的汇报）", '
              '"push": "早上想让她第一眼看到的一句，没有就 null", '
              '"moment": "想发的一条动态，没有就 null"'
              + (', "diary": [{"title": "", "kw": "关键词、顿号隔开", "text": "这一天里值得记的一段，你的视角", "s": 1到5, "v": -5到5, "a": 1到5}]' if first else '')
              + '}')
    text, _ = ask(prompt, system=_quiet_system() + '\n\n' + memory.session_start_text(), max_tokens=2000)
    j = _json_from(text) or {'night': text.strip()[:800]}
    if j.get('night'):
        home.night_add(j['night'])
        mark(c['him'] + ' 夜里醒了一次，写了几行夜记')
    if j.get('moment') and cooldown_ok('moment', 3 * 3600, 3):
        home.moments_act({'action': 'post', 'text': j['moment']}, who='gu')
        mark(c['him'] + ' 发了一条动态')
    for b in (j.get('diary') or [])[:4]:
        if isinstance(b, dict) and b.get('title'):
            home.wall_add(day_of(time.time() - 6 * 3600), b.get('title'), b.get('kw', ''), b.get('text', ''), b.get('s', 3), b.get('v', 0), b.get('a', 3))
    if j.get('push') and cooldown_ok('push', 2 * 3600, 6):
        push_to_her(c['him'], str(j['push'])[:200])
    w['count'] = w.get('count', 0) + 1
    w['last'] = time.time()
    jsave('wake.json', w)
    activity('wake', '第 ' + str(w['count']) + ' 次')
    return j

def make_news():
    c = conf()
    web = mode() == 'sub'
    prompt = ('给 ' + c['me'] + ' 排今天（' + today() + '）的《每日简报》，只有她一个读者。\n'
              + ('先上网查今天的新闻，' if web else '你今天上不了网，就从家里的事（日程、清单、她最近在读的书、天气）和你知道的常识里挑，')
              + '挑她会在意的。格式严格照下面（Markdown）：\n'
              '## 今天\n### 头条标题\n一两段正文。\n- 简讯一句｜一句点评\n## 科技与AI\n### …\n'
              '想插一句自己的话就单独一行写：【插一句】你的话\n不要写开场白，直接从第一个 ## 开始。')
    text, _ = ask(prompt, system=_quiet_system(), web=web, max_tokens=4000)
    if '##' in text:
        home.news_save(text[text.index('##'):])
        activity('news', today())
        return True
    return False

# ───────────────────────── 白天想她（默认关） ─────────────────────────
def proactive_once():
    c = conf()
    rows = db("SELECT kind,text,at FROM msgs WHERE kind IN ('me','gu') ORDER BY seq DESC LIMIT 12")
    rows.reverse()
    convo = '\n'.join(('她：' if r['kind'] == 'me' else '你：') + (r['text'] or '')[:400] for r in rows)
    prompt = ('现在 ' + now_local().strftime('%H:%M') + '，她已经有一阵子没说话了。这是你们最近的几句：\n' + convo +
              '\n\n如果你此刻真有一句想对她说的（想她了、想起一件事、提醒她吃饭喝水、接上没聊完的），写出来，一到三句，像发消息那样。\n'
              '没有真想说的，就只回 PASS。不要为了说而说。')
    text, _ = ask(prompt, system=_quiet_system() + '\n\n' + memory.session_start_text(), max_tokens=600, effort='medium')
    text = (text or '').strip()
    if not text or text.upper().startswith('PASS'):
        activity('proactive', 'PASS')
        return None
    chat = current_chat()
    add_msg(chat['id'], 'gu', text)
    BUS.emit({'type': 'proactive', 'text': text})
    push_to_her(c['him'], text[:120])
    activity('proactive', '主动说了一句')
    return text

# ───────────────────────── 总调度：每分钟看一眼 ─────────────────────────
def _due(name, every):
    st = jload('beat.json', {})
    if time.time() - st.get('t_' + name, 0) < every:
        return False
    with Locked('beat.json', {}) as d:
        d['t_' + name] = time.time()
    return True

def _job(name, fn):
    if job_paused(name):
        return
    try:
        r = fn()
        job_ok(name)
        return r
    except Exception as e:
        traceback.print_exc()
        job_fail(name, explain(e) if isinstance(e, ApiError) else e)

def last_msg_at():
    r = db("SELECT at FROM msgs WHERE kind IN ('me','gu') ORDER BY seq DESC LIMIT 1", one=True)
    return r['at'] if r else 0

def last_her_msg():
    r = db("SELECT at FROM msgs WHERE kind='me' ORDER BY seq DESC LIMIT 1", one=True)
    return r['at'] if r else 0

def tick():
    n = now_local()
    mins = n.hour * 60 + n.minute
    cf = conf()
    # 到点提醒、信到期：每分钟
    for t in home.todos_due():
        push_to_her('到点了', t['text'])
        mark('提醒了你：' + t['text'])
    for L in home.letters_due():
        who = '你' if L.get('from') == 'her' else cf['him']
        push_to_her('有封信可以拆了', who + '写的《' + L.get('title', '') + '》到期了')
        mark('《' + L.get('title', '') + '》到期了，信箱里能拆了')
        inbox_add('信《' + L.get('title', '') + '》到期解封了（' + ('她写的' if L.get('from') == 'her' else '你写的') + '）：' + L.get('text', '')[:400])
    if GU.busy:
        return
    # 整理记忆：每十分钟看一次够不够
    if _due('archive', 600):
        _job('archive', archive_once)
    # 常驻会话快满了：先落档再换口气
    if not GU.busy and rollover_needed() and time.time() - last_msg_at() > 120:
        _job('rollover', rollover)
    # 早报
    if cf.get('news_auto') and 6 * 60 <= mins < 11 * 60 and not os.path.exists(P('news', today() + '.md')) and _due('news_' + today(), 86400):
        _job('news', make_news)
    # 白天想她（默认关；频率由代码管：三小时一次、一天最多三次）
    if cf.get('proactive') and 9 * 60 <= mins < 23 * 60 and time.time() - last_msg_at() > 50 * 60 \
            and _due('proactive', 30 * 60) and cooldown_ok('proactive', 3 * 3600, 3):
        _job('proactive', proactive_once)
    # 夜里的心跳
    w = wake_state()
    if w.get('on', True) and (mins >= 23 * 60 + 30 or mins < 6 * 60 + 30) \
            and time.time() - max(GU.last_her, last_her_msg()) >= WAKE_QUIET \
            and w.get('count', 0) < WAKE_MAX and time.time() - w.get('last', 0) >= WAKE_GAP:
        w['last'] = time.time(); jsave('wake.json', w)
        _job('wake', wake_once)

def heartbeat_loop():
    while True:
        time.sleep(60)
        try:
            tick()
        except Exception:
            traceback.print_exc()

def start_background():
    threading.Thread(target=heartbeat_loop, daemon=True).start()
