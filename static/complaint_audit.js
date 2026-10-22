'use strict';

const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
let granularity = 'day';
let page = 1;
let pages = 1;
let activeBucket = '';
let activeRootCause = '';
let activeComplaintCategory = '';
let activeDescriptionTheme = '';
let activeDescriptionGroup = '';
let activeReasonDescription = '';
let dataVersion = null;
let searchTimer = null;

function query(includePage=false) {
  const params = new URLSearchParams({month: $('monthFilter').value});
  const map = {
    date_from: $('dateFrom').value, date_to: $('dateTo').value,
    review_status: $('reviewFilter').value, complaint_result: $('resultFilter').value,
    responsibility_subject: $('responsibilityFilter').value, agent: $('agentFilter').value,
    brand: $('brandFilter').value, q: $('keywordFilter').value.trim(), bucket: activeBucket,
    root_cause: activeRootCause, complaint_category: activeComplaintCategory,
    description_theme: activeDescriptionTheme, description_group: activeDescriptionGroup,
    reason_description: activeReasonDescription,
  };
  Object.entries(map).forEach(([key,value]) => { if (value) params.set(key,value); });
  if (includePage) { params.set('page',page); params.set('page_size','25'); }
  return params;
}

async function getJson(url, options={}) {
  const response = await fetch(url, {credentials:'same-origin', ...options});
  if (response.status === 401) { location.href = '/complaint-audit/login'; throw new Error('登录已失效'); }
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || '读取失败');
  return result;
}

function formatTime(value) { return value ? value.replace('T',' ').replace(/\+08:00$/,'') : '—'; }
function setText(id,value) { $(id).textContent = value; }
function classificationTone(value) {
  if (value === '客诉成立') return 'upheld';
  if (value === '客诉不成立') return 'not-upheld';
  if (value === '结果冲突') return 'danger';
  if (['未判定','待审核','审核退回'].includes(value)) return 'warning';
  return '';
}

function renderSync(sync) {
  $('syncStrip').className = `sync-strip ${sync.freshness || 'stale'}`;
  const state = sync.source_mode === 'excel' ? `Excel导入模式 · ${sync.time_basis}` : sync.running ? sync.message : sync.last_error ? `同步异常：${sync.last_error}` :
    sync.freshness === 'normal' ? '数据同步正常' : sync.freshness === 'delayed' ? '数据更新有延迟（超过90秒）' : '数据已过期（超过180秒）';
  setText('freshnessText',state);
  setText('sourceTime',formatTime(sync.source_updated_at));
  setText('localTime',formatTime(sync.local_synced_at));
  setText('syncDuration',sync.last_run?.duration_ms != null ? `${(sync.last_run.duration_ms/1000).toFixed(1)}秒` : '—');
  dataVersion = sync.data_version;
  $('syncButton').hidden = sync.source_mode === 'excel';
}

function renderMetrics(s) {
  setText('metricInspected',s.inspected); setText('metricApproved',s.approved);
  setText('metricUpheld',s.upheld); setText('metricNotUpheld',s.not_upheld);
  setText('metricRate',`${s.upheld_rate}%`); setText('metricUndetermined',s.undetermined);
  setText('metricPending',s.pending); setText('metricReturned',s.returned);
  setText('metricConflict',s.conflict); setText('metricAgents',s.agent_count);
  const delta = s.month_over_month.inspected_delta;
  setText('metricMom',s.month_over_month.comparable?`较${s.month_over_month.previous_month} ${delta>0?'+':''}${delta} 张`:'当前月份数据非完整可比区间');
  document.querySelectorAll('.metric-card[data-bucket]').forEach(card => card.classList.toggle('active',card.dataset.bucket===activeBucket));
}

function svgNode(name,attrs={}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg',name);
  Object.entries(attrs).forEach(([key,value]) => node.setAttribute(key,value));
  return node;
}

