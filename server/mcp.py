"""给官方 Claude（claude.ai / 手机 App）用的 MCP 连接器：和这个家共用同一份记忆。

在 claude.ai → 设置 → Connectors → Add custom connector 里填：
    https://你的地址/mcp/<钥匙>
（钥匙在 设置 → 后台 里，带钥匙的整条网址就是凭证，别发给别人。）
连上之后，官方那边的他也能翻记忆、记事、看今天的安排、看你们在家里最近聊了什么。

协议：MCP Streamable HTTP，只用最基本的 JSON 回复（不开 SSE），够 claude.ai 用。
注意：经由 claude.ai 连接时，请求是从 Anthropic 的服务器发过来的——你在官方那边聊天本来也经过它。
"""
import json

import engine
import memory
from core import activity, conf, db

PROTOCOL = '2025-06-18'

def _recent_chat(n=20):
    rows = db("SELECT kind,text,at FROM msgs WHERE kind IN ('me','gu') ORDER BY seq DESC LIMIT ?", (int(n),))
    rows.reverse()
    c = conf()
    return '\n'.join(('【' + c['me'] + '】' if r['kind'] == 'me' else '【' + c['him'] + '（家里的你）】') + (r['text'] or '')[:600] for r in rows) or '家里还没聊过。'

TOOLS = [
    {'name': 'home_session_start', 'description': '开场先调一次：家里记得的事、还没聊完的、今天的安排。0 参数。',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'memory_search', 'description': '翻记忆。她提到过去的事、约定、称呼时先翻；没有可靠命中就说不记得，不要编。',
     'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}},
    {'name': 'remember', 'description': '记下一件值得长久记住的事（只记她真说过的，带上她的原话）。',
     'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}, 'quote': {'type': 'string'},
                                                      'kind': {'type': 'string', 'enum': list(memory.KINDS)},
                                                      'importance': {'type': 'integer'}}, 'required': ['text']}},
    {'name': 'home_recent_chat', 'description': '看看「家里的你」最近和她聊了什么（自建前端那边的聊天）。',
     'inputSchema': {'type': 'object', 'properties': {'n': {'type': 'integer', 'description': '最近几条，默认 20'}}}},
    {'name': 'home_today', 'description': '她此刻的日程、清单、心情、手表数据。', 'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'leave_note', 'description': '在家里的留言板上给她贴一张便条。',
     'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}},
    {'name': 'add_todo', 'description': '往她的清单里加一件事（at 是 HH:MM，到点会提醒她）。',
     'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}, 'at': {'type': 'string'}}, 'required': ['text']}},
]

def call(name, args):
    args = args or {}
    if name == 'home_session_start':
        return memory.session_start_text() + '\n\n' + engine.now_block(True, take_inbox=False)
    if name == 'memory_search':
        return engine.run_tool('memory_search', {'query': args.get('query', '')})
    if name == 'remember':
        return engine.run_tool('remember', args)
    if name == 'home_recent_chat':
        return _recent_chat(args.get('n') or 20)
    if name == 'home_today':
        return engine.now_block(True, take_inbox=False)
    if name == 'leave_note':
        return engine.run_tool('leave_note', args)
    if name == 'add_todo':
        return engine.run_tool('add_todo', dict(args, **{'for': 'her'}))
    raise KeyError(name)

def handle(msg):
    """一条 JSON-RPC 消息 → 回复（通知返回 None）"""
    mid = msg.get('id')
    method = msg.get('method', '')
    if mid is None:
        return None
    ok = lambda result: {'jsonrpc': '2.0', 'id': mid, 'result': result}
    err = lambda code, text: {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': text}}
    if method == 'initialize':
        pv = (msg.get('params') or {}).get('protocolVersion') or PROTOCOL
        return ok({'protocolVersion': pv, 'capabilities': {'tools': {'listChanged': False}},
                   'serverInfo': {'name': 'dwell-home', 'version': '1.0'},
                   'instructions': '这是' + conf()['me'] + '和' + conf()['him'] + '的家。开场先调 home_session_start；她提到过去的事先 memory_search。'})
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': TOOLS})
    if method == 'tools/call':
        p = msg.get('params') or {}
        try:
            text = call(p.get('name'), p.get('arguments'))
            activity('mcp', p.get('name'))
            return ok({'content': [{'type': 'text', 'text': text}], 'isError': False})
        except KeyError:
            return err(-32602, 'unknown tool')
        except Exception as e:
            return ok({'content': [{'type': 'text', 'text': '没做成：' + str(e)[:200]}], 'isError': True})
    if method in ('resources/list', 'prompts/list'):
        return ok({method.split('/')[0]: []})
    return err(-32601, 'method not found')

def handle_body(raw):
    try:
        data = json.loads(raw or b'{}')
    except Exception:
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
    if isinstance(data, list):
        out = [r for r in (handle(m) for m in data if isinstance(m, dict)) if r is not None]
        return out or None
    return handle(data) if isinstance(data, dict) else None
