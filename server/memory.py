"""记忆：存、找、想起来。只用标准库。

思路参考了小红书上大家的做法（Latent · 显影、Ombre Brain、小克 Cat），代码是自己写的：
· 三路检索：BM25（字符二元组）+ 二元组余弦 + 共享显著词的"关系"路，用 RRF 融合
· 用进废退：被真正想起来的记忆权重上升；不按时间自动变淡
· 诚实边界：分数够不上绝对门槛就当没找到，不拿沾边的去圆
· 只记她本人说过的：后台整理出来的每条记忆都要带一句她的原话，原话对不上就不收
· 会长的东西都去重、封顶
"""
import math
import re
from collections import Counter

from core import Locked, jload, nowi, today, day_of

MEM0 = {'items': [], 'next_id': 1}
KINDS = ('日常', '关系', '事件', '约定', '长期')
TEXT_CAP = 800

# ---------- 切词：中文按字二元组，英文/数字按词 ----------
_word = re.compile(r'[a-zA-Z0-9]+|[一-鿿]+')
STOP = set('的了是我你他她它在有和就也都不这那哪吧呢啊吗嗯哦呀么个一们着过来去说要会能什怎还很太想把被给让对从到为又再才只得地吗啦嘛呗')

def grams(text):
    out = []
    for w in _word.findall((text or '').lower()):
        if w[0].isascii():
            if len(w) > 1:
                out.append(w)
        else:
            if len(w) == 1:
                if w not in STOP:
                    out.append(w)
                continue
            for i in range(len(w) - 1):
                g = w[i:i + 2]
                if g[0] not in STOP and g[1] not in STOP:   # 带虚字的二元组（那天、是哪）多半是跨词的噪音
                    out.append(g)
    return out

def _doc(it):
    return (it.get('title', '') + ' ' + it.get('text', '') + ' ' + it.get('quote', '') + ' ' + it.get('kw', ''))

# ---------- 读写 ----------
def items(include_all=False):
    L = jload('memory.json', MEM0).get('items', [])
    return L if include_all else [x for x in L if x.get('status', 'current') == 'current']

def get(mid):
    return next((x for x in items(True) if x['id'] == mid), None)

def _similar(a, b):
    ga, gb = set(grams(a)), set(grams(b))
    if not ga or not gb:
        return 0
    return len(ga & gb) / len(ga | gb)

def add(text, title='', quote='', kind='日常', importance=5, valence=0, date=None, by='gu', kw=''):
    text = re.sub(r'\s+\n', '\n', str(text or '')).strip()[:TEXT_CAP]
    if not text:
        return None
    kind = kind if kind in KINDS else '日常'
    with Locked('memory.json', MEM0) as d:
        for x in d['items']:                       # 去重：几乎一样的就不再记一遍，只把权重抬一点
            if x.get('status', 'current') == 'current' and _similar(x['text'], text) > 0.8:
                x['weight'] = x.get('weight', 0) + 1
                x['updated'] = nowi()
                return x
        mid = 'm' + str(d.get('next_id', 1))
        d['next_id'] = d.get('next_id', 1) + 1
        it = {'id': mid, 'date': date or today(), 'kind': kind, 'title': str(title or text[:20])[:40], 'text': text,
              'quote': str(quote or '')[:300], 'kw': str(kw or '')[:80], 'importance': _clamp(importance, 1, 10, 5),
              'valence': _clamp(valence, -5, 5, 0), 'weight': 0, 'status': 'current', 'by': by,
              'created': nowi(), 'updated': nowi()}
        d['items'].append(it)
    return it

def _clamp(v, lo, hi, dv):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return dv

def edit(mid, **kw):
    with Locked('memory.json', MEM0) as d:
        x = next((i for i in d['items'] if i['id'] == mid), None)
        if not x:
            return None
        for k in ('text', 'title', 'kind', 'quote', 'kw', 'date'):
            if k in kw and kw[k] is not None:
                x[k] = str(kw[k])[:TEXT_CAP if k == 'text' else 300]
        for k, lo, hi in (('importance', 1, 10), ('valence', -5, 5)):
            if k in kw and kw[k] is not None:
                x[k] = _clamp(kw[k], lo, hi, x.get(k, 0))
        x['updated'] = nowi()
        return x