function renderTrend(rows) {
  const svg = $('trendChart');
  svg.innerHTML = '';
  const width = Math.max(760,rows.length*36+80), height=300, left=38, right=42, top=18, bottom=38;
  svg.setAttribute('viewBox',`0 0 ${width} ${height}`); svg.style.width=`${width}px`;
  const plotW=width-left-right, plotH=height-top-bottom;
  const maxCount=Math.max(1,...rows.flatMap(r=>[r.inspected,r.upheld,r.not_upheld]));
  for (let i=0;i<=4;i++) {
    const y=top+plotH*i/4;
    svg.append(svgNode('line',{x1:left,y1:y,x2:width-right,y2:y,class:'chart-grid'}));
    const label=svgNode('text',{x:left-7,y:y+3,'text-anchor':'end',class:'chart-axis-label'}); label.textContent=Math.round(maxCount*(4-i)/4); svg.append(label);
    const rate=svgNode('text',{x:width-right+7,y:y+3,class:'chart-axis-label'}); rate.textContent=`${Math.round((4-i)*25)}%`; svg.append(rate);
  }
  const x=index => left+(rows.length===1?plotW/2:index*plotW/(rows.length-1));
  const yCount=value => top+plotH-(value/maxCount*plotH);
  const yRate=value => top+plotH-(value/100*plotH);
  const series=[['inspected','#9ba9a4',yCount],['upheld','#087b6a',yCount],['not_upheld','#3f74a7',yCount],['upheld_rate','#dd6d3c',yRate]];
  series.forEach(([key,color,yFn]) => {
    const points=rows.map((r,i)=>`${x(i)},${yFn(r[key])}`).join(' ');
    svg.append(svgNode('polyline',{points,fill:'none',stroke:color,'stroke-width':key==='inspected'?1.8:2.4,'stroke-linejoin':'round','stroke-linecap':'round'}));
    rows.forEach((r,i)=>{
      const circle=svgNode('circle',{cx:x(i),cy:yFn(r[key]),r:3,fill:'white',stroke:color,'stroke-width':2,class:'chart-point'});
      const title=svgNode('title'); title.textContent=`${r.label} · ${key==='upheld_rate'?'成立率':key==='inspected'?'已稽查':key==='upheld'?'客诉成立':'客诉不成立'} ${r[key]}${key==='upheld_rate'?'%':'张'}`; circle.append(title);
      circle.addEventListener('click',()=>drillToDate(r.key)); svg.append(circle);
    });
  });
  rows.forEach((r,i)=>{
    if (rows.length>16 && i%Math.ceil(rows.length/10)!==0 && i!==rows.length-1) return;
    const label=svgNode('text',{x:x(i),y:height-14,'text-anchor':'middle',class:'chart-date'}); label.textContent=r.label; svg.append(label);
  });
}

function drillToDate(key) {
  const end=new Date(`${key}T00:00:00Z`);
  if (granularity==='week') end.setUTCDate(end.getUTCDate()+6);
  $('dateFrom').value=key;
  $('dateTo').value=end.toISOString().slice(0,10);
  activeBucket=''; page=1; refreshAll();
}

function renderFacts(facts) {
  $('factList').innerHTML=facts.map((fact,index)=>`<button type="button" class="fact-item" data-index="${index}"><span class="fact-number">${index+1}</span><span>${escapeHtml(fact.text)}</span></button>`).join('');
  $('factList').querySelectorAll('.fact-item').forEach(button=>button.addEventListener('click',()=>{
    const filters=facts[Number(button.dataset.index)].filters || {};
    applyDrilldown(filters);
  }));
}

function renderResponsibility(rows) {
  const max=Math.max(1,...rows.map(r=>r.count));
  $('responsibilityChart').innerHTML=rows.map((r,index)=>`<button type="button" class="responsibility-row" data-index="${index}"><span class="responsibility-name">${escapeHtml(r.subject)}</span><span class="responsibility-track"><i style="width:${r.count/max*100}%"></i></span><span class="responsibility-value"><b>${r.count}</b> · ${r.ratio}%</span></button>`).join('');
  $('responsibilityChart').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>applyDrilldown(rows[Number(button.dataset.index)].filters)));
}

function renderRankings(rows) {
  $('rankingBody').innerHTML=rows.length?rows.map(r=>`<tr><td><button class="agent-link" data-agent="${escapeHtml(r.agent)}">${escapeHtml(r.agent)}</button></td><td>${r.complained_count}</td><td><b>${r.formal_responsibility_count}</b></td><td>${r.upheld_rate}%</td><td>${r.decided_sample}</td></tr>`).join(''):'<tr><td colspan="5" class="empty-row">当前范围没有被投诉客服数据</td></tr>';
  $('rankingBody').querySelectorAll('.agent-link').forEach(button=>button.addEventListener('click',()=>applyDrilldown({agent:button.dataset.agent})));
}

function renderCauseRows(targetId, rows, key='name') {
  $(targetId).innerHTML=rows.length?rows.slice(0,10).map((row,index)=>`<button type="button" class="cause-row" data-index="${index}"><span>${escapeHtml(row[key])}</span><b>${row.count}</b><small>${row.ratio}%</small></button>`).join(''):'<div class="empty-row">当前范围没有正式分类数据</div>';
  $(targetId).querySelectorAll('.cause-row').forEach(button=>button.addEventListener('click',()=>applyDrilldown(rows[Number(button.dataset.index)].filters)));
}

