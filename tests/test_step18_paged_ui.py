"""Browser smoke test of the real panel with isolated in-memory API responses."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from core.browser import find_chrome_path


def test_paged_preview_filters_across_pages_and_applies_selection():
    root = Path(__file__).resolve().parents[1]
    markup = (root / "webui/index.html").read_text(encoding="utf-8")
    start = markup.index('  <section class="card fade-up" id="secStd">')
    end = markup.index('  <!-- ============ ⑧ 配置中心', start)
    script = (root / "webui/step18-panel.js").read_text(encoding="utf-8")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=find_chrome_path(), headless=True)
        except Exception as exc:
            pytest.skip("Chrome is unavailable: %s" % type(exc).__name__)
        try:
            page = browser.new_page()
            page.set_content(markup[start:end])
            page.add_script_tag(content=script)
            page.evaluate(r'''() => {
              const cells=Array.from({length:120},(_,i)=>({type:"cell",row_id:String(i+1),attribute:"材质",before:{value:"旧"},after:{value:"新",source:"标题",basis:`证据${i+1}`}}));
              const result={id:"P"+"a".repeat(32),requirement:"修改",kind:"归类",changed_cells:120,plan:[],warnings:[],non_cell_patches:[],unavailable_rows:[],unavailable_attributes:[]};
              const summary={ok:true,ready:true,stale:false,cat:"demo",version:"V"+"b".repeat(32),row_count:120,attributes:[{name:"材质",values:[{value:"旧",count:120}]}],issues:{numeric:[],cross:[]},history:[],export:{}};
              window.lastApply=null;
              window.Step18Panel.mount({getFile:()=>"demo",isRunning:()=>false,api:async(path,opt={})=>{
                if(path.includes("export-status"))return {status:"idle"};
                if(path.includes("std-workspace/jobs"))return {jobs:[{id:"J"+"c".repeat(32),status:"done",created_at:"2026-11-10T00:00:00Z",selection:{attributes:["材质"]},changed_cells:120}]};
                if(path.includes("preview-page")){
                  const {page,term,selection_rules}=opt.body;const selected=cells.map(()=>true);
                  const match=(p,t)=>!t||[p.row_id,p.attribute,p.before.value,p.after.value,p.after.basis].some(x=>String(x).toLowerCase().includes(t.trim().toLowerCase()));
                  for(const r of selection_rules){if(r.kind==="cell")selected[r.index]=r.selected;else cells.forEach((p,i)=>{if(match(p,r.term))selected[i]=r.selected;});}
                  const matches=cells.map((p,i)=>({p,i})).filter(x=>match(x.p,term));const pages=Math.max(1,Math.ceil(matches.length/50));const n=Math.min(page,pages-1);
                  return {result,items:matches.slice(n*50,n*50+50).map(x=>({index:x.i,patch:x.p,selected:selected[x.i],available:true})),page:n,pages,matched:matches.length,total:cells.length,selected_count:selected.filter(Boolean).length};
                }
                if(path.endsWith("/apply")){window.lastApply=opt.body;return {...summary,partial:true,history_partial:true};}
                if(path.includes("/api/std-workspace?"))return summary;
                throw Error("unexpected "+path);
              }});
              window.Step18Panel.load();
            }''')
            page.locator('[data-job-id]').wait_for()
            page.locator('[data-job-id]').click()
            page.get_by_text('已选择 120 / 120 格', exact=False).wait_for()
            assert page.locator('#s18ChangeRows tr').count() == 50
            page.locator('#s18ChangeNext').click()
            page.get_by_text('第 2 / 3 页', exact=False).wait_for()
            page.locator('#s18ChangeSearch').fill('119')
            page.get_by_text('筛选出 1 格', exact=False).wait_for()
            page.locator('#s18SelectNone').click()
            page.get_by_text('已选择 119 / 120 格', exact=False).wait_for()
            page.locator('#s18ChangeSearch').fill('')
            page.get_by_text('筛选出 120 格', exact=False).wait_for()
            page.locator('#s18Apply').click()
            page.get_by_text('已应用：119格更新', exact=False).wait_for()
            body = page.evaluate('window.lastApply')
            assert body['selection_rules'] == [{'kind': 'filter', 'term': '119', 'selected': False}]
        finally:
            browser.close()
