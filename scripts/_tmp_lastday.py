# -*- coding: utf-8 -*-
"""Stream a DSH session blob and print every record whose local date is in a
target set (default 2026-09-14). Streams, so it is safe on very large blobs."""
import io, json, sys, datetime

src = sys.argv[1]
targets = set((sys.argv[2] if len(sys.argv) > 2 else '2026-09-14').split(','))
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 40

with open(src, 'rb') as f:
    head = f.read(4)

if head[:4] == b'\x28\xb5\x2f\xfd':
    import zstandard as zstd
    fh_text = io.TextIOWrapper(zstd.ZstdDecompressor().stream_reader(open(src, 'rb')), encoding='utf-8', errors='replace')
else:
    fh_text = open(src, 'r', encoding='utf-8', errors='replace')

print('SRC', src)
n = 0
total = 0
for raw in fh_text:
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
    total += 1
    d = datetime.datetime.fromtimestamp(t / 1000.0)
    if d.strftime('%Y-%m-%d') in targets:
        n += 1
        if n <= limit:
            print('  %s  type=%s  data=%s' % (d.strftime('%Y-%m-%d %H:%M:%S'), e.get('type'), json.dumps(e.get('data') or {}, ensure_ascii=False)[:400]))
print('  -> dated records total: %d ; matching %s : %d' % (total, sorted(targets), n))