function renderRootCauses(root) {
  renderCauseRows('rootMechanisms',root.mechanisms);
  renderCauseRows('manualCauses',root.manual_details.length?root.manual_details:root.manual_mechanisms);
  renderCauseRows('complaintCategories',root.categories);
  const q=root.quality;
  $('qualityNotes').innerHTML=`
    <div class="quality-note"><b>${q.result_conflicts}</b>结果冲突，未计入成立或不成立</div>
    <div class="quality-note"><b>${q.multiple_record_inspections}</b>张稽查单存在多条源记录</div>
    <div class="quality-note"><b>${q.responsibility_pending}</b>张成立单责任待确认</div>
    <div class="quality-note"><b>${q.missing_completion_time||0}</b>条已稽查记录缺少完成处理时间，未进入月份统计</div>`;
}

function renderNotUpheldCauses(analysis) {
  renderCauseRows('notUpheldBranches',analysis.branches);
  renderCauseRows('notUpheldDetails',analysis.details);
  renderCauseRows('notUpheldCategories',analysis.categories);
  $('notUpheldFacts').innerHTML=analysis.analyst_facts.length?analysis.analyst_facts.map((fact,index)=>`<button type="button" class="analyst-fact" data-index="${index}"><span>${escapeHtml(fact.text)}</span><small>查看明细 →</small></button>`).join(''):'<div class="empty-row">当前范围没有客诉不成立记录</div>';
  $('notUpheldFacts').querySelectorAll('.analyst-fact').forEach(button=>button.addEventListener('click',()=>applyDrilldown(analysis.analyst_facts[Number(button.dataset.index)].filters)));
}

function renderDescriptionAnalysis(analysis) {
  renderCauseRows('descriptionGroups',analysis.groups);
  renderCauseRows('descriptionOriginals',analysis.originals);
  $('descriptionFacts').innerHTML=analysis.facts.length?analysis.facts.map((fact,index)=>`<button type="button" class="analyst-fact" data-index="${index}"><span>${escapeHtml(fact.text)}</span><small>查看原单 →</small></button>`).join(''):'<div class="empty-row">当前范围没有可分析的客诉不成立原因描述</div>';
  $('descriptionFacts').querySelectorAll('.analyst-fact').forEach(button=>button.addEventListener('click',()=>applyDrilldown(analysis.facts[Number(button.dataset.index)].filters)));
  $('descriptionThemes').innerHTML=analysis.themes.length?analysis.themes.map((row,index)=>`<button type="button" class="theme-row" data-index="${index}">
    <span class="theme-head"><b>${escapeHtml(row.name)}</b><em>${row.count} · ${row.ratio}%</em></span>
    <small>${escapeHtml(row.group)}</small>
    <span class="theme-examples">${row.examples.slice(0,2).map(item=>`<i>“${escapeHtml(item.text)}”</i>`).join('')}</span>
  </button>`).join(''):'<div class="empty-row">当前范围没有原因描述主题</div>';
  $('descriptionThemes').querySelectorAll('.theme-row').forEach(button=>button.addEventListener('click',()=>applyDrilldown(analysis.themes[Number(button.dataset.index)].filters)));
  setText('descriptionMethod',analysis.method);
}

