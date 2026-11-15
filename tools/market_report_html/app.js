(() => {
  'use strict';
  const reports = Array.isArray(window.REPORTS) ? [...window.REPORTS] : [];
  const names = ['关键词分析', '市场大盘分析', 'VOC差评分析', '选品建议'];
  const icons = ['⌕', '▥', '◈', '✦'];
  const emptyFilters = () => ({site:'', price:'', brand:'', decision:'', issue:'', keyword:'', concept:''});
  const state = { report: 0, sheet: 0, search: '', filters: emptyFilters(), selected: {}, sort: {}, page: {}, tableExpanded: {}, aiEnabled:false };   // sheet:0 = 默认落在首屏「决策摘要」(2026-10-10)
  const $ = id => document.getElementById(id);
  const html = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const compact = value => String(value ?? '').replace(/\s+/g,' ').trim();
  const report = () => reports[state.report];
  const sheet = () => report()?.sheets[state.sheet];
  const isNumber = x => typeof x === 'number' && Number.isFinite(x);
  const short = (v,n=25) => compact(v).length>n ? compact(v).slice(0,n-1)+'…' : compact(v);
  const isPct = h => /占比|占纳入样本|占段内|同比|环比|覆盖率|宣称率|明确率|1星率|完整度|渗透率|比重|份额|CR[135]|Verified/.test(h);
  const isMoney = h => /CPC|价格|售价|均价|中位价|销额|销售额|营收/.test(h);
  const sourceRef = (table,row,sheetName=sheet()?.name || '') => row.sourceRef || table.sourceRef || `${sheetName}!${row.sourceCells?.[table.headers[0]] || columnLetter(table.columns[0])+row.sourceRow}`;
  const columnLetter = n => {let s='';while(n>0){n--;s=String.fromCharCode(65+n%26)+s;n=Math.floor(n/26)}return s};
  const fmt = (v,h='') => {
    if(v===null || v===undefined || v==='') return '—';
    if(!isNumber(v)) return String(v);
    if(isPct(h) && Math.abs(v)<=1.5) return (v*100).toFixed(1)+'%';
    if(isMoney(h)) return v.toLocaleString('zh-CN',{minimumFractionDigits: h.includes('销额')?0:2,maximumFractionDigits:2});
    if(Math.abs(v)>=1000) return Math.round(v).toLocaleString('zh-CN');
    return Number.isInteger(v)?String(v):v.toLocaleString('zh-CN',{maximumFractionDigits:2});
  };
  let toastTimer;
  function toast(message){const el=$('toast');el.textContent=message;el.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),2800)}
  function closeOverlays(){ $('scrim').classList.remove('show'); $('aiDrawer').classList.remove('open'); $('detailDrawer').classList.remove('open'); $('sidebar').classList.remove('mobile-open'); $('aiDrawer').setAttribute('aria-hidden','true'); $('detailDrawer').setAttribute('aria-hidden','true'); }
  function openDrawer(id){closeOverlays();$(id).classList.add('open');$(id).setAttribute('aria-hidden','false');$('scrim').classList.add('show')}
  function allTables(s=sheet()){
    if(!s) return [];
    const out=[];
    for(const section of s.sections){
      if(s.name==='市场大盘分析' && /Top20/.test(section.title) && Array.isArray(s.top20) && s.top20.length){
        const headers=['ASIN','价格段','销额排名','品牌','价格','月销量','月销额','机身材质','容量','供电方式','APP','商品链接'];
        out.push({section,table:{id:'top20-index',title:'Top20 产品索引 · 与价格段、品牌联动',headers,columns:headers.map((_,i)=>i+1),sourceHeaderRow:172,rows:s.top20.map(p=>({values:headers.map(h=>p[h]),sourceRow:p.sourceRow,sourceCells:p._source,record:p}))}});
      }
      section.tables.forEach(table=>out.push({section,table}));
    }
    return out;
  }
  const dimensions={site:/^(站点|国家|市场)$/,price:/^(价格段|价段|价格带|目标价段)$/,brand:/^品牌$/,decision:/^(决策|判断|当前判断)$/,issue:/^问题$/,keyword:/^关键词$/,concept:/^方案$/};
  function dimensionColumn(table,kind){
    if(kind==='price' && table.title==='价段内差异') return 0;
    return table.headers.findIndex(h=>dimensions[kind].test(h));
  }
  function activeFilters(){return Object.entries(state.filters).filter(([,value])=>value)}
  function projectTable(table){
    const active=activeFilters();
    if(!active.length)return table;
    let keep=table.headers.map((_,i)=>i);
    for(const [kind,filter] of active){
      if(dimensionColumn(table,kind)>=0)continue;
      if(kind==='price'){
        const bands=new Set(optionsFor('price'));
        const bandCols=table.headers.map((h,i)=>i>0&&bands.has(h)?i:-1).filter(i=>i>=0);
        if(!bandCols.length)return null;
        // This table is a price-by-column matrix. Keep only the selected band.
        keep=keep.filter(i=>i===0||table.headers[i]===filter);
        if(!keep.some(i=>i>0&&table.headers[i]===filter))return null;
        continue;
      }
      if(kind==='brand' && /^Top20面板/.test(table.title)){
        const brandRow=table.rows.find(row=>row.values[0]==='品牌');
        if(!brandRow)return null;
        keep=keep.filter(i=>i===0||String(brandRow.values[i])===filter);
        if(keep.length<2)return null;
        continue;
      }
      return null;
    }
    if(keep.length===table.headers.length)return table;
    return {...table,headers:keep.map(i=>table.headers[i]),columns:keep.map(i=>table.columns[i]),rows:table.rows.map(row=>({...row,values:keep.map(i=>row.values[i])}))};
  }
  function optionsFor(kind){
    const set=new Set();
    for(const {table} of allTables()){
      const i=dimensionColumn(table,kind);
      if(i<0) continue;
      for(const row of table.rows){const v=row.values[i];if(v!==null&&v!==undefined&&String(v).length<45)set.add(String(v))}
    }
    const priceStart=v=>/^[<＜]/.test(v)?0:Number(v.match(/\d+(?:\.\d+)?/)?.[0]??Infinity);
    return [...set].sort((a,b)=> kind==='price'?priceStart(a)-priceStart(b):a.localeCompare(b,'zh-CN'));
  }
  function rowMatches(table,row,ignoreKind='',ignoreSelected=false){
    for(const [kind,filter] of Object.entries(state.filters)){
      if(!filter || kind===ignoreKind) continue;
      const i=dimensionColumn(table,kind);
      if(i>=0 && String(row.values[i])!==filter) return false;
    }
    if(state.search && !row.values.some(v=>String(v??'').toLowerCase().includes(state.search))) return false;
    if(!ignoreSelected && state.selected[table.id]!==undefined && String(row.values[0])!==state.selected[table.id]) return false;
    return true;
  }
  function visibleRows(table,ignoreKind=''){return table.rows.filter(row=>rowMatches(table,row,ignoreKind))}
  function renderNav(){
    // 2026-10-10: 导航改为按**报告实际 sheet**生成 —— 原实现写死 4 个名字/图标, 报告改成 5 张表
    // (新增首屏「决策摘要」)后导航会错位/少一项。图标按位置循环取, 名字一律来自报告。
    const sheetNames=(report()?.sheets||[]).map(s=>s.name);
    const navNames=sheetNames.length?sheetNames:names;
    const navIcons=navNames.map((_,i)=>icons[i%icons.length]||'•');
    $('sideNav').innerHTML=navNames.map((name,i)=>`<button type="button" class="nav-button ${i===state.sheet?'active':''}" data-nav="${i}"><span class="nav-icon">${navIcons[i]}</span><span>${html(name)}</span><span class="nav-count">0${i+1}</span></button>`).join('');
    $('sideReportName').textContent=report()?.title || '暂无报告';
    $('crumbReport').textContent=report()?.title || '报告';
    $('crumbSheet').textContent=sheet()?.name || '';
  }
  function renderHero(){
    const s=sheet(),r=report(); if(!s) return;
    $('pageTitle').textContent=s.title;
    $('pageSubtitle').textContent=s.subtitle;
    $('sourceFile').textContent=r.filename;
    $('lastUpdated').textContent='交互页面 · '+new Date(r.generatedAt).toLocaleDateString('zh-CN');
    $('kpiGrid').innerHTML=s.kpis.map(k=>`<article class="kpi-card"><div class="kpi-label">${html(k.label)}</div><div class="kpi-value" title="${html(fmt(k.value,k.label))}">${html(fmt(k.value,k.label))}</div><div class="kpi-foot">来源 ${html(k.sourceRef || `${s.name}!${columnLetter(k.sourceCol)}${k.sourceRow}`)}</div></article>`).join('');
  }
  function renderFilters(){
    const labels={site:'站点',price:'价格段',brand:'品牌',decision:'决策',issue:'问题',keyword:'关键词',concept:'方案'};
    $('filterControls').innerHTML=Object.keys(labels).map(kind=>{
      const options=optionsFor(kind); if(!options.length) return '';
      return `<select class="filter-select" data-filter="${kind}" aria-label="${labels[kind]}"><option value="">全部${labels[kind]}</option>${options.map(v=>`<option value="${html(v)}" ${state.filters[kind]===v?'selected':''}>${html(v)}</option>`).join('')}</select>`;
    }).join('');
  }
  function sectionNumber(title,index){const m=title.match(/^\d{2}[A-Z]?/);return m?m[0]:String(index+1).padStart(2,'0')}
  function isMatrix(table){return /阶梯|热力图|Top20面板|低至中价段|中高至极端价格尾部/.test(table.title) || table.headers.length>14}
  function chartSpec(table,section){
    if(table.rows.length<2||table.rows.length>20) return null;
    const metricIndex=name=>table.headers.indexOf(name);
    if(table.headers[0]==='属性/功能'&&metricIndex('新品覆盖率')>=0&&metricIndex('老品覆盖率')>=0&&metricIndex('百分点差')>=0) return {type:'lifecycle',cat:0,newRate:metricIndex('新品覆盖率'),oldRate:metricIndex('老品覆盖率'),delta:metricIndex('百分点差'),newYes:metricIndex('新品具备数'),oldYes:metricIndex('老品具备数'),newKnown:metricIndex('新品已明确数'),oldKnown:metricIndex('老品已明确数'),newComplete:metricIndex('新品证据完整度'),oldComplete:metricIndex('老品证据完整度')};
    if(table.headers[0]==='属性/功能'&&metricIndex('新品主配置占比')>=0&&metricIndex('老品主配置占比')>=0&&metricIndex('百分点差')>=0) return {type:'lifecycle',dominant:true,cat:0,newRate:metricIndex('新品主配置占比'),oldRate:metricIndex('老品主配置占比'),delta:metricIndex('百分点差'),newYes:metricIndex('新品主配置数'),oldYes:metricIndex('老品主配置数'),newKnown:metricIndex('新品已明确数'),oldKnown:metricIndex('老品已明确数'),newComplete:metricIndex('新品证据完整度'),oldComplete:metricIndex('老品证据完整度')};
    if(table.headers[0]==='属性/功能'&&metricIndex('新品明确宣称率')>=0&&metricIndex('老品明确宣称率')>=0&&metricIndex('百分点差')>=0) return {type:'lifecycle',claims:true,cat:0,newRate:metricIndex('新品明确宣称率'),oldRate:metricIndex('老品明确宣称率'),delta:metricIndex('百分点差'),newYes:metricIndex('新品宣称数'),oldYes:metricIndex('老品宣称数'),newKnown:metricIndex('新品变体数'),oldKnown:metricIndex('老品变体数'),newComplete:metricIndex('新品字段明确率'),oldComplete:metricIndex('老品字段明确率')};
    if(table.title==='价格段'&&metricIndex('月销额')>=0) return {type:'columns',cat:0,metric:metricIndex('月销额'),metricName:'月销额'};
    if(table.headers[0]==='月份'&&metricIndex('类别合成搜索量')>=0) return {type:'line',cat:0,metric:metricIndex('类别合成搜索量'),metricName:'类别合成搜索量'};
    if(table.title==='automatic cat feeder 关键月份对比'&&metricIndex('月搜索量')>=0) return {type:'bar',cat:0,metric:metricIndex('月搜索量'),metricName:'月搜索量'};
    if(table.title==='长尾词'&&metricIndex('综合覆盖率')>=0) return {type:'bar',cat:0,metric:metricIndex('综合覆盖率'),metricName:'综合覆盖率'};
    if(table.title==='生命周期'&&metricIndex('数量占比')>=0) return {type:'composition',cat:0,metrics:['数量占比','销量占比','销额占比'].map(metricIndex)};
    if(table.title==='履约方式'&&metricIndex('销额占比')>=0) return {type:'composition',cat:0,metrics:[metricIndex('销额占比')]};
    if(table.title==='产品类型'&&metricIndex('销额占比')>=0) return {type:'donut',cat:0,metric:metricIndex('销额占比'),metricName:'销额占比'};
    if(table.title==='食碗材质'&&metricIndex('销额占比')>=0) return {type:'donut',cat:0,metric:metricIndex('销额占比'),metricName:'销额占比'};
    if(table.title==='品牌'&&table.rows.length>=8&&metricIndex('中位价')>=0&&metricIndex('销额占比')>=0&&metricIndex('变体数')>=0) return {type:'scatter',cat:0,x:metricIndex('中位价'),y:metricIndex('销额占比'),size:metricIndex('变体数')};
    if(['机身材质','过滤类型','主体材质','材质','材料','机身材料','碗体材质','面料'].includes(table.title)&&metricIndex('销额占比')>=0) return {type:'donut',cat:0,metric:metricIndex('销额占比'),metricName:'销额占比'};
    if(table.title==='供电方式'&&metricIndex('销额占比')>=0) return {type:'dot',cat:0,metric:metricIndex('销额占比'),metricName:'销额占比'};
    if(table.title==='功能'&&metricIndex('均价差')>=0) return {type:'diverging',cat:0,metric:metricIndex('均价差'),metricName:'均价差'};
    if(/热力图/.test(section.title)&&table.headers[0]==='功能') return {type:'heatmap'};
    if(table.headers.includes('方案')&&metricIndex('总分')>=0) return {type:'bar',cat:metricIndex('方案'),metric:metricIndex('总分'),metricName:'总分'};
    if(isMatrix(table)||/工程动作|体验、服务|用户场景|安全风险|机会白地|结论摘要|风险与验证|Listing|不建议方向/.test(section.title)) return null;
    let cat=0;
    if(/^排名$/.test(table.headers[0]) && table.headers.length>1) cat=1;
    const preferred=/(变体月销额|月销额|销额占比|站内月搜索量|评论数|变体数|样本数|月销量|机会分|总分|销量占比|数量占比|CPC)/;
    const numeric=table.headers.map((h,i)=>({h,i})).filter(({i,h})=>i!==cat && preferred.test(h) && table.rows.some(r=>isNumber(r.values[i])));
    let metric=numeric.find(x=>/月销额|评论数|站内月搜索量|销额占比/.test(x.h)) || numeric[0];
    if(!metric) return null;
    return {cat,metric:metric.i,metricName:metric.h,type:/月份|月度/.test(table.headers[cat])?'line':'bar'};
  }
  const compactNumber=v=>new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:1}).format(v);
  function mobileBars(table,rows,spec,diverging=false){
    const max=Math.max(...rows.map(r=>Math.abs(r.values[spec.metric])),1);
    return `<div class="mobile-bars">${rows.map(row=>{const value=row.values[spec.metric],label=(diverging&&value>0?'+':'')+fmt(value,spec.metricName),width=Math.max(2,Math.abs(value)/max*100),color=diverging&&value<0?'negative':'';return `<div class="mobile-bar-row" role="button" tabindex="0" aria-label="${html(row.values[spec.cat])} · ${html(label)}，点击筛选" ${chartInteraction(table,row,spec.cat)}><div class="mobile-bar-label"><span>${html(row.values[spec.cat])}</span><strong>${html(label)}</strong></div><div class="mobile-bar-track"><i class="${color}" style="width:${width}%"></i></div></div>`}).join('')}</div>`;
  }
  function chartInteraction(table,row,cat){
    const key=String(row.values[cat]),kind=Object.keys(dimensions).find(k=>dimensionColumn(table,k)===cat)||'';
    return `data-chart-table="${html(table.id)}" data-chart-key="${html(String(row.values[0]))}" data-chart-kind="${html(kind)}" data-chart-filter="${html(key)}"`;
  }
  function renderDonutChart(table,spec){
    const rows=table.rows.filter(row=>rowMatches(table,row,'',true)&&isNumber(row.values[spec.metric]));
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const colors=['#3178b9','#67b4a5','#dda265','#9f9bc6','#8ba6bb'],C=2*Math.PI*77,selected=rows.find(row=>state.selected[table.id]===String(row.values[0])),lead=selected||rows.reduce((a,b)=>a.values[spec.metric]>b.values[spec.metric]?a:b);
    let offset=0;
    const arcs=rows.map((row,i)=>{const value=Math.max(0,Math.min(1,row.values[spec.metric])),length=value*C,start=offset;offset+=length;return `<circle class="donut-segment" ${chartInteraction(table,row,spec.cat)} cx="130" cy="130" r="77" fill="none" stroke="${colors[i%colors.length]}" stroke-width="${selected&&selected!==row?'27':selected?'39':'33'}" stroke-dasharray="${length.toFixed(2)} ${(C-length).toFixed(2)}" stroke-dashoffset="${(-start).toFixed(2)}" transform="rotate(-90 130 130)"><title>${html(row.values[spec.cat])} · ${html(fmt(value,spec.metricName))}</title></circle>`}).join('');
    const legend=rows.map((row,i)=>`<button type="button" class="donut-legend-item ${selected===row?'selected':''}" ${chartInteraction(table,row,spec.cat)}><i style="background:${colors[i%colors.length]}"></i><span>${html(row.values[spec.cat])}</span><strong>${html(fmt(row.values[spec.metric],spec.metricName))}</strong></button>`).join('');
    return `<div class="chart-title">${html(spec.metricName)}构成 · 点击扇区或图例查看对应数据</div><div class="donut-layout"><svg viewBox="0 0 260 260" role="img" aria-label="${html(table.title)}销额占比环形图"><circle cx="130" cy="130" r="77" fill="none" stroke="#edf2f7" stroke-width="33"/>${arcs}<text x="130" y="122" text-anchor="middle" class="donut-center-value">${html(fmt(lead.values[spec.metric],spec.metricName))}</text><text x="130" y="146" text-anchor="middle" class="donut-center-label">${html(short(lead.values[spec.cat],11))}</text></svg><div class="donut-legend">${legend}</div></div>`;
  }
  function renderDotChart(table,spec){
    const rows=table.rows.filter(row=>rowMatches(table,row,'',true)&&isNumber(row.values[spec.metric]));
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const scale=Math.max(.1,Math.ceil(Math.max(...rows.map(r=>r.values[spec.metric]))*10)/10);
    const axis=`<div class="dot-axis"><span>0%</span><span>${html(fmt(scale/2,'占比'))}</span><span>${html(fmt(scale,'占比'))}</span></div>`;
    const points=rows.map(row=>{const value=row.values[spec.metric],position=Math.min(100,Math.max(0,value/scale*100));return `<button type="button" class="dot-row ${state.selected[table.id]===String(row.values[0])?'selected':''}" ${chartInteraction(table,row,spec.cat)} title="${html(row.values[spec.cat])} · ${html(fmt(value,spec.metricName))}"><span class="dot-name">${html(row.values[spec.cat])}</span><span class="dot-track"><i style="left:${position}%"></i></span><strong>${html(fmt(value,spec.metricName))}</strong></button>`}).join('');
    return `<div class="chart-title">${html(spec.metricName)} · 点的位置表示全类目占比</div><div class="dot-chart">${axis}${points}</div>`;
  }
  function renderScatterChart(table,spec){
    const rows=visibleRows(table).filter(row=>[spec.x,spec.y,spec.size].every(i=>isNumber(row.values[i])));
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const full=table.rows.filter(row=>[spec.x,spec.y,spec.size].every(i=>isNumber(row.values[i]))),maxX=Math.max(20,Math.ceil(Math.max(...full.map(r=>r.values[spec.x]))/20)*20),maxY=Math.max(.05,Math.ceil(Math.max(...full.map(r=>r.values[spec.y]))*20)/20),maxSize=Math.max(...full.map(r=>r.values[spec.size]),1);
    const W=900,H=365,left=65,right=48,top=28,bottom=58,plotW=W-left-right,plotH=H-top-bottom,x=v=>left+v/maxX*plotW,y=v=>H-bottom-v/maxY*plotH;
    const xGrid=[0,.25,.5,.75,1].map(t=>{const xx=left+t*plotW;return `<line x1="${xx}" y1="${top}" x2="${xx}" y2="${H-bottom}" class="chart-grid"/><text x="${xx}" y="${H-bottom+21}" text-anchor="middle" class="chart-axis">$${Math.round(maxX*t)}</text>`}).join('');
    const yGrid=[0,.25,.5,.75,1].map(t=>{const yy=H-bottom-t*plotH;return `<line x1="${left}" y1="${yy}" x2="${W-right}" y2="${yy}" class="chart-grid"/><text x="${left-9}" y="${yy+3}" text-anchor="end" class="chart-axis">${html(fmt(maxY*t,'占比'))}</text>`}).join('');
    const points=rows.map((row,i)=>{const brand=String(row.values[spec.cat]),xx=x(row.values[spec.x]),yy=y(row.values[spec.y]),radius=7+Math.sqrt(row.values[spec.size]/maxSize)*15,selected=state.filters.brand===brand,label=selected||row.values[spec.y]>=.1;return `<g class="scatter-point" ${chartInteraction(table,row,spec.cat)}><title>${html(brand)} · 中位价 $${html(fmt(row.values[spec.x],'中位价'))} · 销额占比 ${html(fmt(row.values[spec.y],'占比'))} · ${html(row.values[spec.size])} 个变体</title><circle cx="${xx}" cy="${yy}" r="${radius}" fill="${selected?'#1d5b93':i<2?'#3681bb':'#68adbc'}" fill-opacity=".78" stroke="#fff" stroke-width="2"/>${label?`<text x="${xx+radius+5}" y="${yy-5}" class="scatter-label">${html(brand)}</text>`:''}</g>`}).join('');
    const mobilePoints=rows.map((row,i)=>{const brand=String(row.values[spec.cat]),px=Math.min(95,Math.max(5,row.values[spec.x]/maxX*100)),py=Math.min(95,Math.max(5,row.values[spec.y]/maxY*100)),diameter=12+Math.sqrt(row.values[spec.size]/maxSize)*20;return `<button type="button" class="scatter-mobile-point ${state.filters.brand===brand?'selected':''}" ${chartInteraction(table,row,spec.cat)} style="left:${px}%;bottom:${py}%;width:${diameter}px;height:${diameter}px;background:${i<2?'#3681bb':'#68adbc'}" aria-label="${html(brand)}，中位价 $${html(fmt(row.values[spec.x],'中位价'))}，销额占比 ${html(fmt(row.values[spec.y],'占比'))}，${html(row.values[spec.size])} 个变体" title="${html(brand)} · $${html(fmt(row.values[spec.x],'中位价'))} · ${html(fmt(row.values[spec.y],'占比'))}"></button>`}).join('');
    const mobile=`<div class="scatter-mobile"><div class="scatter-mobile-y"><span>${html(fmt(maxY,'占比'))}</span><span>${html(fmt(maxY/2,'占比'))}</span><span>0%</span></div><div class="scatter-mobile-body"><div class="scatter-mobile-plot">${mobilePoints}</div><div class="scatter-mobile-x"><span>$0</span><span>$${Math.round(maxX/2)}</span><span>$${Math.round(maxX)}</span></div><div class="scatter-mobile-caption">中位价（美元）</div></div></div>`;
    const list=rows.map(row=>`<button type="button" class="scatter-list-item ${state.filters.brand===String(row.values[spec.cat])?'selected':''}" ${chartInteraction(table,row,spec.cat)}><span>${html(row.values[spec.cat])}</span><strong>$${html(fmt(row.values[spec.x],'中位价'))} · ${html(fmt(row.values[spec.y],'占比'))}</strong></button>`).join('');
    return `<div class="chart-title">品牌中位价 × 全类目销额占比 · 气泡大小代表变体数 · 点击筛选品牌</div><div class="scatter-viewport"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="品牌价格与销额占比散点图">${xGrid}${yGrid}${points}<text x="${left+plotW/2}" y="${H-5}" text-anchor="middle" class="chart-axis">中位价（美元）</text><text x="15" y="${top+plotH/2}" transform="rotate(-90 15 ${top+plotH/2})" text-anchor="middle" class="chart-axis">销额占比</text></svg></div>${mobile}<div class="scatter-list">${list}</div>`;
  }
  function renderCompositionChart(table,spec){
    const rows=visibleRows(table),palette=['#3178b9','#62b4a4','#e0a064','#9c9cc2','#7c91a5'],W=900,left=135,right=70,top=18,rowH=52,H=top+spec.metrics.length*rowH+45,span=W-left-right;
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const bars=spec.metrics.map((metric,j)=>{
      let x=left;const y=top+j*rowH;
      const parts=rows.map((row,i)=>{
        const value=Math.max(0,Math.min(1,Number(row.values[metric])||0)),width=value*span,start=x;x+=width;
        if(width<1)return '';
        const label=width>53?`<text x="${start+width/2}" y="${y+22}" text-anchor="middle" class="composition-value">${html(fmt(value,table.headers[metric]))}</text>`:'';
        return `<g class="chart-bar" ${chartInteraction(table,row,spec.cat)}><title>${html(row.values[spec.cat])} · ${html(table.headers[metric])} ${html(fmt(value,table.headers[metric]))}</title><rect x="${start}" y="${y}" width="${width}" height="32" fill="${palette[i%palette.length]}"/>${label}</g>`;
      }).join('');
      return `<text x="${left-12}" y="${y+22}" text-anchor="end" class="chart-label">${html(table.headers[metric])}</text><rect x="${left}" y="${y}" width="${span}" height="32" rx="5" fill="#edf2f7"/>${parts}`;
    }).join('');
    const legend=rows.map((row,i)=>`<span class="chart-legend-item"><i style="background:${palette[i%palette.length]}"></i>${html(short(row.values[spec.cat],22))}</span>`).join('');
    const mobile=`<div class="mobile-composition">${spec.metrics.map(metric=>`<div class="mobile-composition-group"><strong>${html(table.headers[metric])}</strong><div class="mobile-stack">${rows.map((row,i)=>`<i ${chartInteraction(table,row,spec.cat)} style="width:${Math.max(0,Math.min(100,(Number(row.values[metric])||0)*100))}%;background:${palette[i%palette.length]}"></i>`).join('')}</div><div class="mobile-composition-values">${rows.map((row,i)=>`<span role="button" tabindex="0" aria-label="${html(row.values[spec.cat])} · ${html(table.headers[metric])} ${html(fmt(row.values[metric],table.headers[metric]))}，点击筛选" ${chartInteraction(table,row,spec.cat)}><i style="background:${palette[i%palette.length]}"></i>${html(row.values[spec.cat])} ${html(fmt(row.values[metric],table.headers[metric]))}</span>`).join('')}</div></div>`).join('')}</div>`;
    return `<div class="chart-title">${spec.metrics.length>1?'不同口径下的生命周期构成':'月销额构成'} · 百分比沿用原报告总量口径</div><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${html(table.title)}占比图">${bars}</svg><div class="chart-legend">${legend}</div>${mobile}`;
  }
  function renderColumnChart(table,spec){
    const rows=chartRows(table,spec),W=900,H=270,left=55,right=25,top=33,bottom=55,span=W-left-right;
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const max=Math.max(...rows.map(r=>r.values[spec.metric]),1),step=span/rows.length,barW=Math.min(56,step*.68),base=H-bottom;
    const grid=[0,.25,.5,.75,1].map(t=>{const y=base-t*(base-top);return `<line x1="${left}" y1="${y}" x2="${W-right}" y2="${y}" class="chart-grid"/><text x="${left-9}" y="${y+3}" text-anchor="end" class="chart-axis">${html(compactNumber(max*t))}</text>`}).join('');
    const bars=rows.map((r,i)=>{const v=r.values[spec.metric],height=Math.max(2,v/max*(base-top)),x=left+i*step+(step-barW)/2,y=base-height,key=r.values[spec.cat];return `<g class="chart-bar" ${chartInteraction(table,r,spec.cat)}><title>${html(key)} · ${html(fmt(v,spec.metricName))}</title><rect x="${x}" y="${y}" width="${barW}" height="${height}" rx="4" fill="#5f9bd0"/><text x="${x+barW/2}" y="${Math.max(top-7,y-7)}" text-anchor="middle" class="chart-value">${html(compactNumber(v))}</text><text x="${x+barW/2}" y="${base+19}" text-anchor="middle" class="chart-label">${html(short(key,12))}</text></g>`}).join('');
    return `<div class="chart-title">各价格段月销额 · 按原报告价格顺序</div><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="价格段月销额柱状图">${grid}${bars}</svg>${mobileBars(table,rows,spec)}`;
  }
  function renderDivergingChart(table,spec){
    const rows=chartRows(table,spec),W=900,left=210,center=520,right=80,rowH=30,H=rows.length*rowH+37,max=Math.max(...rows.map(r=>Math.abs(r.values[spec.metric])),1),span=Math.min(center-left-20,W-center-right-20);
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const bars=rows.map((r,i)=>{const v=r.values[spec.metric],width=Math.max(2,Math.abs(v)/max*span),x=v<0?center-width:center,y=22+i*rowH,key=r.values[spec.cat],label=(v>0?'+':'')+fmt(v,spec.metricName);return `<g class="chart-bar" ${chartInteraction(table,r,spec.cat)}><title>${html(key)} · 均价差 ${html(label)}</title><text x="${left}" y="${y+16}" text-anchor="end" class="chart-label">${html(short(key,27))}</text><rect x="${x}" y="${y+3}" width="${width}" height="19" rx="3" fill="${v<0?'#d8946b':'#68a0cf'}"/><text x="${v<0?x-7:x+width+7}" y="${y+17}" text-anchor="${v<0?'end':'start'}" class="chart-value">${html(label)}</text></g>`}).join('');
    return `<div class="chart-title">功能明确标注商品与未标注商品的均价差 · 横截面对比，非因果效果</div><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="功能价格差异图"><line x1="${center}" y1="14" x2="${center}" y2="${H-5}" class="chart-zero"/>${bars}</svg>${mobileBars(table,rows,spec,true)}`;
  }
  function renderHeatmapChart(table){
    const rows=visibleRows(table);
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const header=`<tr><th>功能 / 价格段</th>${table.headers.slice(1).map(h=>`<th>${html(h)}</th>`).join('')}</tr>`;
    const body=rows.map(row=>`<tr><th>${html(row.values[0])}</th>${row.values.slice(1).map((v,i)=>{const band=table.headers[i+1],valid=isNumber(v),value=valid?Math.max(0,Math.min(1,v)):0,alpha=(.08+value*.78).toFixed(2),color=value>.52?'#fff':'#24465f',label=valid?fmt(v,'占比'):'—';return `<td style="background:rgba(49,120,185,${alpha});color:${color}">${valid?`<button class="heatmap-cell" type="button" data-chart-table="${html(table.id)}" data-chart-key="${html(String(row.values[0]))}" data-chart-kind="price" data-chart-filter="${html(band)}" aria-label="${html(row.values[0])} · ${html(band)} · ${html(label)}，点击筛选价格段" title="${html(row.values[0])} · ${html(band)} · ${html(label)}">${html(label)}</button>`:html(label)}</td>`}).join('')}</tr>`).join('');
    return `<div class="chart-title">功能覆盖率 · 颜色越深表示占比越高 · 点击格子筛选价格段</div><div class="heatmap-scroll"><table class="heatmap-table" aria-label="价格段功能覆盖率热力图"><thead>${header}</thead><tbody>${body}</tbody></table></div><div class="heatmap-scale"><span>低 0%</span><i></i><span>高 100%</span></div>`;
  }
  function chartRows(table,spec){return visibleRows(table).filter(r=>isNumber(r.values[spec.metric])).slice(0,12)}
  function renderLifecycleChart(table,spec){
    const rows=visibleRows(table).filter(r=>isNumber(r.values[spec.newRate])&&isNumber(r.values[spec.oldRate]));
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const items=rows.map(row=>{
      const name=String(row.values[0]),fresh=row.values[spec.newRate],old=row.values[spec.oldRate],delta=row.values[spec.delta],from=Math.min(fresh,old)*100,width=Math.abs(fresh-old)*100;
      const newBase=row.values[spec.newKnown],oldBase=row.values[spec.oldKnown],newComplete=row.values[spec.newComplete],oldComplete=row.values[spec.oldComplete];
      const thin=newComplete<.5||oldComplete<.5,sparse=row.values[spec.newYes]<20||row.values[spec.oldYes]<20;
      const warning=thin?'证据覆盖低':sparse?'明确数少':'';
      const newTotal=spec.claims?newBase:newComplete>0?Math.round(newBase/newComplete):'?',oldTotal=spec.claims?oldBase:oldComplete>0?Math.round(oldBase/oldComplete):'?';
      const kindWord=spec.claims?'明确宣称':(spec.dominant?'主配置':'已明确');
      const title=spec.claims?`${name}：新品 ${fmt(fresh,'占比')}（${row.values[spec.newYes]}/${newTotal} 明确宣称）；老品 ${fmt(old,'占比')}（${row.values[spec.oldYes]}/${oldTotal} 明确宣称）；差 ${delta>0?'+':''}${delta.toFixed(1)} 个百分点${warning?'；'+warning:''}`:`${name}：新品 ${fmt(fresh,'占比')}（${newBase}/${newTotal} ${kindWord}）；老品 ${fmt(old,'占比')}（${oldBase}/${oldTotal} ${kindWord}）；差 ${delta>0?'+':''}${delta.toFixed(1)} 个百分点${warning?'；'+warning:''}`;
      return `<button type="button" class="lifecycle-row ${state.selected[table.id]===name?'selected':''}" data-chart-table="${html(table.id)}" data-chart-key="${html(name)}" title="${html(title)}" aria-label="${html(title)}，点击查看明细"><span class="lifecycle-name"><i>${html(row.values[1])}</i>${html(name)}</span><span class="lifecycle-track"><span class="lifecycle-link" style="left:${from}%;width:${width}%"></span><span class="lifecycle-point old" style="left:${old*100}%"></span><span class="lifecycle-point new" style="left:${fresh*100}%"></span></span><span class="lifecycle-values"><strong class="${delta<0?'negative':''}">${delta>0?'+':''}${delta.toFixed(1)} pp</strong><small>${html(fmt(fresh,'占比'))} / ${html(fmt(old,'占比'))}${warning?' · '+warning:''}</small></span></button>`;
    }).join('');
    return `<div class="chart-title">${spec.claims?'全部变体中的明确宣称率':(spec.dominant?'主配置占比(该列取值最多的配置)':'明确样本中的覆盖率')} · 蓝点新品、橙点老品 · 连线表示百分点差 · 点击查看数据</div><div class="lifecycle-legend"><span><i class="new"></i>新品</span><span><i class="old"></i>老品</span><small>0%</small><small>50%</small><small>100%</small></div><div class="lifecycle-chart">${items}</div><p class="lifecycle-caveat">${spec.claims?'未明确不视为否；宣称率只反映源表明确标注的比例。':'比较的是同一期新品与老品结构。已明确数和证据完整度见数据表。'}商品宣称不等于实测性能。</p>`;
  }
  function renderBarChart(table,spec){
    const rows=chartRows(table,spec);
    if(!rows.length)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const W=900,left=180,right=82,top=6,rowH=28,H=rows.length*rowH+top+8,max=Math.max(...rows.map(r=>Math.abs(r.values[spec.metric])),1);
    const mid=left,span=W-left-right;
    const bars=rows.map((r,i)=>{
      const y=top+i*rowH, v=r.values[spec.metric], width=Math.max(2,Math.abs(v)/max*span);
      const key=String(r.values[spec.cat]);
      const kind=Object.keys(dimensions).find(k=>dimensionColumn(table,k)===spec.cat)||'';
      const chosen=state.selected[table.id]===String(r.values[0]) || (kind&&state.filters[kind]===key);
      return `<g class="chart-bar" data-chart-table="${html(table.id)}" data-chart-key="${html(String(r.values[0]))}" data-chart-kind="${html(kind)}" data-chart-filter="${html(key)}"><title>${html(key)} · ${html(fmt(v,spec.metricName))}</title><text x="${left-10}" y="${y+17}" text-anchor="end" class="chart-label">${html(short(key,24))}</text><rect x="${mid}" y="${y+4}" width="${width}" height="18" rx="4" fill="${chosen?'#225888':'#6c9ac7'}"/><text x="${Math.min(mid+width+8,W-75)}" y="${y+17}" class="chart-value">${html(fmt(v,spec.metricName))}</text></g>`;
    }).join('');
    return `<div class="chart-title">${html(spec.metricName)} · 点击图形筛选本页相关表格</div><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${html(table.title)} ${html(spec.metricName)}图">${bars}</svg>${mobileBars(table,rows,spec)}`;
  }
  function renderLineChart(table,spec){
    const rows=chartRows(table,spec);
    if(rows.length<2)return '<div class="chart-empty">当前筛选下无可绘制数据</div>';
    const W=900,H=230,left=42,right=30,top=22,bottom=36,values=rows.map(r=>r.values[spec.metric]),min=Math.min(...values),max=Math.max(...values),range=Math.max(1,max-min);
    const x=i=>left+i*(W-left-right)/(rows.length-1),y=v=>H-bottom-(v-min)/range*(H-top-bottom);
    const grid=[0,.25,.5,.75,1].map(t=>{const yy=top+t*(H-top-bottom);return `<line x1="${left}" y1="${yy}" x2="${W-right}" y2="${yy}" class="chart-grid"/>`}).join('');
    const path=rows.map((r,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(values[i]).toFixed(1)}`).join(' ');
    const dots=rows.map((r,i)=>`<g><circle class="chart-dot" cx="${x(i)}" cy="${y(values[i])}" r="3.5"><title>${html(r.values[spec.cat])} · ${html(fmt(values[i],spec.metricName))}</title></circle><text x="${x(i)}" y="${H-11}" text-anchor="middle" class="chart-label">${html(short(r.values[spec.cat],5))}</text></g>`).join('');
    return `<div class="chart-title">${html(spec.metricName)} · 趋势来自工作簿原始月份</div><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="趋势图">${grid}<path d="${path}" class="chart-line"/>${dots}</svg>`;
  }
  function sortedRows(table){
    const rows=[...visibleRows(table)],conf=state.sort[table.id];
    if(conf){rows.sort((a,b)=>{const x=a.values[conf.index],y=b.values[conf.index];return (isNumber(x)&&isNumber(y)?x-y:String(x??'').localeCompare(String(y??''),'zh-CN'))*conf.dir})}
    return rows;
  }
  function cellHtml(value,header){
    const text=fmt(value,header);
    if(typeof value==='string' && /^https?:\/\//i.test(value))return `<a href="${html(value)}" target="_blank" rel="noopener noreferrer">查看商品 ↗</a>`;
    if(/^(优先开发|小批验证|持续观察|不建议进入|P0|P1|P2|P3|待验证|未提供)$/.test(text)){
      const tone=/优先开发/.test(text)?'positive':/不建议|P0|P1/.test(text)?'risk':/小批|待验证|持续观察/.test(text)?'warn':'';
      return `<span class="status-chip ${tone}">${html(text)}</span>`;
    }
    return html(text);
  }
  function renderTable(table,section){
    const rows=sortedRows(table),pageSize=isMatrix(table)?30:12,pages=Math.max(1,Math.ceil(rows.length/pageSize)),page=Math.min(state.page[table.id]||0,pages-1);state.page[table.id]=page;
    const shown=rows.slice(page*pageSize,(page+1)*pageSize),spec=chartSpec(table,section);
    const priceLadder=/^(低至中价段|中高至极端价格尾部)$/.test(table.title);
    const top20Panel=/^Top20面板[12]/.test(table.title);
    const showTable=!spec || state.tableExpanded[table.id]===true || (state.tableExpanded[table.id]!==false && (table.id==='r-score' || activeFilters().length>0 || !!state.search || state.selected[table.id]!==undefined));
    const title=table.title==='排名' && table.headers[1]?table.headers[1]+' · 榜单':table.title;
    const head=`<div class="card-head"><div class="card-heading"><h3>${html(title)}</h3><p>${html(table.sourceLabel || `${sheet().name} · ${section.title} · 表头源行 ${table.sourceHeaderRow}`)} · ${rows.length}/${table.rows.length} 行</p></div><div class="card-actions">${spec?`<button class="mini-button ${showTable?'active':''}" data-toggle-table="${html(table.id)}" aria-expanded="${showTable}">${showTable?'收起数据':'查看数据'}</button>`:''}<button class="mini-button" data-csv="${html(table.id)}">导出 CSV ↗</button></div></div>`;
    const renderer={line:renderLineChart,bar:renderBarChart,composition:renderCompositionChart,columns:renderColumnChart,diverging:renderDivergingChart,heatmap:renderHeatmapChart,donut:renderDonutChart,dot:renderDotChart,scatter:renderScatterChart,lifecycle:renderLifecycleChart};
    const chart=spec?`<div class="chart-area">${renderer[spec.type](table,spec)}</div>`:'';
    const th=table.headers.map((h,i)=>`<th data-sort-table="${html(table.id)}" data-sort-index="${i}" title="点击排序">${html(h)} <span class="sort-indicator">${state.sort[table.id]?.index===i?(state.sort[table.id].dir===1?'↑':'↓'):'↕'}</span></th>`).join('');
    const body=shown.map(row=>`<tr data-row-table="${html(table.id)}" data-row-source="${row.sourceRow}" tabindex="0" role="button" aria-expanded="${state.selected[table.id]===String(row.values[0])}" aria-label="查看来源行 ${html(String(row.values[0]))} 的证据" class="${state.selected[table.id]===String(row.values[0])?'selected':''} ${priceLadder&&/^(变体数|销量占比|销额占比)$/.test(String(row.values[0]))?'ladder-summary':''}">${row.values.map((v,j)=>{const hint=isMatrix(table)&&j>0?String(row.values[0]):table.headers[j];return `<td class="${isNumber(v)?'number':''} ${typeof v==='string'&&/^https?:\/\//.test(v)?'link':''}" title="${html(fmt(v,hint))}">${cellHtml(v,hint)}</td>`}).join('')}</tr>`).join('') || `<tr><td colspan="${table.headers.length}" class="empty-state">本表没有匹配的行。请调整搜索或筛选条件(正文命中见上方搜索计数)。</td></tr>`;
    const foot=`<div class="card-foot"><span>点击任意行查看来源位置 · 已显示 ${shown.length} 行</span><div class="pagination"><button data-page-table="${html(table.id)}" data-page-dir="-1" ${page===0?'disabled':''}>‹</button><span>${page+1} / ${pages}</span><button data-page-table="${html(table.id)}" data-page-dir="1" ${page===pages-1?'disabled':''}>›</button></div></div>`;
    const tableBody=showTable?`<div class="table-scroll"><table class="data-table"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>${foot}`:'';
    return `<article class="table-card ${spec?'visual-card':''} ${priceLadder?'price-ladder-card':''} ${top20Panel?'top20-panel-card':''}" id="table-${html(table.id)}" ${priceLadder?`style="--ladder-columns:${table.headers.length-1}"`:top20Panel?`style="--panel-columns:${table.headers.length-1}"`:''}>${head}${chart}${tableBody}</article>`;
  }
  function renderSections(){
    const s=sheet(); if(!s)return;
    const active=activeFilters(),filtering=active.length>0;
    let count=0,tableCount=0;
    $('sections').innerHTML=s.sections.map((section,index)=>{
      const selectedTables=allTables(s).filter(x=>x.section===section).map(x=>projectTable(x.table)).filter(t=>t&&visibleRows(t).length);
      const matchedNotes=filtering?[]:section.notes.filter(n=>!state.search||n.text.toLowerCase().includes(state.search));
      const matchedInsights=filtering?[]:(section.insights||[]).filter(n=>!state.search||n.text.toLowerCase().includes(state.search));
      if(!selectedTables.length&&!matchedNotes.length&&!matchedInsights.length)return '';
      count+=selectedTables.reduce((a,t)=>a+visibleRows(t).length,0);
      tableCount+=selectedTables.length;
      const notes=matchedNotes.length?`<div class="section-notes">${matchedNotes.map(n=>`<div class="note">${html(n.text)} <span style="opacity:.65">· ${html(n.sourceRef || `${s.name}!A${n.row}`)}</span></div>`).join('')}</div>`:'';
      const insights=matchedInsights.length?`<div class="insights">${matchedInsights.map(n=>`<div class="insight-card"><span class="insight-no">${html(n.number)}</span><div>${html(n.text)}<div style="font-size:9px;color:#9cabb8;margin-top:5px">${html(n.sourceRef || `${s.name}!B${n.sourceRow}`)}</div></div></div>`).join('')}</div>`:'';
      return `<section class="report-section" id="section-${index}"><div class="section-header"><span class="section-index">${sectionNumber(section.title,index)}</span><h2>${html(section.title.replace(/^\d{2}[A-Z]?\s*/,''))}</h2><span class="section-rule"></span></div>${selectedTables.map(t=>renderTable(t,section)).join('')}${insights}${notes}</section>`;
    }).join('') || ((state.search||filtering) && (state._lastTextOnly||0)>0
      ? `<div class="empty-state">没有匹配的<b>表格行</b>, 但正文命中 ${state._lastTextOnly} 处 —— 这不是"没有结果"。<br>请检查上方搜索计数或清除筛选(<button class="mini-button" data-reset-inline="1">清除筛选 ↺</button>)。</div>`
      : '<div class="empty-state">未找到匹配内容，请清除筛选后重试。</div>');
    // 搜索计数: **行匹配**(表格行) 与 **正文匹配**(备注/洞察文本) 分开报 ——
    // contract §3: "零表行但有正文命中" 不能被显示成"没有结果"。
    const textHits=filtering?0:s.sections.reduce((a,sec)=>a
      +sec.notes.filter(n=>!state.search||n.text.toLowerCase().includes(state.search)).length
      +((sec.insights||[]).filter(n=>!state.search||n.text.toLowerCase().includes(state.search)).length),0);
    // 评论正文命中(contract §3): 证据库/评论类表格的**正文列**单独计数 ——
    // 零表行但有正文命中时页面必须区分提示, 不得显示成"没有结果"。
    let bodyHits=0;
    if(state.search){
      allTables(s).forEach(x=>{
        const t=projectTable(x.table); if(!t)return;
        if(!/证据库|评论/.test(String(t.title||'')))return;
        const bi=t.headers.findIndex(h=>/正文/.test(String(h)));
        if(bi<0)return;
        t.rows.forEach(r=>{if(String(r.values[bi]??'').toLowerCase().includes(state.search))bodyHits++});
      });
    }
    state._lastCounts={tables:tableCount,rows:count,text:textHits,body:bodyHits};
    state._lastTextOnly=Math.max(textHits,bodyHits);
    $('resultCount').textContent=state.search||filtering
      ? `${tableCount} 张表 · 行匹配 ${count} 行 · 正文匹配 ${textHits} 处${bodyHits?` · 评论正文命中 ${bodyHits} 条`:''}`
      : `${tableCount} 张表 · ${count} 行`;
    $('filterExplain').textContent=filtering?'筛选结果仅包含能同时满足已选条件的表格和图。顶部指标仍为报告总体，未按筛选重算。':'选择条件后，仅显示能按该字段筛选的表格与图；顶部指标始终是报告总体口径。';
    $('kpiGrid').classList.toggle('filtered',filtering);
    renderFilterSummary(active);
  }
  function renderFilterSummary(active){
    const el=$('filterSummary');
    if(!active.length){el.hidden=true;el.innerHTML='';return}
    const labels={site:'站点',price:'价格段',brand:'品牌',decision:'决策',issue:'问题',keyword:'关键词',concept:'方案'};
    const chips=active.map(([kind,value])=>`<span class="filter-pill">${html(labels[kind])} · ${html(value)}</span>`).join('');
    let metrics='';
    if(active.length===1){
      const [kind,selected]=active[0];
      const matches=allTables().map(x=>x.table).filter(t=>dimensionColumn(t,kind)===0);
      const picked=matches.map(t=>({table:t,row:t.rows.find(r=>String(r.values[0])===selected)})).find(x=>x.row);
      if(picked){
        const fields=picked.table.headers.map((h,i)=>({h,v:picked.row.values[i]})).filter(({h,v})=>/变体数|月销量|月销额|销额占比|评论数|占纳入样本|站内月搜索量|CPC|总分|决策/.test(h)&&v!==null&&v!==undefined).slice(0,4);
        metrics=fields.map(({h,v})=>`<span class="filter-metric"><strong>${html(fmt(v,h))}</strong><small>${html(h)}</small></span>`).join('');
      }
    }
    if(!metrics)metrics='<span class="filter-summary-note">显示交叉匹配记录；汇总指标不按复合条件重算。</span>';
    el.hidden=false;
    el.innerHTML=`<div class="filter-summary-label">当前筛选</div><div class="filter-pills">${chips}</div><div class="filter-metrics">${metrics}</div>`;
  }
  function render(){renderNav();renderHero();renderFilters();renderSections();document.title=`${sheet()?.name||'报告'} · ATLAS`}
  function setSheet(i){state.sheet=i;state.search='';state.filters=emptyFilters();state.selected={};state.page={};state.tableExpanded={};$('globalSearch').value='';render();closeOverlays();window.scrollTo({top:0,behavior:'smooth'})}
  function tableById(id){const base=allTables().find(x=>x.table.id===id)?.table;return base?projectTable(base):null}
  function showDetail(id,rowNo){
    const table=tableById(id),row=table?.rows.find(r=>r.sourceRow===rowNo);if(!table||!row)return;
    $('detailContent').innerHTML=`<div class="detail-title">${html(row.values[0])}</div><div class="detail-meta">${html(sourceRef(table,row))} · ${html(table.title)}</div>${table.headers.map((h,i)=>`<div class="detail-row"><strong>${html(h)}<br><small>${html(row.sourceCells?.[h] || sourceRef(table,row))}</small></strong><span>${cellHtml(row.values[i],isMatrix(table)&&i>0?String(row.values[0]):h)}</span></div>`).join('')}`;
    openDrawer('detailDrawer');
  }
  function downloadCsv(id){
    const table=tableById(id);if(!table)return;
    const quote=v=>'"'+String(v??'').replace(/"/g,'""')+'"';
    const text='\ufeff'+[table.headers.map(quote).join(','),...sortedRows(table).map(r=>r.values.map(quote).join(','))].join('\r\n');
    const url=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));
    const a=document.createElement('a');a.href=url;a.download=`${sheet().name}-${table.title.replace(/[\\/:*?"<>|]/g,'_')}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function topicSheet(question){
    const q=question.toLowerCase();
    if(/新品|老品|迭代|属性|功能/.test(q))return 1;
    if(/voc|差评|投诉|安全|故障|水泵|漏水/.test(q))return 2;
    if(/关键词|搜索|cpc|趋势|广告/.test(q))return 0;
    if(/价格|价段|品牌|top20|销量|销额|市场/.test(q))return 1;
    if(/选品|方案|机会|验证|产品定义/.test(q))return 3;
    return state.sheet;
  }
  function currentContext(question){
    const r=report(),target=topicSheet(question),s=r.sheets[target],same=target===state.sheet;
    const tables=allTables(s).map(({section,table})=>({section,table:same?projectTable(table):table})).filter(x=>x.table).map(({section,table})=>({module:section.title,title:table.title,headers:table.headers,rows:(same?visibleRows(table):table.rows).slice(0,25).map(row=>({values:row.values,source:sourceRef(table,row,s.name)}))})).filter(t=>t.rows.length);
    return {question,report:r.title,sheet:s.name,subtitle:s.subtitle,filters:same?state.filters:{},search:same?state.search:'',kpis:s.kpis,tables:tables.slice(0,25),insights:s.sections.flatMap(section=>(section.insights||[]).map(i=>({text:i.text,source:i.sourceRef || `${s.name}!B${i.sourceRow}`}))).slice(0,10)};
  }
  function localInterpret(question){
    const q=question.toLowerCase(),target=topicSheet(question),s=report().sheets[target],same=target===state.sheet;
    const pairs=allTables(s).map(x=>({...x,table:same?projectTable(x.table):x.table})).filter(x=>x.table),getRows=t=>same?visibleRows(t):t.rows;
    const scored=pairs.map(({section,table})=>{
      const corpus=(section.title+' '+table.title+' '+table.headers.join(' ')).toLowerCase();
      const words=q.match(/[\u4e00-\u9fa5]{2,}|[a-z0-9]+/gi)||[];
      const score=words.reduce((n,w)=>n+(corpus.includes(w)?3:0),0)+(/价格|价段/.test(q)&&/价格段/.test(corpus)?5:0)+(/品牌/.test(q)&&/品牌/.test(corpus)?5:0)+(/差评|voc|问题|风险/.test(q)&&/VOC|问题/.test(corpus)?5:0)+(/选品|方案|机会/.test(q)&&/方案|机会/.test(corpus)?5:0)+(/新品|老品|迭代/.test(q)&&/新老品|生命周期|功能迭代/.test(section.title)?12:0)+(/新品|老品|迭代/.test(q)&&table.headers.includes('新品明确宣称率')?12:0)+(/新品|老品/.test(q)&&/功能|属性/.test(q)&&/功能明确宣称/.test(table.title)?8:0)+(/新品|老品/.test(q)&&/类型|形态/.test(q)&&/产品类型差异/.test(table.title)?8:0);
      return {section,table,score};
    }).filter(x=>x.table.rows.length&&getRows(x.table).length).sort((a,b)=>b.score-a.score);
    const best=scored.find(x=>x.score>0) || scored.find(x=>chartSpec(x.table,x.section)) || scored[0];
    if(!best)return '当前筛选没有可用数据。请清除筛选后重试。';
    const t=best.table,sp=chartSpec(t,best.section),rows=getRows(t);
    const filterText=same?(Object.values(state.filters).filter(Boolean).join('、')||'当前页面'):'跨模块原始口径';
    const lines=[`范围：${filterText}；${s.name}的「${best.section.title}」。`];
    if(sp?.type==='lifecycle'){
      rows.filter(r=>isNumber(r.values[sp.delta])).sort((a,b)=>Math.abs(b.values[sp.delta])-Math.abs(a.values[sp.delta])).slice(0,4).forEach((row,i)=>lines.push(`对比 ${i+1}：${row.values[0]}，新品 ${fmt(row.values[sp.newRate],'占比')}、老品 ${fmt(row.values[sp.oldRate],'占比')}，差 ${row.values[sp.delta]>0?'+':''}${row.values[sp.delta].toFixed(1)} 个百分点。来源 ${sourceRef(t,row,s.name)}。`));
      best.section.notes.forEach(note=>lines.push(`${note.text}（${note.sourceRef || `${s.name}!A${note.row}`}）`));
    }else if(sp){
      const ranked=rows.filter(r=>isNumber(r.values[sp.metric])).sort((a,b)=>b.values[sp.metric]-a.values[sp.metric]).slice(0,3);
      ranked.forEach((row,i)=>lines.push(`事实 ${i+1}：${row.values[sp.cat]}的${sp.metricName}为 ${fmt(row.values[sp.metric],sp.metricName)}。来源 ${sourceRef(t,row,s.name)}。`));
    }else{
      rows.slice(0,3).forEach((row,i)=>lines.push(`证据 ${i+1}：${t.headers.slice(0,4).map((h,j)=>`${h} ${fmt(row.values[j],h)}`).join('；')}。来源 ${sourceRef(t,row,s.name)}。`));
    }
    const insight=s.sections.flatMap(x=>x.insights||[]).find(x=>!q||x.text.toLowerCase().includes(q.slice(0,2)));
    if(insight)lines.push(`报告解读：${insight.text}（${insight.sourceRef || `${s.name}!B${insight.sourceRow}`}）`);
    lines.push('判断：以上为工作簿中的描述性证据；进一步选品须结合成本、利润、测试及缺失数据验证。');
    return lines.join('\n\n');
  }
  function appendMessage(type,text,mode=''){
    const div=document.createElement('div');div.className=`message ${type}`;div.textContent=text;
    if(mode){const small=document.createElement('small');small.textContent=mode;div.appendChild(small)}
    $('aiConversation').appendChild(div);$('aiConversation').scrollTop=$('aiConversation').scrollHeight;
  }
  async function ask(question){
    if(!question.trim())return;
    openDrawer('aiDrawer');appendMessage('user',question);$('askInput').value='';
    if(state.aiEnabled){
      try{
        const response=await fetch('/api/interpret',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(currentContext(question))});
        if(!response.ok)throw new Error(`HTTP ${response.status}`);
        const data=await response.json();appendMessage('ai',data.answer,'模型解读 · 请核对来源行');return;
      }catch(e){toast('模型暂不可用，已使用本地证据解读')}
    }
    appendMessage('ai',localInterpret(question),'本地证据解读 · 不调用外部模型');
  }
  async function detectServer(){
    if(location.protocol==='file:'){$('aiMode').textContent='离线证据解读已就绪。回答依据内置报告及当前筛选数据，并显示工作表来源行。';return}
    try{const res=await fetch('/api/status');if(!res.ok)return;const data=await res.json();state.aiEnabled=!!data.modelConnected;$('aiMode').textContent=state.aiEnabled?`模型已连接（${data.model}）。根据当前筛选的数据回答，并列出工作表来源行。`:'本地证据解读已就绪。配置 OPENAI_API_KEY 后可启用模型问答。';}
    catch{$('aiMode').textContent='本地证据解读已就绪。运行本地服务并配置 API Key 后可启用模型问答。'}
  }
  function renderPrompts(){const list=['新品相对老品有哪些功能变化？','价格段有什么机会？','主要 VOC 风险是什么？','哪些方案值得优先验证？'];$('promptList').innerHTML=list.map(q=>`<button class="prompt-chip" data-prompt="${html(q)}">${html(q)}</button>`).join('')}
  document.addEventListener('click',event=>{
    const e=event.target.closest('[data-nav],[data-sort-table],[data-page-table],[data-row-table],[data-toggle-table],[data-csv],[data-chart-table],[data-prompt]');if(!e)return;
    if(e.dataset.nav!==undefined){setSheet(Number(e.dataset.nav));return}
    if(e.dataset.sortTable){const id=e.dataset.sortTable,index=Number(e.dataset.sortIndex),prev=state.sort[id];state.sort[id]={index,dir:prev?.index===index?-prev.dir:1};renderSections();return}
    if(e.dataset.pageTable){const id=e.dataset.pageTable;state.page[id]=Math.max(0,(state.page[id]||0)+Number(e.dataset.pageDir));renderSections();return}
    if(e.dataset.rowTable){if(event.target.closest('a'))return;showDetail(e.dataset.rowTable,Number(e.dataset.rowSource));return}
    if(e.dataset.toggleTable){state.tableExpanded[e.dataset.toggleTable]=e.getAttribute('aria-expanded')!=='true';renderSections();return}
    if(e.dataset.csv){downloadCsv(e.dataset.csv);return}
    if(e.dataset.chartTable){const id=e.dataset.chartTable,key=e.dataset.chartKey,kind=e.dataset.chartKind,filter=e.dataset.chartFilter;if(kind){state.filters[kind]=state.filters[kind]===filter?'':filter;delete state.selected[id]}else state.selected[id]=state.selected[id]===key?undefined:key;state.page={};renderFilters();renderSections();return}
    if(e.dataset.prompt){ask(e.dataset.prompt);return}
    if(e.dataset.resetInline){document.getElementById('resetFilters').click();return}
  });
  $('filterControls').addEventListener('change',e=>{if(!e.target.dataset.filter)return;state.filters[e.target.dataset.filter]=e.target.value;state.selected={};state.page={};renderSections()});
  $('globalSearch').addEventListener('input',e=>{state.search=e.target.value.trim().toLowerCase();state.page={};renderSections()});
  $('resetFilters').addEventListener('click',()=>{state.filters=emptyFilters();state.search='';state.selected={};state.page={};$('globalSearch').value='';renderFilters();renderSections()});
  $('openAi').addEventListener('click',()=>openDrawer('aiDrawer'));$('topAi').addEventListener('click',()=>openDrawer('aiDrawer'));
  $('closeAi').addEventListener('click',closeOverlays);$('closeDetail').addEventListener('click',closeOverlays);$('scrim').addEventListener('click',closeOverlays);
  $('menuButton').addEventListener('click',()=>{$('sidebar').classList.add('mobile-open');$('scrim').classList.add('show')});
  $('askForm').addEventListener('submit',e=>{e.preventDefault();ask($('askInput').value)});
  $('importReport').addEventListener('click',()=>$('fileInput').click());
  $('fileInput').addEventListener('change',async e=>{
    const file=e.target.files[0];if(!file)return;
    if(location.protocol==='file:'){toast('导入功能需先运行 python server.py');return}
    toast('正在读取工作簿…');
    try{
      const res=await fetch('/api/import',{method:'POST',headers:{'X-Filename':encodeURIComponent(file.name),'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},body:file});
      const data=await res.json();if(!res.ok)throw new Error(data.error||'导入失败');reports.push(data.report);state.report=reports.length-1;setSheet(0);toast('报告已导入');
    }catch(err){toast(err.message)}finally{e.target.value=''}
  });
  $('reportSwitch').addEventListener('click',()=>{
    if(reports.length===1){toast(location.protocol==='file:'?'当前离线文件包含一份报告。':'当前只有一份报告。可通过「导入工作簿」添加。');return}
    state.report=(state.report+1)%reports.length;setSheet(0);
  });
  document.addEventListener('keydown',e=>{
    // 表格行/卡片都支持键盘打开证据(contract §3: 证据展开须键盘可操作)
    if((e.key==='Enter'||e.key===' ')){
      const tr=e.target.closest&&e.target.closest('tr[data-row-table]');
      if(tr){e.preventDefault();showDetail(tr.dataset.rowTable,Number(tr.dataset.rowSource));return}
    }
    if((e.key==='Enter'||e.key===' ')&&e.target.matches('[role="button"][data-chart-table]')){e.preventDefault();e.target.click();return}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('globalSearch').focus()}if(e.key==='Escape')closeOverlays()});
  if(!reports.length){$('sections').innerHTML='<div class="empty-state">请先使用 convert_report.py 导入一份四模块报告。</div>';return}
  renderPrompts();render();detectServer();
})();
