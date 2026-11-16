/* Live Step18 panel. The server owns every effective value and history entry. */
window.Step18Panel = (() => {
  let host, options, data = null, file = "", generation = 0, timer = null, jobPollSeq = 0, jobsSignature = null;
  let fields = new Set(), filters = new Map(), rowIds = null, proposal = null, active = false;
  let selectedChanges = null, pagedPreview = null;
  const numericGroups = new Map(), numericSearch = new Map();
  const exportingFiles = new Set();
  let loadPromise = null, loadFile = "";
  let exportTimer = null;
  function completionMessage() {
    const ready=data?.ready && !data.stale && data.export?.filename && !data.export.stale;
    return ready?"导出完成，可以继续运行步骤18。":"导出已完成，但当前版本或输入已变化，请重新导出后再运行步骤18。";
  }
  function exportIndicator(running, message) {
    const label=q("#s18OutputStatus");
    label.setAttribute("role", "status");label.setAttribute("aria-live", "polite");
    label.setAttribute("aria-busy", String(running));
    if(message)label.textContent=message;
    if(running)q("#s18Export").textContent="正在导出，请稍候…";
    else updateExportButton();
    if(running)q("#s18Download").hidden=true;
  }
  async function pollExport(selectedFile, wasRunning=false, failedMessage="") {
    clearTimeout(exportTimer);
    try {
      const state=await options.api(`/api/std-workspace/export-status?file=${encodeURIComponent(selectedFile)}`);
      if(selectedFile!==options.getFile())return;
      if(state.status==="running" || state.status==="unknown") {
        exportingFiles.add(selectedFile);exportIndicator(true,state.message||"正在导出交付，请等待完成后再运行步骤18。");controls();
        exportTimer=setTimeout(()=>pollExport(selectedFile,true,failedMessage),2000);return true;
      }
      exportingFiles.delete(selectedFile);exportIndicator(false);controls();
      if(wasRunning){
        await load();
        // ★ 2026-11-12: 优先显示**真实错误**（导出接口抛出的原因），
        //   不要再被兜底文案「操作已中断，请重新导出。」覆盖掉 ——
        //   那样用户只看得到一句笼统提示，永远不知道是"流水线在跑"还是"任务占用"。
        const code=state.status;
        const detail=state.error || failedMessage ||
          (code==="idle" ? "导出没有真正开始（可能有任务占用、流水线正在运行，或被中断）" :
           code==="interrupted" ? "导出进程已中断" : "操作未完成");
        const message=code==="complete"?completionMessage()
          :`导出未完成：${detail}${/[。.!！]$/.test(detail)?"":"。"}请重新导出。`;
        exportIndicator(false,message);notice(message,code!=="complete");
        options.toast?.(message,code==="complete"?"ok":"err");
      }
      return false;
    } catch(e) {
      if(selectedFile!==options.getFile())return;
      if(wasRunning){exportIndicator(true,"暂时无法确认导出状态，请勿启动步骤18，正在重新检查…");exportTimer=setTimeout(()=>pollExport(selectedFile,true,failedMessage),3000);return true;}
      throw e;
    }
  }
  const q = s => host.querySelector(s), qa = s => [...host.querySelectorAll(s)];
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const kinds = {import:"导入原始标注",merge:"同义归并",reannotate:"按要求重判",delete_row:"删除商品行",delete_rows_value:"按属性值删除商品",delete_attribute:"删除属性列",undo:"仅撤销某次操作",rollback:"恢复历史版本"};
  function jobTime(value) {
    const date=new Date(value || "");
    if(Number.isNaN(date.getTime()))return "时间未知";
    return new Intl.DateTimeFormat("zh-CN",{year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(date);
  }
  function timingText(t) {
    if(!t || typeof t!=="object")return "";
    const fmt=ms=>`${(Number(ms)/1000).toFixed(2)}秒`;
    const parts=[];
    if(Number.isFinite(t.prepare))parts.push(`提交准备 ${fmt(t.prepare)}`);
    if(Number.isFinite(t.processing))parts.push(`后台处理 ${fmt(t.processing)}`);
    if(Number.isFinite(t.model_cumulative))parts.push(`模型调用累计 ${fmt(t.model_cumulative)}（${t.model_calls||0}次；并发时可超过后台耗时）`);
    if(Number.isFinite(t.apply))parts.push(`应用 ${fmt(t.apply)}`);
    if(Number.isFinite(t.refresh))parts.push(`后台刷新 ${fmt(t.refresh)}`);
    return parts.join(" · ");
  }
  function jobTags(job) {
    const values=job.selection?.values || {};
    return (job.selection?.attributes || []).filter(name=>Array.isArray(values[name]) && values[name].length)
      .map(name=>`${name}：${values[name].map(v=>v===""?"（空）":String(v)).join("、")}`).join("；");
  }
  function jobRequirement(job, expanded) {
    const req=String(job.requirement || "");
    if(!req)return "<p>处理要求：未记录</p>";
    if(req.length<=120)return `<p class="s18-job-request">处理要求：${esc(req)}</p>`;
    return `<p class="s18-job-request">处理要求：${esc(req.slice(0,120))}…</p><details data-job-details="${esc(job.id)}" ${expanded.has(job.id)?"open":""}><summary>展开完整要求</summary><p class="s18-job-request">${esc(req)}</p></details>`;
  }
  function historyContent(v) {
    const selected=v.selected_attributes||[], affected=v.affected_attributes||[];
    const names=selected.length?selected:affected;
    const tags=Object.entries(v.selected_values||{}).filter(([,values])=>Array.isArray(values)&&values.length)
      .map(([name,values])=>`${name}：${values.map(x=>x===""?"（空）":x).join("、")}`).join("；");
    const text=String(v.requirement||"");
    const requirement=text.length>160?`<details><summary>查看完整处理要求</summary><p style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(text)}</p></details>`:`<p style="white-space:pre-wrap;overflow-wrap:anywhere">处理要求：${esc(text||"未记录")}</p>`;
    const changes=Object.entries(v.value_changes||{}).flatMap(([name,items])=>items.map(x=>`${name}：${x.before||"（空）"} → ${x.after||"（空）"}${x.before===x.after?"（来源或依据更新）":""} ×${x.count}`));
    return `${names.length?`<p>${selected.length?"处理属性":"涉及属性"}：${esc(names.join("、"))}</p>`:""}${tags?`<p>选中取值：${esc(tags)}</p>`:""}${requirement}${v.omitted_cells?`<p>部分应用：${v.changed_cells}格已应用，${v.omitted_cells}格未应用</p>`:""}${changes.length?`<details><summary>实际涉及的取值（${changes.length}组）</summary><p style="white-space:pre-wrap;overflow-wrap:anywhere">${changes.map(esc).join("<br>")}</p></details>`:""}`;
  }
  function notice(text, error = false) {
    q("#s18Message").hidden = false;
    q("#s18Message").classList.toggle("s18-error", error);
    q("#s18Message").textContent = text;
  }
  function invalidate() { proposal = null; selectedChanges = null; pagedPreview = null; q("#s18Preview").hidden = true; }
  function selection() { return {attributes:[...fields], values:Object.fromEntries([...filters].map(([n,v]) => [n,[...v]])), ...(rowIds ? {row_ids:rowIds} : {})}; }
  function body(extra = {}) { return {config_file:file, expected_version:data?.version, ...extra}; }
  async function post(action, extra = {}) {
    const selectedFile = file;
    const result = await options.api("/api/std-workspace/" + action, {method:"POST",body:body(extra)});
    if (selectedFile !== options.getFile()) throw new Error("所选任务已切换，本次响应已忽略");
    return result;
  }
  function controls() {
    const exporting=exportingFiles.has(file);
    qa("button,input,textarea,select").forEach(el => { el.disabled = active || exporting || el.dataset.unavailable === "true"; });
    q("#s18Reload").disabled = false;
    if (!active && !exporting) {
      q("#s18Start").disabled = !data?.ready || data.stale || !fields.size;
      q("#s18Export").disabled = !data?.ready || data.stale || !!(data.export?.filename && !data.export.stale);
      q("#s18RefreshInput").disabled = !data?.ready;
      q("#s18AuditNegative").disabled = !data?.ready || data.stale;
      q("#s18ReviewColumns").disabled = !data?.ready || data.stale || fields.size < 2;
      if(pagedPreview && (!pagedPreview.valid || (pagedPreview.total>0 && !pagedPreview.selectedCount)))q("#s18Apply").disabled=true;
    }
    updateExportButton();
  }
  function updateExportButton() {
    if(exportingFiles.has(file)) { q("#s18Export").textContent="正在导出，请稍候…"; return; }
    const exported=data?.export;
    q("#s18Export").textContent=exported?.filename ? (exported.stale ? "重新导出交付" : "当前结果已导出") : "确认当前结果并导出交付";
  }
  function selectedCount() {
    if (rowIds) return rowIds.length;
    // Marginal distributions cannot determine intersections. The server computes
    // the exact count in the preview; never add counts from different columns.
    if (filters.size === 1) {
      const [name, vals] = [...filters][0];
      return (data.attributes.find(a => a.name === name)?.values || []).filter(v=>vals.has(v.value)).reduce((n,v)=>n+v.count,0);
    }
    return filters.size ? null : data.row_count;
  }
  function renderScope() {
    const n = selectedCount(), count = n === null ? "多列筛选，预览时核对行数" : `${n}行`;
    q("#s18Scope").innerHTML = (fields.size ? `${esc([...fields].join("＋"))} · ${count}` : "尚未选择属性")
      + ([...filters].map(([name, vals]) => ` · ${esc(name)}：${esc([...vals].join("、"))}`).join(""))
      + (filters.size || rowIds ? ' <button class="btn-sm btn-ghost" id="s18Clear">清除筛选</button>' : "");
    q("#s18Clear")?.addEventListener("click", () => { filters.clear(); rowIds = null; invalidate(); render(); });
    const name = fields.size === 1 ? [...fields][0] : "", vals = filters.get(name);
    q("#s18MergeSelected").hidden = !(vals && vals.size >= 2);
    if (vals) q("#s18MergeSelected").textContent = `归并选中的 ${vals.size} 个值（可改统一名称）`;
  }
  function render(onlyNames = null) {
    const query=q("#s18Find").value.trim().toLocaleLowerCase();
    const rowsScroll=q("#s18Rows").scrollTop;
    const valueScroll=new Map([...q("#s18Rows").querySelectorAll("[data-numeric-values]")].map(el=>[el.dataset.numericValues,el.scrollTop]));
    const openDetails=new Map([...q("#s18Rows").querySelectorAll("[data-s18-attribute]")].map(el=>[el.dataset.s18Attribute,[...el.querySelectorAll("details")].map(d=>d.open)]));
    const visibleAttrs=(data?.attributes || []).filter(attr=>!query||attr.name.toLocaleLowerCase().includes(query)||(attr.values||[]).some(v=>v.value.toLocaleLowerCase().includes(query)));
    const attributeMarkup = attr => {
      const items = attr.values || [], visible = items.slice(0, 32), rest = items.slice(32);
      const values = (list,term="") => list.map(v => `<button type="button" class="s18-value" data-name="${esc(attr.name)}" data-value="${esc(v.value)}" aria-pressed="${filters.get(attr.name)?.has(v.value) || false}" ${term&&!v.value.toLocaleLowerCase().includes(term)?"hidden":""}>${esc(v.value || "(空)")} ×${v.count}</button>`).join("");
      const numeric=(data.issues?.numeric||[]).filter(x=>x.属性===attr.name), cross=(data.issues?.cross||[]).filter(x=>x.左属性===attr.name||x.右属性===attr.name);
      const isNumeric=attr.rule==="numeric";
      const groups=new Map();
      if(isNumeric)items.forEach(v=>{const group=v.unit_group||"其他／无单位";if(!groups.has(group))groups.set(group,[]);groups.get(group).push(v);});
      const group=numericGroups.get(attr.name), activeGroup=groups.has(group)?group:null;
      const groupItems=activeGroup?groups.get(activeGroup):items;
      const search=(numericSearch.get(attr.name)||"").toLocaleLowerCase();
      const matching=groupItems.filter(v=>v.value.toLocaleLowerCase().includes(search));
      const chosen=filters.get(attr.name)||new Set();
      const groupChips=[...groups].map(([g,list])=>`<button type="button" class="s18-unit-chip" data-unit-filter-attr="${esc(attr.name)}" data-unit-filter="${esc(g)}" aria-pressed="${activeGroup===g}">${esc(g)} <span>${list.length}</span></button>`).join("");
      const groupMarkup=`<div class="s18-numeric-picker"><div class="s18-unit-filters" aria-label="按单位筛选"><button type="button" class="s18-unit-chip" data-unit-filter-attr="${esc(attr.name)}" data-unit-filter="" aria-pressed="${activeGroup===null}">全部 <span>${items.length}</span></button>${groupChips}</div><div class="s18-numeric-tools"><label class="s18-visually-hidden" for="numeric-search-${esc(attr.name)}">搜索${esc(attr.name)}取值</label><input id="numeric-search-${esc(attr.name)}" type="search" data-numeric-search="${esc(attr.name)}" value="${esc(numericSearch.get(attr.name)||"")}" placeholder="搜索取值"><button type="button" class="btn-sm btn-ghost" title="选择当前单位下全部取值，不受搜索影响" data-group-attr="${esc(attr.name)}" data-group-key="${esc(activeGroup||"")}">${groupItems.length && groupItems.every(v=>chosen.has(v.value))?"取消本组":"选择本组"}</button></div><div class="s18-numeric-count">${esc(activeGroup||"全部单位")} · ${groupItems.length}种取值${chosen.size?` · 已选${chosen.size}种`:""}</div><div class="s18-values s18-numeric-values" data-numeric-values="${esc(attr.name)}">${values(groupItems,search)}</div><p class="s18-numeric-empty" ${matching.length?"hidden":""}>没有匹配的取值</p></div>`;
      const tags=isNumeric?`<div class="s18-numeric-heading"><span>${items.length}种取值${attr.unit?` · 目标单位 ${esc(attr.unit)}`:""}${chosen.size?` · 已选 ${chosen.size}种`:""}</span><button class="btn-sm btn-ghost" data-unit-attr="${esc(attr.name)}">统一单位</button></div><details class="s18-numeric-details" ${chosen.size?"open":""}><summary>查看和选择取值</summary>${groupMarkup}</details>`:`<div class="s18-values">${values(visible)}</div>${rest.length?`<details><summary>更多取值（${rest.length}）</summary><div class="s18-values">${values(rest)}</div></details>`:""}`;
      const selected=filters.get(attr.name);
      const deleteControl=selected?.size?`<p class="s18-help"><button type="button" class="btn-sm btn-ghost" data-delete-value-attr="${esc(attr.name)}">删除所选 ${selected.size} 个取值对应的商品</button> <span data-delete-value-message role="status"></span></p>`:"";
      return `<div class="s18-attr" data-s18-attribute="${esc(attr.name)}"><div class="s18-row s18-between"><label><input type="checkbox" data-attr="${esc(attr.name)}" ${fields.has(attr.name)?"checked":""}> ${esc(attr.name)} ${data.changed_attributes?.[attr.name]?`<span class="s18-changed">本次更新 ${data.changed_attributes[attr.name]} 格</span>`:""}</label><span class="s18-row">${numeric.length?`<button class="btn-sm btn-ghost" data-issue-attr="${esc(attr.name)}" data-issue-kind="numeric">数值待核${numeric.length}处</button>`:""}${cross.length?`<button class="btn-sm btn-ghost" data-issue-attr="${esc(attr.name)}" data-issue-kind="cross">跨列问题${cross.length}处</button>`:""}<button type="button" class="btn-sm btn-ghost" data-delete-attribute="${esc(attr.name)}" aria-label="删除属性 ${esc(attr.name)}">删除属性</button></span></div>${tags}${deleteControl}</div>`;
    };
    const rows=q("#s18Rows"), empty=`<div class="s18-loading">${data?.ready?"没有匹配的属性或取值，已选范围保持不变。":"尚无可用输入，请先完成步骤16确认与步骤17标注。"}</div>`;
    if(!onlyNames) rows.innerHTML=visibleAttrs.map(attributeMarkup).join("") || empty;
    else {
      const scrollTop=rows.scrollTop;
      const visible=new Map(visibleAttrs.map(a=>[a.name,a]));
      for(const name of onlyNames){
        const old=[...rows.querySelectorAll("[data-s18-attribute]")].find(el=>el.dataset.s18Attribute===name);
        const attr=visible.get(name);
        if(!attr){old?.remove();continue;}
        const template=document.createElement("template");template.innerHTML=attributeMarkup(attr);
        const updated=template.content.firstElementChild;
        if(old){
          const oldDetails=[...old.querySelectorAll("details")],newDetails=[...updated.querySelectorAll("details")];
          oldDetails.forEach((d,i)=>{if(d.open && newDetails[i])newDetails[i].open=true;});
          old.replaceWith(updated);
        } else {
          rows.querySelector(".s18-loading")?.remove();
          const position=visibleAttrs.findIndex(a=>a.name===name);
          const after=visibleAttrs.slice(position+1).map(a=>[...rows.querySelectorAll("[data-s18-attribute]")].find(el=>el.dataset.s18Attribute===a.name)).find(Boolean);
          rows.insertBefore(updated,after || null);
        }
      }
      if(!rows.querySelector("[data-s18-attribute]"))rows.innerHTML=empty;
      rows.scrollTop=scrollTop;
    }
    if(!onlyNames)[...rows.querySelectorAll("[data-s18-attribute]")].forEach(el=>{const saved=openDetails.get(el.dataset.s18Attribute);if(saved)[...el.querySelectorAll("details")].forEach((d,i)=>{if(saved[i])d.open=true;});});
    rows.scrollTop=rowsScroll;
    rows.querySelectorAll("[data-numeric-values]").forEach(el=>{if(valueScroll.has(el.dataset.numericValues))el.scrollTop=valueScroll.get(el.dataset.numericValues);});
    qa("[data-attr]").forEach(el => el.onchange = () => { if(el.checked) fields.add(el.dataset.attr); else {fields.delete(el.dataset.attr); filters.delete(el.dataset.attr);} invalidate();render([el.dataset.attr]); });
    qa("[data-unit-filter-attr]").forEach(el=>el.onclick=()=>{const name=el.dataset.unitFilterAttr; if(el.dataset.unitFilter)numericGroups.set(name,el.dataset.unitFilter);else numericGroups.delete(name);render([name]);});
    qa("[data-numeric-search]").forEach(el=>el.oninput=()=>{const name=el.dataset.numericSearch,term=el.value.toLocaleLowerCase();numericSearch.set(name,el.value);const section=el.closest(".s18-numeric-picker");let count=0;section.querySelectorAll("[data-value]").forEach(button=>{button.hidden=!button.dataset.value.toLocaleLowerCase().includes(term);if(!button.hidden)count++;});section.querySelector(".s18-numeric-empty").hidden=count>0;});
    qa("[data-group-attr]").forEach(el=>el.onclick=()=>{
      const name=el.dataset.groupAttr, group=el.dataset.groupKey;
      const items=(data.attributes.find(a=>a.name===name)?.values||[]).filter(v=>!group||(v.unit_group||"其他／无单位")===group);
      const vals=filters.get(name)||new Set(), all=items.every(v=>vals.has(v.value));
      items.forEach(v=>all?vals.delete(v.value):vals.add(v.value));
      if(vals.size){filters.set(name,vals);fields.add(name);}else{filters.delete(name);fields.delete(name);}
      rowIds=null;invalidate();render([name]);
    });
    qa("[data-name]").forEach(el => el.onclick = () => {
      const {name,value} = el.dataset, vals = filters.get(name) || new Set();
      fields.add(name); rowIds=null;
      if(vals.has(value)) vals.delete(value); else vals.add(value);
      if(vals.size) filters.set(name,vals); else filters.delete(name);
      invalidate();render([name]);
    });
    qa("[data-delete-value-attr]").forEach(el=>el.onclick=()=>deleteValueRows(el.dataset.deleteValueAttr));
    qa("[data-delete-attribute]").forEach(el=>el.onclick=()=>deleteAttribute(el.dataset.deleteAttribute));
    qa("[data-unit-attr]").forEach(el=>el.onclick=()=>{
      const name=el.dataset.unitAttr, unit=data.attributes.find(a=>a.name===name)?.unit;
      fields=new Set([name]);filters.clear();rowIds=null;
      q("#s18Request").value=unit?`将${name}统一为${unit}，根据商品原文换算，不猜测；证据不足时保留原值并说明。`:`将${name}统一为【填写目标单位】，根据商品原文换算；证据不足时保留原值并说明。`;
      invalidate();render();q("#s18Request").focus();
    });
    qa("[data-issue-attr]").forEach(el=>el.onclick=()=>{
      const name=el.dataset.issueAttr, numeric=el.dataset.issueKind==="numeric";
      const hits=(numeric?data.issues.numeric:data.issues.cross).filter(x=>numeric?x.属性===name:x.左属性===name||x.右属性===name);
      fields=new Set(numeric?[name]:hits.flatMap(x=>[x.左属性,x.右属性])); filters.clear();
      rowIds=[...new Set(hits.map(x=>String(numeric?x.行:x.行号)))];
      q("#s18Request").value=numeric?"核查这些数值问题，按商品原文与属性单位纠正；证据不足时保留原值并说明。":"联合复核所选属性的跨列冲突，只按同一商品证据纠正，无法判断时保留原值并说明。";
      invalidate();render();
    });
    renderScope();
    q("#stdStatusHint").textContent = data?.ready ? `${data.row_count}件商品 · ${data.attributes.length}个属性 · 当前版本 ${data.version.slice(0,9)}${data.stale?" · 上游已变化":""}` : "等待步骤17输入";
    const exported = data?.export;
    if(!exportingFiles.has(file))q("#s18OutputStatus").textContent = exported?.filename ? (exported.stale?(exported.exists===false?"交付文件不存在，请重新导出。":"已有交付与当前版本不一致，请重新导出。"): `已导出当前版本，可继续运行步骤18：${exported.filename}`) : "尚未导出当前版本。";
    q("#s18Download").hidden = !exported?.filename || exported.stale;
    if(exported?.filename) q("#s18Download").href = `/api/download?file=${encodeURIComponent(file)}&step=18&name=${encodeURIComponent("18_标准化/"+exported.filename)}`;
    else q("#s18Download").removeAttribute("href");
    q("#s18History").innerHTML = (data?.history || []).map(v => `<div class="s18-history-item"><div class="s18-row s18-between"><strong>${esc(kinds[v.kind] || v.kind)} · ${esc(v.version.slice(0,9))} · ${v.changed_cells}格${v.changed_rows?` · ${v.changed_rows}行`:""}${v.changed_columns?` · ${v.changed_columns}列`:""}</strong><span class="s18-row">${v.can_undo?`<button class="btn-sm btn-ghost" data-restore="${esc(v.version)}" data-selective="yes">仅撤销本次</button>`:""}${v.parent?`<button class="btn-sm btn-ghost" data-restore="${esc(v.version)}" data-selective="no">整体回到此操作前</button>`:""}</span></div><p>${esc(jobTime(v.created_at))}</p>${historyContent(v)}</div>`).join("");
    qa("[data-restore]").forEach(el => el.onclick=()=>restore(el.dataset.restore,el.dataset.selective==="yes"));
    const cross = data?.issues?.cross || [];
    q("#s18Issues").textContent = cross.length ? `检测到${cross.length}条跨列问题，可选择相关属性后输入纠正要求。` : "未明确与词表外值保留；多选属性可联合核对。";
    controls();
  }
  function showProposal(result) {
    pagedPreview = null;
    proposal = result;
    const patches=result.patches || [], cells=patches.filter(p=>p.type==="cell"), defs=patches.filter(p=>p.type==="definition"), rows=patches.filter(p=>p.type==="row"), columns=patches.filter(p=>p.type==="attribute");
    const selective=cells.length>0 && !["undo","rollback"].includes(result.kind);
    const unavailable=new Set(result.unavailable_rows || []);
    const missingAttributes=new Set(result.unavailable_attributes || []);
    const unavailableCell=p=>unavailable.has(p.row_id)||missingAttributes.has(p.attribute);
    selectedChanges=selective?new Set(cells.map((p,i)=>unavailableCell(p)?null:i).filter(i=>i!==null)):null;
    q("#s18Preview").hidden=false;
    const plan=result.plan || [];
    const planDetails=plan.map((op,i)=>`<li>${esc(op.type)}${op.method?` · ${esc(op.method)}`:""}${op.unit?` · 目标单位 ${esc(op.unit)}`:""}：${Object.entries(op.counts||{}).map(([name,c])=>`${esc(name)} ${c.matched}格命中，${c.changed}格修改，${c.unchanged}格未改`).join("；")}${op.requirement?`<br><span class="s18-help">要求：${esc(op.requirement)}</span>`:""}${op.mappings?`<br><span class="s18-help">${esc(Object.entries(op.mappings).flatMap(([name,items])=>Object.entries(items).map(([from,to])=>`${name}：${from || "空值"} → ${to}`)).join("；"))}</span>`:""}</li>`).join("");
    const scope=Object.entries(result.scope_counts||{}).map(([name,count])=>`${esc(name)} ${count}格`).join(" · ");
    q("#s18PreviewContent").innerHTML = `<p>${esc(result.requirement)}</p><p class="s18-help">${cells.length}格实际修改 · ${defs.length}项词表/映射调整${result.reviewed_rows!==undefined?` · 已选择${result.reviewed_rows}行`:""}</p>`
      + (scope?`<p class="s18-help">选中范围：${scope}</p>`:"")
      + (planDetails?`<details open><summary>识别的处理计划（${plan.length}项）</summary><ol>${planDetails}</ol></details>`:"")
      + (unavailable.size?`<p class="s18-help">有 ${unavailable.size} 个商品行已删除，涉及的修改已自动取消选择，不会应用。其他冲突仍需重新预览。</p>`:"")
      + (missingAttributes.size?`<p class="s18-help">属性已删除：${esc([...missingAttributes].join("、"))}。相关修改已自动取消选择，其他属性仍可应用。</p>`:"")
      + (rows.length?`<p>商品行变更：${rows.map(p=>`${p.after?"恢复":"删除"}第 ${esc(p.row_id)} 行（全部属性）`).join("；")}</p>`:"")
      + (columns.length?`<p>属性列变更：${columns.map(p=>`${p.after?"恢复":"删除"} ${esc(p.attribute)}`).join("；")}</p>`:"")
      + (cells.length?`<div id="s18ChangeControls">${selective?'<p id="s18SelectedCount" class="s18-help" aria-live="polite"></p><div class="s18-row"><label>查找行号、属性或取值 <input id="s18ChangeSearch" type="search" placeholder="例如 593"></label><button type="button" class="btn-sm btn-ghost" id="s18SelectAll">全选筛选结果</button><button type="button" class="btn-sm btn-ghost" id="s18SelectNone">取消筛选结果</button></div>':'<p class="s18-help">回滚操作需要整体应用。</p>'}</div><div class="s18-table-wrap"><table class="s18-table"><thead><tr>${selective?'<th>应用</th>':""}<th>商品行 / 属性</th><th>修改前 → 后</th><th>来源与依据</th></tr></thead><tbody id="s18ChangeRows"></tbody></table></div><div class="s18-row"><button type="button" class="btn-sm btn-ghost" id="s18ChangePrev">上一页</button><span id="s18ChangePage" class="s18-help"></span><button type="button" class="btn-sm btn-ghost" id="s18ChangeNext">下一页</button></div>`:rows.length||columns.length?'':'<p class="s18-help">本次未发现需要改写的值；确认后仍会记录复核过程。</p>')
      + (defs.length?`<details><summary>词表 / 同义映射调整（${defs.length}项）</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(JSON.stringify(defs,null,2))}</pre></details>`:"")
      + ((result.warnings || []).length?`<details><summary>待复核说明（${result.warnings.length}项，点条目看依据与原文）</summary>${result.warnings.slice(0,40).map(w=>{const detail=[w.proposed_value?`模型给出的值：${esc(w.proposed_value)}`:"",w.basis?`模型写的依据：${esc(w.basis)}`:"",w.claimed_source?`它自称的来源：${esc(w.claimed_source)}`:"",w.source_text?`该字段原文：${esc(w.source_text)}`:""].filter(Boolean).join("<br>");return `<details class="s18-warn"><summary class="s18-help">${esc(w.row_id)} · ${esc(w.attribute)}：${esc(w.reason)}</summary><div class="s18-help" style="padding:6px 0 8px 14px">${detail||"没有更多细节"}</div></details>`;}).join("")}</details>`:"");
    if(cells.length){
      let page=0; const pageSize=50;
      const filtered=()=>{const term=(q("#s18ChangeSearch")?.value||"").trim().toLowerCase();return cells.map((p,i)=>({p,i})).filter(({p})=>!term||[p.row_id,p.attribute,p.before.value,p.after.value,p.after.basis].some(v=>String(v??"").toLowerCase().includes(term)));};
      const draw=()=>{const rows=filtered(), pages=Math.max(1,Math.ceil(rows.length/pageSize));page=Math.min(page,pages-1);
        q("#s18ChangeRows").innerHTML=rows.slice(page*pageSize,(page+1)*pageSize).map(({p,i})=>`<tr>${selective?`<td><input type="checkbox" data-change-index="${i}" data-unavailable="${unavailableCell(p)}" aria-label="应用第 ${esc(p.row_id)} 行 ${esc(p.attribute)}" ${selectedChanges.has(i)?"checked":""} ${unavailableCell(p)?"disabled":""}></td>`:""}<td>${esc(p.row_id)} · ${esc(p.attribute)}${unavailableCell(p)?`<br><span class="s18-help">${missingAttributes.has(p.attribute)?"属性已删除":"商品行已删除"}，不应用</span>`:selective?`<br><button type="button" class="btn-sm btn-ghost" data-delete-row="${esc(p.row_id)}">删除整行</button>`:""}</td><td>${esc(p.before.value)} → ${esc(p.after.value)}${p.before.value===p.after.value?"（来源或依据更新）":""}</td><td>${esc(p.after.source)}<br>${esc(p.after.basis)}</td></tr>`).join("") || `<tr><td colspan="${selective?4:3}">没有匹配的修改</td></tr>`;
        q("#s18ChangePage").textContent=`第 ${page+1} / ${pages} 页 · 筛选出 ${rows.length} 格`;q("#s18ChangePrev").disabled=page===0;q("#s18ChangeNext").disabled=page>=pages-1;
        if(selective){q("#s18SelectedCount").textContent=`已选择 ${selectedChanges.size} / ${cells.length} 格；未显示的已选项也会应用。部分应用时词表按实际结果更新。`;qa("[data-change-index]").forEach(el=>el.onchange=()=>{const i=Number(el.dataset.changeIndex);el.checked?selectedChanges.add(i):selectedChanges.delete(i);draw();});qa("[data-delete-row]").forEach(el=>el.onclick=()=>deleteRow(el.dataset.deleteRow));}
      };
      q("#s18ChangePrev").onclick=()=>{page--;draw();};q("#s18ChangeNext").onclick=()=>{page++;draw();};
      if(selective){q("#s18ChangeSearch").oninput=()=>{page=0;draw();};q("#s18SelectAll").onclick=()=>{filtered().forEach(({p,i})=>{if(!unavailableCell(p))selectedChanges.add(i);});draw();};q("#s18SelectNone").onclick=()=>{filtered().forEach(({i})=>selectedChanges.delete(i));draw();};}
      draw();
    }
  }
  async function showPagedJob(jobId) {
    const first=await post("preview-page",{job_id:jobId,page:0,term:"",selection_rules:[]});
    const result=first.result, noncells=result.non_cell_patches||[];
    // Structural rollback/delete previews retain the existing complete flow.
    if(noncells.some(p=>p.type==="row" || p.type==="attribute")) {
      const job=await options.api(`/api/std-workspace/job?file=${encodeURIComponent(file)}&id=${encodeURIComponent(jobId)}`);
      if(job.status!=="done")throw new Error("任务状态已变化，请重新加载");
      showProposal(job.result);return;
    }
    const state={jobId, rules:[], page:0, term:"", seq:0, valid:true, total:first.total, selectedCount:first.selected_count, timer:null};
    pagedPreview=state;proposal=result;selectedChanges=null;
    q("#s18Preview").hidden=false;
    const plan=(result.plan||[]).map(op=>`<li>${esc(op.type)}${op.method?` · ${esc(op.method)}`:""}${op.unit?` · 目标单位 ${esc(op.unit)}`:""}：${Object.entries(op.counts||{}).map(([name,c])=>`${esc(name)} ${c.matched}格命中，${c.changed}格修改，${c.unchanged}格未改`).join("；")}${op.requirement?`<br><span class="s18-help">要求：${esc(op.requirement)}</span>`:""}${op.mappings?`<br><span class="s18-help">${esc(Object.entries(op.mappings).flatMap(([name,items])=>Object.entries(items).map(([from,to])=>`${name}：${from||"空值"} → ${to}`)).join("；"))}</span>`:""}</li>`).join("");
    const scope=Object.entries(result.scope_counts||{}).map(([name,count])=>`${esc(name)} ${count}格`).join(" · ");
    const defs=noncells.filter(p=>p.type==="definition");
    const warnings=(result.warnings||[]).map(w=>{const detail=[w.proposed_value?`模型给出的值：${esc(w.proposed_value)}`:"",w.basis?`模型写的依据：${esc(w.basis)}`:"",w.claimed_source?`它自称的来源：${esc(w.claimed_source)}`:"",w.source_text?`该字段原文：${esc(w.source_text)}`:""].filter(Boolean).join("<br>");return `<details class="s18-warn"><summary class="s18-help">${esc(w.row_id)} · ${esc(w.attribute)}：${esc(w.reason)}</summary><div class="s18-help">${detail||"没有更多细节"}</div></details>`;}).join("");
    q("#s18PreviewContent").innerHTML=`<p>${esc(result.requirement)}</p><p class="s18-help">${first.total}格实际修改 · ${defs.length}项词表/映射调整${result.reviewed_rows!==undefined?` · 已选择${result.reviewed_rows}行`:""}</p>`
      +(scope?`<p class="s18-help">选中范围：${scope}</p>`:"")
      +(plan?`<details open><summary>识别的处理计划（${(result.plan||[]).length}项）</summary><ol>${plan}</ol></details>`:"")
      +`<p id="s18Unavailable" class="s18-help"></p>`
      +(first.total?`<p id="s18SelectedCount" class="s18-help" aria-live="polite"></p><div class="s18-row"><label>查找行号、属性或取值 <input id="s18ChangeSearch" type="search" placeholder="例如 593"></label><button type="button" class="btn-sm btn-ghost" id="s18SelectAll">全选筛选结果</button><button type="button" class="btn-sm btn-ghost" id="s18SelectNone">取消筛选结果</button></div><div class="s18-table-wrap"><table class="s18-table"><thead><tr><th>应用</th><th>商品行 / 属性</th><th>修改前 → 后</th><th>来源与依据</th></tr></thead><tbody id="s18ChangeRows"></tbody></table></div><div class="s18-row"><button type="button" class="btn-sm btn-ghost" id="s18ChangePrev">上一页</button><span id="s18ChangePage" class="s18-help"></span><button type="button" class="btn-sm btn-ghost" id="s18ChangeNext">下一页</button></div>`:'<p class="s18-help">本次未发现需要改写的值；确认后仍会记录复核过程。</p>')
      +(defs.length?`<details><summary>词表 / 同义映射调整（${defs.length}项）</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(JSON.stringify(defs,null,2))}</pre></details>`:"")
      +(result.warning_count?`<details><summary>待复核说明（${result.warning_count}项，显示前40项）</summary>${warnings}</details>`:"");
    function draw(response) {
      if(pagedPreview!==state)return;
      state.page=response.page;state.total=response.total;state.selectedCount=response.selected_count;state.valid=true;
      const unavailable=response.result.unavailable_rows||[], missing=response.result.unavailable_attributes||[];
      q("#s18Unavailable").textContent=[unavailable.length?`${unavailable.length}个商品行已删除，相关修改不应用。`:"",missing.length?`属性已删除：${missing.join("、")}。`:""].filter(Boolean).join(" ");
      if(response.total){
        q("#s18SelectedCount").textContent=`已选择 ${response.selected_count} / ${response.total} 格；未显示的已选项也会应用。部分应用时词表按实际结果更新。`;
        q("#s18ChangeRows").innerHTML=response.items.map(({index,patch:p,selected,available})=>`<tr><td><input type="checkbox" data-page-index="${index}" data-unavailable="${!available}" aria-label="应用第 ${esc(p.row_id)} 行 ${esc(p.attribute)}" ${selected?"checked":""} ${available?"":"disabled"}></td><td>${esc(p.row_id)} · ${esc(p.attribute)}${available?`<br><button type="button" class="btn-sm btn-ghost" data-delete-row="${esc(p.row_id)}">删除整行</button>`:'<br><span class="s18-help">商品行或属性已删除，不应用</span>'}</td><td>${esc(p.before.value)} → ${esc(p.after.value)}${p.before.value===p.after.value?"（来源或依据更新）":""}</td><td>${esc(p.after.source)}<br>${esc(p.after.basis)}</td></tr>`).join("")||'<tr><td colspan="4">没有匹配的修改</td></tr>';
        q("#s18ChangePage").textContent=`第 ${response.page+1} / ${response.pages} 页 · 筛选出 ${response.matched} 格`;
        q("#s18ChangePrev").dataset.unavailable=String(response.page===0);q("#s18ChangeNext").dataset.unavailable=String(response.page>=response.pages-1);
        qa("[data-page-index]").forEach(el=>el.onchange=()=>{state.rules.push({kind:"cell",index:Number(el.dataset.pageIndex),selected:el.checked});refresh();});
        qa("[data-delete-row]").forEach(el=>el.onclick=()=>deleteRow(el.dataset.deleteRow));
      }
      controls();
    }
    async function refresh() {
      const seq=++state.seq;state.valid=false;controls();
      try {
        const response=await post("preview-page",{job_id:jobId,page:state.page,term:state.term,selection_rules:state.rules});
        if(pagedPreview===state && seq===state.seq)draw(response);
      } catch(e) { if(pagedPreview===state && seq===state.seq){state.valid=false;controls();notice(e.message,true);} }
    }
    if(first.total){
      q("#s18ChangePrev").onclick=()=>{state.page=Math.max(0,state.page-1);refresh();};
      q("#s18ChangeNext").onclick=()=>{state.page++;refresh();};
      q("#s18ChangeSearch").oninput=e=>{state.term=e.target.value;state.page=0;state.valid=false;state.seq++;controls();clearTimeout(state.timer);state.timer=setTimeout(refresh,200);};
      q("#s18SelectAll").onclick=()=>{state.rules.push({kind:"filter",term:state.term,selected:true});refresh();};
      q("#s18SelectNone").onclick=()=>{state.rules.push({kind:"filter",term:state.term,selected:false});refresh();};
    }
    draw(first);
  }
  function resumeProposal(result) {
    if(result.selection){
      fields=new Set(result.selection.attributes||[]);
      filters=new Map(Object.entries(result.selection.values||{}).map(([name,vals])=>[name,new Set(vals)]));
      rowIds=result.selection.row_ids||null;
      q("#s18Request").value=result.requirement||"";
      q("#s18Source").value=result.mode||"auto";
      render();
    }
    showProposal(result);
  }
  function mergePartialResult(result) {
    const previous=data, newAttrs=new Map((result.attributes||[]).map(a=>[a.name,a]));
    const changed=new Set(newAttrs.keys());
    const removedFilters=[];
    const scope=new Set(result.issues?.scope||[]);
    const issues=Array.isArray(result.issues?.scope)?{
      ...result.issues,
      numeric:[...(previous.issues?.numeric||[]).filter(x=>!scope.has(x.属性)),...(result.issues.numeric||[])],
      cross:[...(previous.issues?.cross||[]).filter(x=>!scope.has(x.左属性)&&!scope.has(x.右属性)),...(result.issues.cross||[])]
    }:result.issues;
    const issueFor=(issues,name)=>JSON.stringify({
      numeric:(issues?.numeric||[]).filter(x=>x.属性===name),
      cross:(issues?.cross||[]).filter(x=>x.左属性===name||x.右属性===name)
    });
    for(const attr of previous.attributes)if(issueFor(previous.issues,attr.name)!==issueFor(issues,attr.name))changed.add(attr.name);
    for(const [name,attr] of newAttrs){
      const selected=filters.get(name);
      if(selected){
        const allowed=new Set((attr.values||[]).map(v=>v.value));
        const kept=new Set([...selected].filter(v=>allowed.has(v)));
        if(kept.size!==selected.size)removedFilters.push(name);
        if(kept.size)filters.set(name,kept);else {filters.delete(name);fields.delete(name);}
      }
    }
    data={...previous,...result,issues,history:result.history_partial?[...(result.history||[]),...(previous.history||[])]:result.history,
          attributes:previous.attributes.map(a=>newAttrs.get(a.name)||a)};
    qa(".s18-changed").forEach(el=>el.remove());
    render(changed);
    return removedFilters;
  }
  async function deleteAttribute(name) {
    if(!data?.ready || data.stale || active){notice("请先加载当前步骤18数据。",true);return;}
    if(!confirm(`确定删除属性「${name}」？\n\n步骤18当前结果及导出将移除该列和全部 ${data.row_count} 行的属性值、来源与依据。其他属性保留；可在操作记录中撤销。步骤16、17原始数据不变。`))return;
    const retainedFilters=new Map([...filters].filter(([n])=>n!==name));
    const retainedRows=rowIds;
    active=true;controls();
    try{
      await post("delete-attribute",{attribute:name});
      fields.delete(name);invalidate();await load();
      filters=retainedFilters;rowIds=retainedRows;render();
      notice(`已删除属性「${name}」，旧任务中的该属性不会默认应用；可在操作记录中撤销。`);
    }catch(e){notice(e.message,true);}finally{active=false;controls();}
  }
  async function deleteValueRows(attribute) {
    if(!data?.ready || data.stale || active){notice("请先加载当前步骤18数据。",true);return;}
    const selectedValues=[...(filters.get(attribute)||[])];
    if(!selectedValues.length){notice("请先选择要删除的属性值。",true);return;}
    const msg=[...qa("[data-delete-value-attr]")].find(el=>el.dataset.deleteValueAttr===attribute)?.parentElement?.querySelector("[data-delete-value-message]");
    if(msg)msg.textContent="正在核对选中商品…";
    active=true;controls();
    try{
      const info=await post("value-rows-info",{attribute,values:selectedValues});
      if(!info.count)throw new Error("该属性值没有对应商品，请重新加载。");
      if(info.count===info.total)throw new Error("此操作会删除全部商品；至少需要保留一行。");
      const examples=(info.examples||[]).map(r=>`第 ${r.row_id} 行：${r.title||"（无标题）"}`).join("\n");
      const label=selectedValues.map(v=>v||"（空）").join("、");
      if(!confirm(`确定删除「${attribute}」中任一所选取值对应的全部 ${info.count} 行商品？\n所选 ${selectedValues.length} 个取值：${label}\n\n${examples}${info.count>5?"\n…":""}\n\n每行的所有属性和值都会从步骤18当前结果及导出中移除；此操作不受其他属性筛选影响，可在操作记录中撤销。步骤17原始数据保留。`))return;
      await post("delete-value-rows",{attribute,values:selectedValues});
      filters.delete(attribute);rowIds=null;invalidate();await load();
      notice(`已删除「${attribute}：${label}」对应的 ${info.count} 行商品，可在操作记录中撤销；请重新导出。`);
    }catch(e){
      const message=e.message==="属性或取值格式无效" && selectedValues.length>1
        ? "后台可能仍是旧版，请重启网页服务并刷新页面后重试；本次未删除。" : e.message;
      if(msg)msg.textContent=message;
      notice(message,true);options.toast?.(message,"err");
    }finally{if(msg && msg.textContent==="正在核对选中商品…")msg.textContent="";active=false;controls();}
  }
  async function deleteRow(rowId) {
    rowId=String(rowId||"").trim();
    if(!/^\d+$/.test(rowId)){notice("请输入有效的商品行号。",true);return;}
    if(!data?.ready || data.stale || active){notice("请先加载当前步骤18数据。",true);return;}
    active=true;controls();
    try{
      const info=await post("row-info",{row_id:rowId});
      if(!confirm(`删除第 ${info.row_id} 行商品？\n${info.title||"（无标题）"}${info.asin?`\nASIN：${info.asin}`:""}\n该行的 ${info.attribute_count} 个属性和资料将从步骤18当前结果及导出移除，可在操作记录中撤销。步骤17不受影响。`))return;
      await post("delete-row",{row_id:rowId});
      invalidate();await load();
      notice(`已删除第 ${rowId} 行商品及全部属性；可在操作记录中撤销，请重新导出交付。`);
    }catch(e){notice(e.message,true);}finally{active=false;controls();}
  }
  async function restore(version, selective) {
    invalidate();active=true;controls();
    try {
      const result=await post("restore-preview",{version,selective});
      if(result.blocked) notice("后续操作修改过相同内容，整次撤销已暂停：\n"+result.conflicts.map(c=>`${c.row_id || "规则"} · ${c.attribute}`).join("\n"),true);
      else showProposal(result);
    } catch(e) {notice(e.message,true);} finally {active=false;controls();}
  }
  function startJobPolling(epoch) { clearTimeout(timer); const seq=++jobPollSeq; pollJobs(epoch,seq); }
  async function pollJobs(epoch, seq) {
    if(epoch!==generation || seq!==jobPollSeq || file!==options.getFile())return;
    try {
      const result=await options.api(`/api/std-workspace/jobs?file=${encodeURIComponent(file)}`);
      if(epoch!==generation || seq!==jobPollSeq)return;
      const labels={pending:"等待处理",running:"处理中",cancelling:"正在取消",cancelled:"已取消",done:"预览已完成 · 未应用",failed:"处理失败",interrupted:"已中断",applied:"已应用"};
      const jobs=[...(result.jobs||[])].sort((a,b)=>{
        const at=Date.parse(a.created_at)||0, bt=Date.parse(b.created_at)||0;
        return bt-at || String(b.created_at||"").localeCompare(String(a.created_at||""));
      });
      // The badge follows the most recently submitted preview, not progress,
      // completion time, or a separate latest item for each attribute.
      const newest=jobs.find(j=>j.created_at && !Number.isNaN(Date.parse(j.created_at)));
      const signature=JSON.stringify(result.jobs||[]);
      if(signature!==jobsSignature){
        const panel=q("#s18Jobs"), scrollTop=panel.scrollTop;
        const expanded=new Set([...panel.querySelectorAll("details[data-job-details][open]")].map(el=>el.dataset.jobDetails));
        panel.innerHTML=jobs.map(j=>`<div class="s18-job ${j.status==="done"?"is-unapplied":j.status==="applied"?"is-applied":j.status==="failed"?"is-failed":""}"><div class="s18-row s18-between"><strong>${esc((j.selection?.attributes||[]).join("＋")||"处理任务")} · ${esc(labels[j.status]||j.status)}${newest && j.id===newest.id?'<span class="s18-job-latest">最新</span>':""}</strong><span class="s18-row">${j.status==="done"?`<button class="btn-sm btn-ghost" data-job-id="${esc(j.id)}">查看预览${j.changed_cells===null||j.changed_cells===undefined?"":`（${j.changed_cells}格）`}</button>`:""}${["pending","running"].includes(j.status)?`<button type="button" class="btn-sm btn-ghost" data-cancel-job="${esc(j.id)}">取消</button>`:""}${["done","failed","interrupted","cancelled","applied"].includes(j.status)?`<button type="button" class="btn-sm btn-ghost" data-retry-job="${esc(j.id)}">重新执行</button>`:""}</span></div><p class="s18-job-time">提交时间：${esc(jobTime(j.created_at))}</p><p>${esc(j.message||"")}${j.row_count?` · ${j.row_count}行`:""}</p>${j.plan?.length?`<p class="s18-help">识别结果：${esc(j.plan.map(p=>p.type).join(" · "))}</p>`:""}${jobTags(j)?`<p class="s18-job-tags">选中标签：${esc(jobTags(j))}</p>`:""}${jobRequirement(j,expanded)}${timingText(j.timings_ms)?`<p class="s18-help">耗时：${esc(timingText(j.timings_ms))}</p>`:""}${(j.sample_changes||[]).slice(0,1).map(c=>`<p>${esc(c.row_id)} · ${esc(c.attribute)}：${esc(c.before)} → ${esc(c.after)}${c.evidence_only?"（来源或依据更新）":""}</p>`).join("")}</div>`).join("")||"暂无处理任务";
        panel.scrollTop=scrollTop; jobsSignature=signature;
        qa("[data-job-id]").forEach(el=>el.onclick=async()=>{const label=el.textContent;el.disabled=true;el.textContent="正在加载预览…";notice("正在加载任务预览，请稍候…");try{await showPagedJob(el.dataset.jobId);notice("预览已显示，确认应用后才会更新数据。");q("#s18Preview").scrollIntoView({block:"start",behavior:"instant"});q("#s18Preview").focus({preventScroll:true});}catch(e){notice(e.message,true);}finally{el.disabled=false;el.textContent=label;}});
        qa("[data-cancel-job]").forEach(el=>el.onclick=async()=>{try{await post("cancel-job",{job_id:el.dataset.cancelJob});const job=jobs.find(j=>j.id===el.dataset.cancelJob);if(job?.proposal_id===proposal?.id)invalidate();notice("已提交取消；正在进行的模型请求结束后停止。");jobsSignature=null;startJobPolling(generation);}catch(e){notice(e.message,true);}});
        qa("[data-retry-job]").forEach(el=>el.onclick=async()=>{try{await post("retry-job",{job_id:el.dataset.retryJob});notice("已重新提交任务，完成后可查看新预览。");jobsSignature=null;startJobPolling(generation);}catch(e){notice(e.message,true);}});
        controls();
      }
    } catch(e){if(epoch===generation && seq===jobPollSeq){jobsSignature=null;q("#s18Jobs").textContent="任务进度读取失败："+e.message;}}
    if(epoch===generation && seq===jobPollSeq)timer=setTimeout(()=>pollJobs(epoch,seq),2000);
  }
  async function preview() {
    const requirement=q("#s18Request").value.trim();
    if(!requirement || !fields.size){notice("请选择属性并填写处理要求。",true);return;}
    if(requirement.includes("【填写目标单位】")){notice("请先在要求中填写目标单位。",true);return;}
    if(options.isRunning()){notice("当前有流水线运行，请结束后再操作步骤18。",true);return;}
    const epoch=generation;
    invalidate();active=true;controls();
    try {
      const extra={requirement,selection:selection(),mode:q("#s18Source").value};
      const explicit=requirement.match(/^把选中的值统一成[「“]([^」”]+)[」”][。.!！]?$/);
      if(explicit && fields.size===1) {
        const name=[...fields][0], values=filters.get(name);
        if(!values?.size)throw new Error("请先选择需要归并的取值。");
        extra.mapping={[name]:Object.fromEntries([...values].map(v=>[v,explicit[1].trim()]))};
      }
      notice("正在提交处理任务，请稍候…");
      const taskPanel=q("#s18Jobs");
      const pending=document.createElement("div");
      pending.className="s18-job";pending.setAttribute("role","status");
      pending.textContent="正在提交处理任务…";taskPanel.prepend(pending);
      try { await post("preview",extra); } finally { pending.remove(); }
      if(epoch!==generation)return;
      fields.clear();filters.clear();rowIds=null;render();
      notice("任务已提交，已清空所选属性和取值；进度和结果见处理任务列表。");
      startJobPolling(generation);
    } catch(e){
      if(String(e.message||"").includes("当前版本已更新")){
        await load();
        if(data?.ready)notice("数据已更新，页面已刷新。处理要求已保留，请检查属性并重新选择取值后再次预览。",true);
      }else notice(e.message,true);
    } finally {active=false;controls();}
  }
  function load() {
    const selectedFile=options.getFile();
    if(loadPromise && loadFile===selectedFile)return loadPromise;
    loadFile=selectedFile;
    const pending=loadImpl();
    loadPromise=pending;
    pending.then(()=>{if(loadPromise===pending)loadPromise=null;},()=>{if(loadPromise===pending)loadPromise=null;});
    return pending;
  }
  async function loadImpl() {
    const epoch=++generation, selectedFile=options.getFile(), switching=file!==selectedFile;
    clearTimeout(timer);active=true;file=selectedFile;invalidate();
    clearTimeout(exportTimer);
    if(switching){fields.clear();filters.clear();numericGroups.clear();numericSearch.clear();rowIds=null;q("#s18Request").value="";q("#s18Find").value="";data=null;jobsSignature=null;}
    controls();
    q("#s18Rows").innerHTML='<div class="s18-loading">正在加载步骤17标注和步骤18有效版本…</div>';
    try {
      const exporting=await pollExport(selectedFile);
      if(epoch!==generation || selectedFile!==options.getFile())return;
      if(exporting){active=false;controls();return;}
      const result=await options.api(`/api/std-workspace?file=${encodeURIComponent(selectedFile)}&include_job=0`);
      if(epoch!==generation || file!==options.getFile())return;
      if(data?.version!==result.version){filters.clear();rowIds=null;}
      data=result;
      active=false;
      const names=new Set((data.attributes||[]).map(a=>a.name));
      fields=new Set([...fields].filter(a=>names.has(a)));
      render();
      // 上游尚未生成步骤17输入属于等待状态，面板已显示“等待步骤17输入”。
      // 只隐藏这两种已知情况，其他数据异常仍应原样展示。
      if(!result.ready && /^缺少步骤18输入：(input|result)$/.test(result.error || "")) q("#s18Message").hidden=true;
      else if(!result.ready)notice(result.error||"请先完成步骤17。",true);
      else if(result.stale)notice("上游标注或规则已更新。请点击重新导入步骤17；现有历史保留。",true);
      else q("#s18Message").hidden=true;
      startJobPolling(epoch);
    }catch(e){if(epoch===generation){active=false;data=null;q("#s18Rows").textContent="加载失败："+e.message;notice(e.message,true);controls();}}
  }
  function mount(opts) {
    options=opts;host=document.getElementById("secStd");
    q("#s18Reload").onclick=load;
    q("#s18Find").oninput=()=>render();
    q("#s18Start").onclick=preview;
    q("#s18ReviewColumns").onclick=()=>{
      if(fields.size<2)return;
      filters.clear();rowIds=null;
      q("#s18Request").value="逐行联合审核所选整列的跨属性冲突与一致性，覆盖全部商品及已有明确值。结合每行同一商品资料核对属性组合，按证据纠正；兼有多种模式或不同部件不直接判冲突，证据不足时保留原值并说明。";
      invalidate();render();
      notice(`已选中 ${fields.size} 个整列、全部 ${data.row_count} 行，已清除取值和行筛选。可补充具体冲突要求，再点击预览处理。`);
      q("#s18Request").focus();
    };
    q("#s18Request").oninput=invalidate;
    q("#s18Source").onchange=invalidate;
    q("#s18Cancel").onclick=invalidate;
    q("#s18MergeSelected").onclick=()=>{
      const name=[...fields][0], values=filters.get(name);if(!values?.size)return;
      const counts=new Map(data.attributes.find(a=>a.name===name).values.map(v=>[v.value,v.count]));
      const target=[...values].sort((a,b)=>(counts.get(b)||0)-(counts.get(a)||0))[0];
      q("#s18Request").value=`把选中的值统一成「${target}」。`;invalidate();q("#s18Request").focus();
    };
    q("#s18Apply").onclick=async()=>{
      if(!proposal)return;
      const cells=(proposal.patches||[]).filter(p=>p.type==="cell");
      const chosen=pagedPreview ? null : selectedChanges===null || selectedChanges.size===cells.length ? null : cells.filter((_,i)=>selectedChanges.has(i)).map(p=>({row_id:p.row_id,attribute:p.attribute}));
      if(chosen && !chosen.length){notice("请至少勾选一个要应用的修改。",true);return;}
      active=true;controls();notice("正在确认应用并更新列表，请稍候…");const pid=proposal.id, count=pagedPreview?pagedPreview.selectedCount:chosen?chosen.length:proposal.changed_cells;
      try{
        const result=await post("apply",{proposal_id:pid,partial:true,...(pagedPreview && pagedPreview.total?{selection_rules:pagedPreview.rules}:chosen?{selected_cells:chosen}:{})});
        invalidate();
        if(result.refresh_required){data.stale=true;notice("修改已保存，但局部刷新失败；请点击重新加载查看最新数据。",true);return;}
        if(result.rows_changed){await load();notice("商品行变更已应用，相关数据已刷新；交付文件需重新导出。");return;}
        if(!result.partial){data.stale=true;notice("修改已保存。服务版本较旧，请重启后重新加载查看最新数据。",true);return;}
        const removed=mergePartialResult(result);
        notice(`已应用：${count}格更新，相关属性已刷新；交付文件需重新导出。${timingText(result.timings_ms)?` ${timingText(result.timings_ms)}。`:""}${removed.length?` 已移除${removed.join("、")}中不再存在的取值筛选。`:""}`);
        startJobPolling(generation);
      }catch(e){notice(e.message,true);}finally{active=false;controls();}
    };
    q("#s18Export").onclick=async()=>{
      if(options.isRunning()){notice("流水线运行期间请勿导出。",true);return;}
      const selectedFile=file;
      exportingFiles.add(selectedFile);active=true;controls();
      exportIndicator(true,"正在导出当前版本，包含来源、依据和操作记录。完成前请勿运行步骤18。");
      notice("正在导出交付，请稍候；导出完成后才能继续运行步骤18。");
      options.toast?.("步骤18正在导出，请等待完成提示。","info");
      try{await post("export");exportingFiles.delete(selectedFile);await load();const message=completionMessage();exportIndicator(false,message);notice(message);options.toast?.(message,"ok");}
      catch(e){if(selectedFile===options.getFile()){notice(`导出未完成：${e.message}`,true);await pollExport(selectedFile,true,e.message);}}
      finally{if(selectedFile===options.getFile()){active=false;controls();}}
    };
    q("#s18RefreshInput").onclick=async()=>{
      if(!confirm("重新读取步骤17作为新的基准？当前版本及历史会保留，其他步骤文件不会修改。"))return;
      active=true;controls();try{await post("refresh-input");await load();}catch(e){notice(e.message,true);}finally{active=false;controls();}
    };
    q("#s18AuditNegative").onclick=async()=>{
      if(!confirm("二审所有「否/不适用」的依据是否真的支持该取值？只生成预览，确认后才改数据。"))return;
      active=true;controls();
      try{
        const r=await post("audit");
        notice(`已提交二审：${r.candidate_cells||0} 个候选格，结果会在任务列表里显示。`);
        await load();
      }catch(e){notice(e.message,true);}finally{active=false;controls();}
    };
  }
  return {mount,load,isExporting:selectedFile=>exportingFiles.has(selectedFile)};
})();