def supersede(mid, new_text, by='gu'):
    """事实变了：旧的留着当历史，新的接上"""
    old = get(mid)
    if not old:
        return None
    new = add(new_text, title=old.get('title'), kind=old.get('kind', '日常'), importance=old.get('importance', 5),
              valence=old.get('valence', 0), by=by)
    with Locked('memory.json', MEM0) as d:
        for x in d['items']:
            if x['id'] == mid:
                x['status'] = 'superseded'; x['by_new'] = new['id']
            if x['id'] == new['id']:
                x['supersedes'] = mid
    return new

def retract(mid):
    """从来就不是真的：以后谁都搜不到"""
    with Locked('memory.json', MEM0) as d:
        for x in d['items']:
            if x['id'] == mid:
                x['status'] = 'retracted'; x['updated'] = nowi()
                return True
    return False

def delete(mid):
    with Locked('memory.json', MEM0) as d:
        n = len(d['items'])
        d['items'] = [x for x in d['items'] if x['id'] != mid]
        return len(d['items']) < n

# ---------- 检索 ----------
def _rank(query, pool):
    q = grams(query)
    if not q or not pool:
        return [], {}
    docs = [grams(_doc(x)) for x in pool]
    N = len(docs)
    df = Counter()
    for g in docs:
        df.update(set(g))
    avgdl = sum(len(g) for g in docs) / max(1, N)
    idf = {t: math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) for t in set(q)}
    qc = Counter(q)
    bm, cos, hit_idf = [], [], []
    for g in docs:
        tf = Counter(g)
        dl = len(g) or 1
        s = 0.0
        h = 0.0
        for t in qc:
            if tf.get(t):
                s += idf[t] * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * dl / avgdl))
                h += 1
        bm.append(s)
        hit_idf.append(h)
        dot = sum(qc[t] * tf.get(t, 0) for t in qc)
        na = math.sqrt(sum(v * v for v in qc.values())) or 1
        nb = math.sqrt(sum(v * v for v in tf.values())) or 1
        cos.append(dot / (na * nb))
    # 关系路：和 BM25 前几名共享"显著词"（全库里少见的二元组）的，也算沾上
    top = sorted(range(N), key=lambda i: -bm[i])[:3]
    rare = set()
    for i in top:
        if bm[i] > 0:
            rare |= {t for t in set(docs[i]) if df[t] <= max(2, N // 20)}
    rel = [len(rare & set(g)) for g in docs]
    rrf = Counter()
    for scores in (bm, cos, rel):
        order = sorted((i for i in range(N) if scores[i] > 0), key=lambda i: -scores[i])
        for r, i in enumerate(order):
            rrf[i] += 1 / (60 + r)
    for i in rrf:                                   # 用进废退：常被想起的往前挪一点
        rrf[i] *= 1 + 0.08 * math.log1p(pool[i].get('weight', 0)) + 0.02 * (pool[i].get('importance', 5) - 5)
    q_idf = len(qc) or 1          # 覆盖率按词数算：小库里没见过的词 idf 偏大，按权重算会把真命中压没
    info = {i: {'bm25': round(bm[i], 3), 'cover': round(hit_idf[i] / q_idf, 3)} for i in rrf}
    return sorted(rrf, key=lambda i: -rrf[i]), info

def search(query, top=5, update=True, min_cover=0.3, history=False):
    """返回 [(item, info)]。覆盖率（命中的词占问法信息量的比例）够不上门槛的不算命中——宁可说没有"""
    pool = items(True) if history else items()
    pool = [x for x in pool if x.get('status') != 'retracted']
    order, info = _rank(query, pool)
    out = [(pool[i], info[i]) for i in order if info[i]['cover'] >= min_cover and info[i]['bm25'] > 0][:top]
    if update and out:
        ids = {x['id'] for x, _ in out}
        with Locked('memory.json', MEM0) as d:
            for x in d['items']:
                if x['id'] in ids:
                    x['weight'] = x.get('weight', 0) + 1
                    x['last_hit'] = nowi()
    return out

def fmt(it, n=220):
    t = it['text'] if len(it['text']) <= n else it['text'][:n] + '…'
    return '[' + it.get('date', '') + ' · ' + it.get('kind', '') + '] ' + t

# ---------- 自动浮现：她每说一句，先想想有没有相关的旧事 ----------
RECALL_COOLDOWN = 12 * 3600
RECALL_BUDGET = 1000

def recall_for(text):
    if len(grams(text)) < 2:
        return ''
    hits = search(text, top=6, update=False, min_cover=0.3)
    if not hits:
        return ''
    log = jload('recall_log.json', {})
    now = nowi()
    picked, used = [], 0
    for it, _ in hits:
        if now - log.get(it['id'], 0) < RECALL_COOLDOWN:
            continue
        line = fmt(it, 200)
        if used + len(line) > RECALL_BUDGET:
            break
        picked.append((it, line)); used += len(line)
        if len(picked) >= 3:
            break
    if not picked:
        return ''
    with Locked('recall_log.json', {}) as d:
        for it, _ in picked:
            d[it['id']] = now
        for k in [k for k, v in d.items() if now - v > 7 * 86400]:
            del d[k]
    with Locked('memory.json', MEM0) as d:
        ids = {it['id'] for it, _ in picked}
        for x in d['items']:
            if x['id'] in ids:
                x['weight'] = x.get('weight', 0) + 1
                x['last_hit'] = now
    return '\n'.join(l for _, l in picked)

# ---------- 线头：还没聊完的 ----------
def threads():
    return jload('threads.json', {'open': [], 'last': None})

def threads_set(open_items=None, last=None):
    with Locked('threads.json', {'open': [], 'last': None}) as d:
        if open_items is not None:
            seen, clean = set(), []
            for t in open_items:
                t = str(t).strip()[:160]
                if t and t not in seen:
                    seen.add(t); clean.append({'text': t, 'at': nowi()})
            d['open'] = clean[:8]
        if last is not None:
            d['last'] = {'text': str(last)[:1200], 'at': nowi()}

def threads_close(text):
    with Locked('threads.json', {'open': [], 'last': None}) as d:
        d['open'] = [t for t in d['open'] if text not in t['text']]

def session_start_text():
    """新会话开场：没聊完的 → 上次聊到哪 → 最近和最常想起的几件"""
    th = threads()
    L = []
    if th.get('open'):
        L.append('【还没聊完的】' + '；'.join(t['text'] for t in th['open']))
    if th.get('last'):
        L.append('【上次聊到】（' + day_of(th['last']['at']) + ' 的快照，是过去的状态）' + th['last']['text'])
    its = items()
    if its:
        recent = sorted(its, key=lambda x: (x.get('date', ''), x.get('created', 0)), reverse=True)[:4]
        heavy = sorted(its, key=lambda x: (x.get('importance', 5) + math.log1p(x.get('weight', 0))), reverse=True)[:3]
        seen, L2 = set(), []
        for x in recent + heavy:
            if x['id'] not in seen:
                seen.add(x['id']); L2.append(fmt(x, 160))
        L.append('【记得的事】\n' + '\n'.join(L2))
    return '\n'.join(L) if L else '记忆库还是空的。'

# ---------- 给页面看的 ----------
def ripples():
    """按天聚成涟漪：大小=那天记下几件，颜色=冷暖，线型=种类"""
    days = {}
    for x in items():
        d = days.setdefault(x.get('date', today()), {'date': x.get('date', today()), 'n': 0, 'v': 0, 'kinds': Counter()})
        d['n'] += 1; d['v'] += x.get('valence', 0); d['kinds'][x.get('kind', '日常')] += 1
    out = []
    for k in sorted(days):
        d = days[k]
        out.append({'date': k, 'n': d['n'], 'valence': round(d['v'] / d['n'], 2), 'kinds': dict(d['kinds'])})
    return out

def stats():
    its = items()
    return {'total': len(its), 'days': len({x.get('date') for x in its}),
            'by_kind': dict(Counter(x.get('kind', '日常') for x in its))}
