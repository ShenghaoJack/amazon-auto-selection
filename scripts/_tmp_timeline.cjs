// Build a fully-dated timeline (optionally filtered to one calendar day) from a DSH session jsonl.
// Usage: node _tmp_timeline.cjs <in.jsonl> <out.txt> [YYYY-MM-DD]
const fs = require('fs');
const rl = require('readline');

const inp = process.argv[2];
const out = process.argv[3];
const day = process.argv[4] || null;
const o = fs.createWriteStream(out, { encoding: 'utf8' });
const p = (n) => String(n).padStart(2, '0');
const fmt = (ms) => { const d = new Date(ms); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
const clip = (s, n) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, n);
const textOf = (c) => (typeof c === 'string' ? c : Array.isArray(c) ? c.map((x) => (x && (x.text || x.content)) || '').join('\n') : '');

const days = new Map();
let n = 0;
const r = rl.createInterface({ input: fs.createReadStream(inp), crlfDelay: Infinity });
r.on('line', (line) => {
  if (!line.trim()) return;
  let e; try { e = JSON.parse(line); } catch { return; }
  const t = e.time || e.createdAt;
  if (!t) return;
  const when = fmt(t);
  const d0 = when.slice(0, 10);
  days.set(d0, (days.get(d0) || 0) + 1);
  if (day && d0 !== day) return;
  const d = e.data || {};
  if (e.type === 'user/message') {
    if (d.source && d.source.kind && d.source.kind !== 'user') return;
    const txt = textOf(d.content);
    if (/^Current runtime context/.test(txt)) return;
    if (txt.length > 12000) return;
    n++; o.write(`\n\n===== [${when}] 【用户】 ${clip(txt, 2500)}\n`);
  } else if (e.type === 'assistant/message') {
    const txt = textOf(d.content);
    if (!txt.trim()) return;
    n++; o.write(`[${when}] ASSISTANT: ${clip(txt, 900)}\n`);
  } else if (e.type === 'tool/call') {
    const name = d.name || d.tool || '?';
    if (name === 'todo_write' || name === 'get_goal' || name === 'list_agents') return;
    n++; o.write(`  [${when}] ${name}: ${clip(JSON.stringify(d.arguments || d.args || {}), 260)}\n`);
  }
});
r.on('close', () => {
  o.write(`\n\n=== date histogram (records per local day) ===\n`);
  for (const [k, v] of [...days.entries()].sort()) o.write(`  ${k}\t${v}\n`);
  o.write(`\n(events emitted: ${n})\n`);
  o.end();
});
