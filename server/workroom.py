"""工作间：手机上直接用这台机器上的 Claude Code 干活（改代码、查服务器、整理文件）。

和聊天分开：聊天那边只放行联网和家里的几样工具；这里是一个完整的 Claude Code 会话，
工作目录可以选，能接上你在电脑上开过的 Claude Code 会话（同一台机器、同一个目录）。
它能读写那个目录里的文件、执行命令——只给自己用，后端一定要设密码（HOME_PASSWORD）。
"""
import glob
import json
import os
import threading
import time

import engine
from core import Bus, activity, conf, conf_set, jload, jsave, nowi, state, state_set

WORKBUS = Bus()
WORK_TOOLS = ['Bash', 'Read', 'Edit', 'Write', 'Glob', 'Grep', 'WebSearch', 'WebFetch']
SESSION = engine.CliSession('work', WORK_TOOLS, WORK_TOOLS, perm='acceptEdits', prompt_name='prompt_work.md')
SESSION.append = True
_busy = {'on': False, 'since': 0}
_cancel = threading.Event()

def work_dir():
    d = os.path.expanduser(conf().get('work_dir') or '~')
    return d if os.path.isdir(d) else os.path.expanduser('~')

def _log_add(kind, text, extra=None):
    d = jload('work_log.json', {'items': []})
    d['items'].append({'k': kind, 't': text[:20000], 'x': extra, 'at': nowi()})
    d['items'] = d['items'][-400:]
    jsave('work_log.json', d)

def _project_dir(path):
    """Claude Code 把会话记录放在 ~/.claude/projects/<目录名>/，目录名是把路径里的分隔符换成 -"""
    base = os.path.expanduser('~/.claude/projects')
    import re
    name = re.sub(r'[:\\/._]+', '-', path)
    for cand in (name, name.lstrip('-')):
        if os.path.isdir(os.path.join(base, cand)):
            return os.path.join(base, cand)
    return None

def sessions():
    pd = _project_dir(work_dir())
    files = sorted(glob.glob(os.path.join(pd, '*.jsonl')) if pd else [], key=os.path.getmtime, reverse=True)[:30]
    out = []
    for f in files:
        first = ''
        try:
            with open(f, encoding='utf-8', errors='ignore') as fh:
                for line in fh:
                    try:
                        j = json.loads(line)
                    except Exception:
                        continue
                    if j.get('type') == 'user' and not j.get('toolUseResult') and not j.get('isSidechain'):
                        c = (j.get('message') or {}).get('content')
                        first = c if isinstance(c, str) else ' '.join(x.get('text', '') for x in (c or []) if isinstance(x, dict) and x.get('type') == 'text')
                        if first.strip():
                            break
        except OSError:
            continue
        out.append({'id': os.path.basename(f)[:-6], 'mtime': int(os.path.getmtime(f)), 'first': first.strip()[:80]})
    return out

def _turn(text):
    exe = engine.find_claude()
    try:
        if not exe:
            raise RuntimeError('这台机器上没找到 claude 命令——先装好 Claude Code 并登录一次')
        model = engine.current_model() if engine.mode() == 'sub' else (state().get('work_model') or '')
        sysp = ('你在 ' + conf()['me'] + ' 的机器上帮她干活，她在手机上看着。做之前用一两句说清楚要做什么；'
                '删除、覆盖、推送、花钱的事先问她。回复简短，结果说清楚。')
        SESSION.cwd = work_dir()
        with SESSION.lock:
            SESSION.ensure(exe, model, engine.current_effort(), sysp, state().get('work_sid') or '')
            SESSION.send([{'type': 'text', 'text': text}])
            got = False
            for ev in SESSION.events(_cancel, idle_timeout=1800):
                t = ev.get('type')
                if t in ('_dead', '_timeout'):
                    if not got:
                        err = ''
                        try:
                            err = (SESSION.proc.stderr.read() or '')[-300:] if SESSION.proc else ''
                        except Exception:
                            pass
                        raise RuntimeError(err or '工作间的 Claude Code 没回话')
                    break
                got = True
                if t == 'system' and ev.get('subtype') == 'init' and ev.get('session_id'):
                    SESSION.sid = ev['session_id']
                    state_set(work_sid=ev['session_id'])
                elif t == 'assistant':
                    for p in (ev.get('message') or {}).get('content') or []:
                        if p.get('type') == 'text' and p.get('text'):
                            _log_add('gu', p['text'])
                        elif p.get('type') == 'tool_use':
                            _log_add('tool', p.get('name', ''), p.get('input'))
                if t in engine.FORWARD:
                    WORKBUS.emit(ev)
        activity('work', '工作间回了一轮')
    except Exception as e:
        if 'No conversation found' in str(e):
            state_set(work_sid='')
        WORKBUS.emit({'type': 'result', 'is_error': True, 'result': str(e)[:300]})
        _log_add('err', str(e)[:300])
    finally:
        _busy['on'] = False
        WORKBUS.emit({'type': 'idle'})

def route_get(path, q):
    if path == 'state':
        return {'ok': True, 'busy': _busy['on'], 'dir': work_dir(), 'sid': state().get('work_sid') or '',
                'found': bool(engine.find_claude()), 'alive': SESSION.alive()}
    if path == 'poll':
        evs, nxt = WORKBUS.since(int((q.get('since') or ['0'])[0] or 0), timeout=25)
        return {'next': nxt, 'events': evs}
    if path == 'log':
        return {'items': jload('work_log.json', {'items': []})['items'][-200:], 'upto': WORKBUS.n}
    if path == 'sessions':
        return {'ok': True, 'dir': work_dir(), 'items': sessions()}
    return None

def route_post(path, b):
    if path == 'send':
        text = str(b.get('text') or '').strip()
        if not text:
            return {'ok': False}
        if _busy['on']:
            return {'ok': False, 'error': '上一件还没干完，先停下或等一等'}
        _busy.update(on=True, since=nowi())
        _cancel.clear()
        _log_add('me', text)
        WORKBUS.emit({'type': 'echo', 'text': text})
        threading.Thread(target=_turn, args=(text,), daemon=True).start()
        return {'ok': True}
    if path == 'stop':
        _cancel.set()
        SESSION.interrupt()
        for _ in range(20):
            if not _busy['on']:
                return {'ok': True}
            time.sleep(0.1)
        return {'ok': not _busy['on'], 'error': '' if not _busy['on'] else '已经请它停了，还没停下来'}
    if path == 'new':
        SESSION.kill(); state_set(work_sid='')
        jsave('work_log.json', {'items': []})
        return {'ok': True}
    if path == 'resume':
        SESSION.kill(); state_set(work_sid=str(b.get('id') or ''))
        _log_add('sys', '接上了电脑上的会话 ' + str(b.get('id'))[:8])
        return {'ok': True}
    if path == 'dir':
        p = os.path.expanduser(str(b.get('path') or '').strip())
        if not os.path.isdir(p):
            return {'ok': False, 'error': '这台机器上没有这个文件夹'}
        conf_set(work_dir=p)
        SESSION.kill(); state_set(work_sid='')
        return {'ok': True, 'dir': p}
    return None