function updateOptions(options) {
  const apply=(id,rows)=>{
    const select=$(id), current=select.value;
    select.innerHTML='<option value="">全部</option>'+rows.map(value=>`<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
    if ([...select.options].some(option=>option.value===current)) select.value=current;
  };
  apply('responsibilityFilter',options.responsibilities); apply('agentFilter',options.agents); apply('brandFilter',options.brands);
}

function applyDrilldown(filters={}) {
  if (filters.bucket !== undefined) activeBucket=filters.bucket;
  if (filters.root_cause !== undefined) { activeRootCause=filters.root_cause; activeDescriptionTheme=''; activeDescriptionGroup=''; activeReasonDescription=''; }
  if (filters.complaint_category !== undefined) { activeComplaintCategory=filters.complaint_category; activeDescriptionTheme=''; activeDescriptionGroup=''; activeReasonDescription=''; }
  if (filters.description_theme !== undefined) { activeDescriptionTheme=filters.description_theme; activeDescriptionGroup=''; activeReasonDescription=''; }
  if (filters.description_group !== undefined) { activeDescriptionGroup=filters.description_group; activeDescriptionTheme=''; activeReasonDescription=''; }
  if (filters.reason_description !== undefined) { activeReasonDescription=filters.reason_description; activeDescriptionTheme=''; activeDescriptionGroup=''; }
  if (filters.responsibility_subject !== undefined) $('responsibilityFilter').value=filters.responsibility_subject;
  if (filters.agent !== undefined) $('agentFilter').value=filters.agent;
  page=1; $('clearDrilldown').hidden=false; refreshAll();
  document.querySelector('.records-panel').scrollIntoView({behavior:'smooth',block:'start'});
}

async function loadSummary() {
  const params=query(); params.set('granularity',granularity);
  const result=await getJson(`/api/complaint-audit/summary?${params}`);
  const data=result.data;
  renderMetrics(data.summary); renderTrend(data.trend); renderFacts(data.facts);
  renderResponsibility(data.responsibility); renderRankings(data.rankings); renderRootCauses(data.root_causes); renderNotUpheldCauses(data.not_upheld_causes); renderDescriptionAnalysis(data.not_upheld_descriptions); updateOptions(data.options); renderSync(data.sync);
}

async function loadRecords() {
  const result=await getJson(`/api/complaint-audit/records?${query(true)}`);
  const data=result.data; page=data.page; pages=data.pages;
  setText('recordCount',`当前筛选 ${data.total} 张唯一稽查单`);
  $('recordBody').innerHTML=data.records.length?data.records.map(r=>`<tr>
    <td><a class="record-id" href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">${escapeHtml(r.inspect_id)}</a><span class="sub-id">工单 ${escapeHtml(r.ticket_id||'—')}</span>${r.multiple_records?`<span class="pill warning">${r.record_count}条关联记录</span>`:''}</td>
    <td>${escapeHtml(r.shop_name||r.shop_id||'—')}<span class="cell-muted">${escapeHtml(r.brand||'未填写品牌')}</span></td>
    <td>${escapeHtml(formatTime(r.inspect_completed_at))}<span class="cell-muted">${escapeHtml(r.inspect_status)}</span></td>
    <td class="cell-copy">${escapeHtml(r.inspect_result)}<span class="cell-muted">异常：${escapeHtml(r.abnormal_state)} · ${escapeHtml(r.abnormal_type)}</span></td>
    <td><span class="pill ${classificationTone(r.classification)}">${escapeHtml(r.classification)}</span><span class="cell-muted">${escapeHtml(r.review_status||'未填写审核状态')}</span></td>
    <td>${escapeHtml(r.complained_agent||'未填写')}</td>
    <td class="cell-copy"><span class="pill">${escapeHtml(r.responsibility_subject)}</span><span class="cell-muted">节点 ${escapeHtml(r.reason_node_id||'未填写')}</span>${escapeHtml(r.reason_path||'未填写完整路径')}</td>
    <td class="cell-copy">${escapeHtml(r.reason_description||'未填写')}</td>
    <td>${escapeHtml(formatTime(r.source_updated_at))}<span class="cell-muted">审核 ${escapeHtml(formatTime(r.reviewed_at))}</span></td>
    <td><button class="history-button" data-inspect="${escapeHtml(r.inspect_id)}">历史</button></td>
  </tr>`).join(''):'<tr><td colspan="10" class="empty-row">当前筛选条件下没有稽查单</td></tr>';
  $('recordBody').querySelectorAll('.history-button').forEach(button=>button.addEventListener('click',()=>openHistory(button.dataset.inspect)));
  setText('pageLabel',`第 ${page} / ${pages} 页`); $('prevPage').disabled=page<=1; $('nextPage').disabled=page>=pages;
  const conditions=[activeBucket,activeRootCause,activeComplaintCategory,activeDescriptionTheme,activeDescriptionGroup,activeReasonDescription,$('reviewFilter').value,$('resultFilter').value,$('responsibilityFilter').value,$('agentFilter').value,$('brandFilter').value,$('keywordFilter').value,$('dateFrom').value,$('dateTo').value].filter(Boolean);
  setText('filterStatus',conditions.length?`已启用 ${conditions.length} 项筛选/下钻条件`:'默认展示本月全部已稽查单');
  $('clearDrilldown').hidden=!(activeBucket||activeRootCause||activeComplaintCategory||activeDescriptionTheme||activeDescriptionGroup||activeReasonDescription);
}

async function openHistory(inspectId) {
  const dialog=$('historyDialog'); setText('historyTitle',`稽查单 ${inspectId} · 变更历史`);
  $('historyBody').innerHTML='<div class="empty-row">正在读取…</div>'; dialog.showModal();
  try {
    const result=await getJson(`/api/complaint-audit/records/${encodeURIComponent(inspectId)}/history`);
    const rows=result.data.versions;
    $('historyBody').innerHTML=rows.length?rows.map(version=>`<section class="history-version"><h3>${escapeHtml(formatTime(version.changed_at))} · 源记录 ${escapeHtml(version.source_record_id)} · 版本 ${version.version_no}</h3>${version.changes.map(change=>`<div class="change-row"><b>${escapeHtml(change.label)}</b><span class="change-before">${escapeHtml(change.before??'—')}</span><span class="change-arrow">→</span><span class="change-after">${escapeHtml(change.after??'—')}</span></div>`).join('')}</section>`).join(''):'<div class="empty-row">暂无变更版本</div>';
  } catch (error) { $('historyBody').innerHTML=`<div class="login-error">${escapeHtml(error.message)}</div>`; }
}

async function refreshAll() {
  try { await Promise.all([loadSummary(),loadRecords()]); }
  catch (error) { setText('filterStatus',`读取失败：${error.message}`); }
}

async function manualSync() {
  $('syncButton').disabled=true; $('syncButton').textContent='同步已启动';
  try { await getJson('/api/complaint-audit/sync',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'}); }
  catch (error) { alert(error.message); }
  finally { setTimeout(()=>{$('syncButton').disabled=false;$('syncButton').textContent='立即同步';},2500); }
}

async function importExcel(file) {
  const status=$('importStatus');
  status.hidden=false; status.className='import-status'; status.textContent=`正在导入 ${file.name}…`;
  const body=new FormData(); body.append('file',file);
  try {
    const result=await getJson('/api/complaint-audit/import',{method:'POST',headers:{'X-CSRF-Token':csrfToken},body});
    const d=result.data;
    status.textContent=`导入完成：读取 ${d.received} 条，更新 ${d.changed} 条，跳过 ${d.skipped} 条；${d.time_basis}。`;
    page=1; await refreshAll();
  } catch (error) {
    status.className='import-status error'; status.textContent=error.message;
  } finally { $('importFile').value=''; }
}

async function pollStatus() {
  try {
    const result=await getJson('/api/complaint-audit/sync/status');
    const previousVersion=dataVersion;
    renderSync(result.data);
    if (previousVersion!==null && result.data.data_version!==previousVersion) refreshAll();
  } catch (_) {}
}

document.querySelectorAll('.metric-card[data-bucket]').forEach(card=>card.addEventListener('click',()=>{ activeBucket=activeBucket===card.dataset.bucket?'':card.dataset.bucket; page=1; refreshAll(); }));
document.querySelectorAll('.segmented button').forEach(button=>button.addEventListener('click',()=>{ granularity=button.dataset.granularity; document.querySelectorAll('.segmented button').forEach(item=>item.classList.toggle('active',item===button)); loadSummary(); }));
['monthFilter','dateFrom','dateTo','reviewFilter','resultFilter','responsibilityFilter','agentFilter','brandFilter'].forEach(id=>$(id).addEventListener('change',()=>{ if(id==='monthFilter'){$('dateFrom').value='';$('dateTo').value='';} activeBucket='';activeRootCause='';activeComplaintCategory='';activeDescriptionTheme='';activeDescriptionGroup='';activeReasonDescription='';page=1;refreshAll(); }));
$('keywordFilter').addEventListener('input',()=>{ clearTimeout(searchTimer); searchTimer=setTimeout(()=>{page=1;refreshAll();},350); });
$('resetFilters').addEventListener('click',()=>{ ['dateFrom','dateTo','reviewFilter','resultFilter','responsibilityFilter','agentFilter','brandFilter','keywordFilter'].forEach(id=>$(id).value='');activeBucket='';activeRootCause='';activeComplaintCategory='';activeDescriptionTheme='';activeDescriptionGroup='';activeReasonDescription='';page=1;refreshAll(); });
$('clearDrilldown').addEventListener('click',()=>{activeBucket='';activeRootCause='';activeComplaintCategory='';activeDescriptionTheme='';activeDescriptionGroup='';activeReasonDescription='';page=1;refreshAll();});
$('prevPage').addEventListener('click',()=>{if(page>1){page--;loadRecords();}}); $('nextPage').addEventListener('click',()=>{if(page<pages){page++;loadRecords();}});
$('syncButton').addEventListener('click',manualSync); $('closeHistory').addEventListener('click',()=>$('historyDialog').close());
$('importFile').addEventListener('change',event=>{const file=event.target.files[0];if(file)importExcel(file);});
$('historyDialog').addEventListener('click',event=>{if(event.target===$('historyDialog'))$('historyDialog').close();});

refreshAll();
setInterval(pollStatus,10000);
