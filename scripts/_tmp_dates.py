# -*- coding: utf-8 -*-
"""Inspect a DSH session blob (zstd or plain jsonl): record counts per local day,
first/last records, and every record dated on a target day."""
import io, json, os, sys, datetime, collections

src = sys.argv[1]
target = sys.argv[2] if len(sys.argv) > 2 else '2026-09-14'
out = sys.argv[3] if len(sys.argv) > 3 else None

with open(src, 'rb') as f:
    head = f.read(4)
is_zstd = head[:4] == b'\x28\xb5\x2f\xfd'

if is_zstd:
    import zstandard as zstd
    with open(src, 'rb') as f:
        data = zstd.ZstdDecompressor().stream_reader(f).read()
    text = data.decode('utf-8', 'replace')
else:
    with open(src, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()

lines = [l for l in text.split('\n') if l.strip()]
days = collections.Counter()
types = collections.Counter()
first = last = None
target_recs = []
bad = 0
for l in lines:
    try:
        e = json.loads(l)
    except Exception:
        bad += 1
        continue
    t = e.get('time') or e.get('createdAt')
    if t:
        try:
            d = datetime.datetime.fromtimestamp(t / 1000.0)
        except Exception:
            d = None
    else:
        d = None
    if d:
        days[d.strftime('%Y-%m-%d')] += 1
        if first is None or d < first[0]:
            first = (d, e)
        if last is None or d > last[0]:
            last = (d, e)
        if d.strftime('%Y-%m-%d') == target:
            target_recs.append((d, e))
    types[e.get('type')] += 1

buf = io.StringIO()
w = buf.write
w('SRC: %s\n' % src)
w('format: %s\n' % ('zstd' if is_zstd else 'plain-jsonl'))
w('total lines: %d  parsed: %d  unparsable: %d\n' % (len(lines), len(lines) - bad, bad))
w('first record: %s\n' % (first[0].isoformat() if first else 'n/a'))
w('last  record: %s\n' % (last[0].isoformat() if last else 'n/a'))
w('\n=== records per local day ===\n')
for k in sorted(days):
    w('  %s\t%d\n' % (k, days[k]))
w('\n=== record types ===\n')
for k, v in types.most_common():
    w('  %s\t%d\n' % (k, v))
w('\n=== records dated %s : %d ===\n' % (target, len(target_recs)))
for d, e in target_recs:
    s = json.dumps(e, ensure_ascii=False)
    w('\n--- %s type=%s ---\n%s\n' % (d.isoformat(), e.get('type'), s[:4000]))

res = buf.getvalue()
if out:
    with open(out, 'w', encoding='utf-8') as f:
        f.write(res)
    print('wrote', out, len(res))
else:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(res)
