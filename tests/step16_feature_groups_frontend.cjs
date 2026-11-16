const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '..', 'webui', 'index.html'), 'utf8');
const extract = name => {
  const start = html.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, `missing ${name}`);
  const end = html.slice(start).search(/^}\r?$/m);
  return html.slice(start, start + end + 1);
};
const funcs = ['attrRow', 'collectAttrRows', 'deleteAttrRow', 'ungroupAttrRow'].map(extract).join('\n');
const children = [{name:'是否甲',hint:'旧甲'}, {name:'是否乙',hint:'旧乙'}];
let removed = false, confirmResult = false, rendered = null;
const parent = {value:'设备功能'}, hint = {value:'分组说明'};
const tr = {
  dataset: {group:JSON.stringify(children), bands:'[]', isNew:'0'},
  querySelector(s) { return s === '.attr-name' ? parent : s === '.attr-hint' ? hint : null; },
  querySelectorAll(s) { return s === '.attr-group-hint' ? [
    {dataset:{childIndex:'0'},value:'新甲'}, {dataset:{childIndex:'1'},value:'旧乙'}] : []; },
  remove() { removed = true; }
};
const ctx = vm.createContext({
  esc: s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;', '"':'&quot;', "'":'&#39;'}[c])),
  $: id => id === 'attrRows' ? {querySelectorAll: () => [tr]} : null,
  window: {confirm: () => confirmResult}, renderAttrRows: rows => { rendered = rows; }, markAttrDirty() {}
});
vm.runInContext(funcs, ctx);
const markup = ctx.attrRow('设备功能', '分组说明', 1, '', false, [], false, children);
assert.match(markup, /查看和编辑 2 个独立子项/);
assert.match(markup, /readonly/);
assert.match(markup, /class="attr-ungroup/);
assert.doesNotMatch(markup, /class="attr-ignore/);
const result = ctx.collectAttrRows();
assert.equal(result[0].group_members[0].hint, '新甲');
assert.equal(result[0].group_members[1].hint, '旧乙');
ctx.deleteAttrRow({closest: () => tr});
assert.equal(removed, false, 'cancel must preserve the whole group');
ctx.ungroupAttrRow({closest: () => tr});
assert.equal(rendered.length, 2);
assert.equal(rendered[0].hint, '新甲');
process.stdout.write('功能组界面渲染、子项编辑及删除取消检查通过\n');
