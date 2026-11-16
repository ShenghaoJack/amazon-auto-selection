// Exercise the production functions with a small DOM and an isolated API store.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '..', 'webui', 'index.html'), 'utf8');
const names = ['attrDimension', 'syncAttrButtons', 'applyAttrAction', 'markAttrDirty', 'attrRow',
  'renderAttrRows', 'reindexAttrRows', 'updateAttrStatus', 'collectAttrRows', 'saveAttrSpec',
  'loadAttrSpec', 'attrExcludeAction'];
const dimOrder = html.match(/^const ATTR_DIM_ORDER = .*;$/m)[0];
const functions = dimOrder + '\n' + names.map(name => {
  const start = html.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, `missing ${name}`);
  const close = html.slice(start).search(/^}\r?$/m);
  assert.ok(close >= 0);
  return html.slice(start, start + close + 1);
}).join('\n');

async function runCase(failSave) {
  let entries = [];
  const rows = {
    set innerHTML(value) {
      // 2026-10-21: 生产 attrRow 的 hint 已从 `<input value>` 改为 `<textarea>…</textarea>`
      //   (用户"hint 文本框太小/样式高级一点") → 正则同步改为取 textarea 内容；
      //   同时兼容旧的 input value 形态, 两种都能取到。
      const re = /class="attr-name" value="([^"]*)"[\s\S]*?class="attr-hint"(?:\s+value="([^"]*)"|[^>]*>([^<]*)<\/textarea>)/g;
      const matches = [...value.matchAll(re)];
      entries = matches.map(m => ({name: {value: m[1]}, hint: {value: m[2] !== undefined ? m[2] : m[3]}}));
    },
    // 2026-10-21: `collectAttrRows` 现在读 `tr.dataset.isNew` / `tr.dataset.bands`(价格段/新老属性)
    //   → mock 行对象必须带 `dataset: {}`，否则 `tr.dataset.isNew` 抛 TypeError。
    querySelectorAll() { return entries.map(e => ({dataset: {}, querySelector(s) { return s === '.attr-name' ? e.name : s === '.attr-hint' ? e.hint : {}; }})); }
  };
  const elements = {attrRows: rows, attrStatusHint: {}, attrSummary: {}, btnSaveAttr: {}, btnConfirmAttr: {}};
  let state = {ok: true, exists: true, status: 'confirmed', revision: 'r1',
    attributes: [{name: '材质', hint: '旧说明'}], candidates: [], excluded: []};
  const calls = [];
  let delayLoad = false, finishDelayed;
  let next = 1;
  const ctx = vm.createContext({
    document: {querySelectorAll() { return entries.flatMap(e => [e.name, e.hint]); }},
    $: id => elements[id], esc: String, __panelDup: () => false,
    // 2026-10-14: `loadAttrSpec` 新增调用 `renderAttrSpecRuns` / `renderAttrMinRate`
    // (index.html 10-13 改动)。未 stub 时 vm 里是 ReferenceError → 被 loadAttrSpec 的
    // catch 吞掉 → 渲染成"加载失败"行 → 正则取不到条目 → 报误导性的 `.hint of undefined`。
    // 2026-10-21: `renderAttrRows` 又新增依赖 `renderBandLegend` / `bandStrip` / `autoGrowHints`
    // (价格段点条 UI)，同样必须 stub，否则同上。
    renderAttrExcluded() {}, renderAttrSpecRuns() {}, renderAttrMinRate() {},
    renderBandLegend() {}, bandStrip() { return ""; }, autoGrowHints() {},
    toast() {},
    async api(url, options) {
      if (!options) {
        if (delayLoad) {
          delayLoad = false;
          return new Promise(resolve => { finishDelayed = resolve; });
        }
        return JSON.parse(JSON.stringify(state));
      }
      calls.push({url, body: options.body});
      assert.equal(options.body.expected_revision, state.revision);
      assert.ok(entries.every(e => e.name.disabled && e.hint.disabled), 'edits must be locked during requests');
      if (url === '/api/attr-spec/save') {
        if (failSave) throw new Error('fixture save failure');
        state.attributes = structuredClone(options.body.attributes);
      } else if (url === '/api/attr-exclude') {
        state.attributes.push({name: options.body.name, hint: ''});
      } else {
        throw new Error('unexpected URL ' + url);
      }
      state.status = 'pending_confirm';
      state.revision = 'r' + (++next);
      return structuredClone(state);
    }
  });
  vm.runInContext('let attrData = null, __attrDirty = false, __attrBusy = false, __attrLoadSequence = 0, selFile = "data/test/config.yaml";\n' + functions, ctx);
  await vm.runInContext('loadAttrSpec(true)', ctx);
  entries[0].hint.value = '人工未保存说明';
  vm.runInContext('markAttrDirty()', ctx);
  await vm.runInContext('saveAttrSpec()', ctx);
  if (failSave) {
    assert.equal(calls.length, 1);
    assert.equal(state.attributes.length, 1);
    assert.equal(entries[0].hint.value, '人工未保存说明');
    assert.equal(vm.runInContext('__attrDirty', ctx), true);
  } else {
    assert.equal(calls[0].url, '/api/attr-spec/save');
    assert.equal(state.attributes[0].hint, '人工未保存说明');
    assert.deepEqual(entries.map(e => e.name.value), ['材质']);
    assert.equal(elements.btnConfirmAttr.disabled, false);
    await vm.runInContext('attrExcludeAction("包装形式", "mkt", true)', ctx);
    assert.deepEqual(entries.map(e => e.name.value), ['材质', '包装形式']);
    entries[0].hint.value = '再次编辑说明';
    vm.runInContext('markAttrDirty()', ctx);
    assert.equal(await vm.runInContext('saveAttrSpec()', ctx), true);
    assert.equal(state.attributes.length, 2, 'saving must retain the restored attribute');
    assert.equal(state.attributes[0].hint, '再次编辑说明');
    delayLoad = true;
    const oldRequest = vm.runInContext('loadAttrSpec(true)', ctx);
    await vm.runInContext('loadAttrSpec(true)', ctx);
    finishDelayed({exists: true, status: 'confirmed', revision: 'r1', attributes: [{name: '材质', hint: '旧说明'}]});
    await oldRequest;
    assert.equal(vm.runInContext('attrData.revision', ctx), state.revision);
    assert.equal(entries.length, 2, 'older load responses must not replace the fresh table');
  }
  assert.equal(vm.runInContext('__attrBusy', ctx), false);
  assert.ok(entries.every(e => !e.name.disabled && !e.hint.disabled));
}

(async () => {
  await runCase(false);
  await runCase(true);
  process.stdout.write('Production UI draft/adopt/restore/save and failure checks passed.\n');
})().catch(e => { console.error(e); process.exitCode = 1; });
