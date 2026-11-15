# -*- coding: utf-8 -*-
"""Dump a fully-dated readable transcript from a DSH session jsonl (plain or zstd).
Shapes handled:
  user/message      -> data.content[*].text
  assistant/message -> data.message.content[*] of type reasoning|text|tool-call
  tool/call         -> data.name / data.arguments
  tool/result       -> data.message.content[*].content[*].text  (+ isError)
"""
import io, json, sys, datetime, re

src = sys.argv[1]
out = sys.argv[2]
want = sys.argv[3] if len(sys.argv) > 3 else 'all'   # all | errors | results
CLIP_A = 1400
CLIP_R = 2500

ERR = re.compile(r'(Traceback|isError.: ?true|ModuleNotFoundError|ImportError|No module named|PermissionError|FileNotFoundError|cannot open|Access is denied|ERROR:|error:|FAILED|failed|Timeout|timed out|超时|失败|错误|denied|EPERM|EACCES|HTTPError|SSLError|ConnectionError|ReadTimeout|ProxyError|WinError|zstd|not recognized)', re.I)


def open_text(src):
    with open(src, 'rb') as f:
        head = f.read(4)
    if head[:4] == b'\x28\xb5\x2f\xfd':
        import zstandard as zstd
        return io.TextIOWrapper(zstd.ZstdDecompressor().stream_reader(open(src, 'rb')), encoding='utf-8', errors='replace')
    return open(src, 'r', encoding='utf-8', errors='replace')


def parts_text(c):
    if c is None:
        return ''
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        acc = []
        for x in c:
            if isinstance(x, str):
                acc.append(x)
            elif isinstance(x, dict):
                if x.get('type') in ('text', 'reasoning'):
                    acc.append(x.get('text') or '')
                elif x.get('type') == 'tool-result':
                    inner = x.get('content')
                    acc.append(parts_text(inner))
                elif x.get('type') == 'tool-call':
                    pass
        return '\n'.join(a for a in acc if a)
    if isinstance(c, dict):
        return c.get('text') or ''
    return str(c)


def fmt(t):
    try:
        return datetime.datetime.fromtimestamp(t / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return '?'


o = io.StringIO()
n_err = 0
n_res = 0
for raw in open_text(src):
    raw = raw.strip()
    if not raw:
        continue
    try:
        e = json.loads(raw)
    except Exception:
        continue
    t = e.get('time') or e.get('createdAt')
    if not t:
        continue
    ty = e.get('type')
    d = e.get('data') or {}
    when = fmt(t)
    if ty == 'user/message':
        sr = d.get('source') or {}
        if sr.get('kind') and sr.get('kind') != 'user':
            continue
        s = parts_text(d.get('content'))
        if s.startswith('Current runtime context'):
            continue
        o.write('\n\n########## [%s] USER ##########\n%s\n' % (when, s[:4000]))
    elif ty == 'assistant/message':
        msg = d.get('message') or {}
        s = parts_text(msg.get('content'))
        if s.strip():
            o.write('\n---- [%s] ASSISTANT ----\n%s\n' % (when, s[:3000]))
    elif ty == 'tool/call':
        o.write('  [%s] CALL %s %s\n' % (when, d.get('name') or d.get('tool'), json.dumps(d.get('arguments') or d.get('args') or {}, ensure_ascii=False)[:CLIP_A]))
    elif ty == 'tool/result':
        msg = d.get('message') or {}
        body = parts_text(msg.get('content'))
        meta = e.get('meta') or {}
        iserr = 'isError": true' in json.dumps(msg, ensure_ascii=False) or bool(meta.get('isError'))
        n_res += 1
        hit = bool(ERR.search(body)) or iserr
        if hit:
            n_err += 1
        if want == 'results' or want == 'all' or (want == 'errors' and hit):
            o.write('  [%s] RESULT%s %s\n' % (when, ' !!ERROR!!' if hit else '', body[:CLIP_R].replace('\r', '').replace('\n', ' ¶ ')))
    elif ty == 'approval/asked':
        o.write('\n  [%s] APPROVAL-ASKED %s\n' % (when, json.dumps(d, ensure_ascii=False)[:1500]))
    elif ty == 'session/title':
        o.write('\n## [%s] TITLE %s\n' % (when, json.dumps(d, ensure_ascii=False)[:300]))

with open(out, 'w', encoding='utf-8') as f:
    f.write(o.getvalue())
print('wrote', out, 'results:', n_res, 'flagged:', n_err)
