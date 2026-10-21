'use strict';

const colors = {
  '多付/漏付': '#e76f3c', '介入问题': '#006b63', '批量核实': '#3678a8',
  '回访工单': '#d9a11e', '其他': '#7968a8'
};
let dashboardData = null;
let rootPage = 1;
let rootPages = 1;
let rootSearchTimer = null;
let selectedCategoryIndex = 0;
let selectedKaCategory = '全部';
let merchantRankingData = null;
let merchantRankingRevision = null;
let merchantRankingRequest = 0;
let lastAutoSyncFinishedAt = null;
let followsLatestDate = true;
const $ = (id) => document.getElementById(id);
const isPublicDashboard = window.TICKET_PUBLIC === true;
let publicManifest = null;
let publicSnapshot = null;
let publicLoadedRevision = null;
let publicRequest = 0;
const signed = (n) => `${n > 0 ? '+' : ''}${n}`;
const signClass = (n) => n > 0 ? 'positive' : n < 0 ? 'negative' : '';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

async function loadDashboard() {
  if (isPublicDashboard) return loadPublicDashboard();
  const date = $('datePicker').value;
  const windowSize = $('windowPicker').value;
  $('statusLine').className = 'status-line';
  $('statusLine').textContent = '正在计算趋势、异常和问题主题…';
  try {
    const response = await fetch(`/api/dashboard?date=${encodeURIComponent(date)}&window=${windowSize}`);
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || '读取失败');
    dashboardData = result.data;
    render(result.data, result.sync, result.realtime);
    rootPage = 1;
    loadRootCauses();
  } catch (error) {
    $('statusLine').className = 'status-line error';
    $('statusLine').textContent = error.message;
  }
}

async function loadMerchantRanking() {
  const requestId = ++merchantRankingRequest;
  const list = $('merchantRankingList');
  try {
    let data;
    if (isPublicDashboard) {
      if (!merchantRankingData || merchantRankingRevision !== publicLoadedRevision) {
        data = await fetchPublicJson('data/merchant-ranking.json');
      } else {
        data = merchantRankingData;
      }
    } else {
      const response = await fetch('/api/merchant-ranking');
      const result = await response.json();
      if (!result.ok) throw new Error(result.error || '读取商家排行失败');
      data = result.data;
    }
    if (requestId !== merchantRankingRequest) return;
    merchantRankingData = data;
    merchantRankingRevision = publicLoadedRevision;
    renderMerchantRanking(data);
  } catch (error) {
    if (requestId !== merchantRankingRequest) return;
    $('merchantRankingCoverage').className = 'merchant-ranking-coverage uncertain';
    $('merchantRankingCoverage').textContent = error.message;
    if (!merchantRankingData) list.innerHTML = '<p class="empty">排行数据暂时无法读取</p>';
  }
}

function rootQuery() {
  const pageSize = window.innerWidth <= 620 ? 10 : 25;
  const params = new URLSearchParams({date: $('datePicker').value, page: rootPage, page_size: pageSize});
  const values = {
    q: $('rootSearch').value.trim(), layer: $('rootLayer').value,
    status: $('rootStatus').value, ka: $('rootKa').value
  };
  Object.entries(values).forEach(([key,value]) => { if (value) params.set(key,value); });
  if ($('rootUnresolved').checked) params.set('unresolved','1');
  return params;
}

async function loadRootCauses() {
  if (isPublicDashboard) return loadPublicRootCauses();
  $('rootStatusLine').className = 'root-status-line';
  $('rootStatusLine').textContent = '正在读取逐单根因和稽查证据…';
  try {
    const response = await fetch(`/api/root-causes?${rootQuery().toString()}`);
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || '逐单根因读取失败');
    renderRootCauses(result.data, result.evidence_sync);
  } catch (error) {
    $('rootStatusLine').className = 'root-status-line error';
    $('rootStatusLine').textContent = error.message;
    $('rootTable').innerHTML = '<tr><td colspan="6" class="empty">逐单根因暂时无法读取</td></tr>';
  }
}

function statusTone(status) {
  if (status === '已确认异常') return 'danger';
  if (status === '已核实无异常') return 'success';
  if (status === '流程事项') return 'info';
  return 'pending';
}

function renderRootCauses(data, evidenceSync) {
  rootPage = data.page; rootPages = data.pages;
  const statusMap = Object.fromEntries(data.statuses.map(x => [x.name,x.count]));
  $('rootSummary').innerHTML = `<div><span>证据覆盖</span><strong>${data.evidence_ratio}%</strong><small>${data.evidence_ready} / ${data.total} 单</small></div>
    <div><span>已确认异常</span><strong>${statusMap['已确认异常']||0}</strong><small>需继续看是否成损</small></div>
    <div><span>已核实无异常</span><strong>${statusMap['已核实无异常']||0}</strong><small>回放暂未发现异常</small></div>
    <div><span>待核实</span><strong>${statusMap['待核实']||0}</strong><small>缺证据，暂不判责</small></div>`;
  const syncText = evidenceSync?.running ? evidenceSync.message : `共 ${data.total} 单，当前筛选 ${data.filtered_total} 单`;
  $('rootStatusLine').textContent = `${syncText} · A为有文字结论，B为仅有状态，C为仅有商家描述`;
  $('rootTable').innerHTML = data.records.length ? data.records.map(r => `<tr>
    <td class="identity-cell" data-label="工单 / 店铺"><a class="ticket-link" href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">${escapeHtml(r.ticket_id)}</a><span>${escapeHtml(r.shop_id)}</span><small>${escapeHtml(r.brand)}</small><small>${escapeHtml(r.created_at.slice(11,16))} · ${r.is_ka?'KA':'非KA'}</small></td>
    <td data-label="商家主张"><p class="cell-copy claim-copy">${escapeHtml(r.merchant_claim)}</p><span class="category-tag">${escapeHtml(r.category)}</span></td>
    <td data-label="现场实际行为"><span class="status-badge ${statusTone(r.verification_status)}">${escapeHtml(r.verification_status)}</span><p class="cell-copy behavior-copy">${escapeHtml(r.customer_behavior)}</p></td>
    <td data-label="根因 / 责任"><div class="layer-tag">${escapeHtml(r.root_layer)}</div><strong class="root-cause">${escapeHtml(r.root_cause)}</strong><p class="cell-note">${escapeHtml(r.responsibility)}</p></td>
    <td data-label="证据 / 缺口"><div class="evidence-line"><b>${escapeHtml(r.evidence_grade)}</b><span>${r.confidence}%</span></div><p class="cell-note evidence-summary">${escapeHtml(r.evidence_summary)}</p><div class="gap-list">${r.evidence_gap.map(g => `<span>${escapeHtml(g)}</span>`).join('')}</div></td>
    <td data-label="损失 / 下一步"><div class="loss-state">${escapeHtml(r.loss_status)}</div><p class="cell-note">${escapeHtml(r.loss_summary)}</p><p class="pipeline-note">流程：${escapeHtml(r.pipeline_status)}</p><div class="next-action"><b>下一步</b>${escapeHtml(r.next_action)}</div></td>
  </tr>`).join('') : '<tr><td colspan="6" class="empty">当前筛选条件下没有工单</td></tr>';
  $('rootPage').textContent = `第 ${data.page} / ${data.pages} 页 · ${data.filtered_total} 单`;
  $('rootPrev').disabled = data.page <= 1;
  $('rootNext').disabled = data.page >= data.pages;
}

function render(data, sync, realtime) {
  const s = data.summary;
  $('sourceBreakdown').innerHTML = `<span>天润工单：<b>${Number(s.tianrun_count || 0).toLocaleString()}</b></span><span>商家APP发起：<b>${Number(s.merchant_app_count || 0).toLocaleString()}</b></span>`;
  $('totalMetric').textContent = s.total.toLocaleString();
  $('totalCompare').className = signClass(s.delta);
  $('totalCompare').textContent = `较前一日 ${signed(s.delta)}（${signed(s.pct)}%），近7日均值 ${s.avg7}`;
  const driver = s.top_driver;
  $('driverMetric').textContent = driver ? driver.category : '—';
  $('driverCompare').className = driver ? signClass(driver.delta) : '';
  $('driverCompare').textContent = driver ? `较前一日 ${signed(driver.delta)} 单；天润净变化 ${signed(s.tianrun_delta || 0)} 单` : '暂无数据';
  $('anomalyMetric').textContent = s.record_anomaly_count;
  $('anomalyCompare').textContent = `${s.risk_ticket_count} 张敏感风险工单 · ${s.growth_anomaly_count} 个类型增长异常`;
  $('kaMetric').textContent = `${data.ka.ratio}%`;
  $('kaCompare').className = signClass(data.ka.ratio_delta);
  $('kaCompare').textContent = `${data.ka.count} 单，较前一日 ${signed(data.ka.ratio_delta)} 个百分点`;
  lastAutoSyncFinishedAt = realtime?.last_finished_at || lastAutoSyncFinishedAt;
  const syncText = data.last_sync ? `本地数据更新：${data.last_sync.finished_at.replace('T',' ')} · ${data.last_sync.message}` : '尚未同步数据';
  const cutoffText = data.cutoff_time ? ` · 当日与历史均统计截至 ${data.cutoff_time}` : '';
  const refreshText = realtime?.next_run_at ? ` · 自动同步：每5分钟，下一次 ${realtime.next_run_at.slice(11,16)}` : ' · 自动同步：每5分钟';
  $('statusLine').textContent = sync?.running ? sync.message : `${data.selected_date} 分析完成${cutoffText} · ${syncText}${refreshText}`;
  renderTrend(data.daily_trend);
  renderCompletionRate(data.completion_rate);
  renderMerchantApp(data.merchant_app);
  loadMerchantRanking();
  renderCategoryInsights(data.category_insights);
  renderAlerts(data.growth_anomalies, data.risk_summary, data.risk_records, data.operational_anomaly_summary, data.merchant_app?.risk_records || []);
  renderTopics(data.topics);
  renderKA(data.ka);
  renderAnomalyTable(data.anomaly_records);
}

function renderMerchantRanking(data) {
  const coverage = data.coverage || {};
  const complete = coverage.complete;
  const rankRows = data.ranking || [];
  const search = $('merchantRankingSearch').value.trim().toLowerCase();
  const visibleRows = rankRows.filter(row => `${row.brand} ${row.shop_id}`.toLowerCase().includes(search));
  const summary = $('merchantRankingSummary');
  summary.innerHTML = `<div><span>纳入排行商家</span><strong>${Number(data.merchant_count || 0).toLocaleString()}</strong><small>${data.period_start} 至 ${data.period_end}</small></div>
    <div><span>可归集天润</span><strong>${Number(data.tianrun_count || 0).toLocaleString()}</strong><small>按天润工单 ID 计数</small></div>
    <div><span>可归集商家 APP</span><strong>${Number(data.merchant_app_count || 0).toLocaleString()}</strong><small>按 APP 稽查单 ID 计数</small></div>
    <div><span>可归集合计</span><strong>${Number(data.total_count || 0).toLocaleString()}</strong><small>跨入口不去重</small></div>`;
  const coverageText = complete
    ? `已完成双来源逐日查询 90 / 90 天 · ${data.period_start} 至 ${data.period_end}，截止日持续更新；统计范围为源看板可查记录。`
    : `数据覆盖不足：已确认双来源 ${coverage.fully_synced_days || 0} / 90 天，连续覆盖最近 ${coverage.contiguous_days_to_end || 0} 天；首个未确认日期 ${coverage.first_missing_date || '—'}。未同步日期的“0单”不作为已核实数据。`;
  $('merchantRankingCoverage').className = `merchant-ranking-coverage ${complete ? 'complete' : 'uncertain'}`;
  $('merchantRankingCoverage').textContent = coverageText;
  $('merchantRankingDefinition').textContent = `${data.definition} 无店铺ID未匹配：天润 ${data.unmatched_tianrun_count || 0} 单，APP ${data.unmatched_merchant_app_count || 0} 单。当前显示前 ${rankRows.length} 名，搜索仅在本榜内。`;
  const openShops = new Set([...$('merchantRankingList').querySelectorAll('details[open]')].map(row => row.dataset.shopId));
  $('merchantRankingList').innerHTML = visibleRows.length ? visibleRows.map(row => `<details class="merchant-rank-row" data-shop-id="${escapeHtml(row.shop_id)}" ${openShops.has(String(row.shop_id)) ? 'open' : ''}>
    <summary><span class="merchant-rank-number">${row.rank}</span><span class="merchant-rank-identity"><strong>${escapeHtml(row.brand || '未匹配品牌')}</strong><small>店铺 ID ${escapeHtml(row.shop_id)}${row.is_ka ? ' · KA' : ''}</small></span>
    <strong class="merchant-rank-total">${row.total}</strong><span class="merchant-rank-tianrun">${row.tianrun_count}</span><span class="merchant-rank-app">${row.merchant_app_count}</span><span class="merchant-rank-week">${row.last_7_count}</span><span class="merchant-rank-date">${escapeHtml(row.last_ticket_at || '—')}</span><span class="merchant-rank-toggle" aria-hidden="true"></span></summary>
    <div class="merchant-rank-tickets">${row.tickets.map(ticket => `<article class="merchant-rank-ticket"><div><a href="${escapeHtml(ticket.source_url)}" target="_blank" rel="noopener">${escapeHtml(ticket.source_label)} ${escapeHtml(ticket.ticket_id)}</a><span>${escapeHtml(ticket.category)}</span></div><p>${escapeHtml(ticket.description) || '未填写问题描述'}</p><small>${escapeHtml(ticket.created_at)} · ${escapeHtml(ticket.status)}</small></article>`).join('')}</div>
  </details>`).join('') : '<p class="empty">没有匹配的商家；如果结果为空，请检查所选日期范围内数据是否已同步。</p>';
}

function renderCompletionRate(rate) {
  if (!rate) return;
  const previous = rate.previous || {};
  const delta = Number(rate.rate_24h_delta || 0);
  const previousLabel = previous.completed ? `${previous.rate_24h}% · ${delta >= 0 ? '+' : ''}${delta} 个百分点` : '暂无可比数据';
  $('completionSummary').innerHTML = `<div class="completion-main"><span>总体24小时完成率</span><strong>${rate.rate_24h}%</strong><small class="${previous.completed ? signClass(delta) : ''}">前一周期 ${previousLabel}</small></div>
    <div><span>24小时内完成</span><strong>${Number(rate.within_24h || 0).toLocaleString()}</strong><small>已完结有效样本 ${Number(rate.completed || 0).toLocaleString()}</small></div>
    <div><span>48小时内完成率</span><strong>${rate.rate_48h}%</strong><small>${Number(rate.within_48h || 0).toLocaleString()} 单</small></div>
    <div><span>72小时内完成率</span><strong>${rate.rate_72h}%</strong><small>${Number(rate.within_72h || 0).toLocaleString()} 单</small></div>`;
  $('completionCategories').innerHTML = (rate.categories || []).map(item => { const prior = (previous.categories || []).find(p => p.category === item.category); const priorText = prior?.completed ? `${prior.rate_24h}%` : '暂无可比'; return `<article class="completion-category"><div class="completion-category-head"><strong>${escapeHtml(item.category)}</strong><span class="${prior?.completed ? signClass((item.rate_24h || 0) - prior.rate_24h) : ''}">${item.rate_24h}%</span></div><div class="completion-category-grid"><span>24小时内 <b>${item.within_24h}</b> / ${item.completed}</span><span>48小时 <b>${item.rate_48h}%</b></span><span>72小时 <b>${item.rate_72h}%</b></span></div><small>前一周期24小时率 ${priorText} · 排除 ${item.excluded} 单</small></article>`; }).join('');
  const excluded = rate.excluded_breakdown || [];
  $('completionQuality').innerHTML = `<span>${escapeHtml(rate.source_definition || '')}</span>${excluded.length ? `<span class="completion-excluded">未纳入分母：${excluded.map(item => `${escapeHtml(item.reason)} ${item.count} 单`).join(' · ')}</span>` : '<span>本周期没有发现需要排除的完结时间数据</span>'}`;
}

function renderTrend(days) {
  const categories = Object.keys(colors);
  $('trendLegend').innerHTML = `${categories.map(c => `<span><i style="background:${colors[c]}"></i>${c}</span>`).join('')}<span><i class="app-legend-swatch"></i>商家APP发起</span>`;
  const max = Math.max(1, ...days.map(d => d.total));
  $('trendChart').style.setProperty('--bars', days.length);
  $('trendChart').style.minWidth = `${Math.max(640, days.length * 58)}px`;
  $('trendChart').innerHTML = days.map(day => {
    const segments = categories.map(cat => {
      const height = day.total ? day.categories[cat] / day.total * 100 : 0;
      return `<span class="bar-seg" style="--seg-height:${height}%;background:${colors[cat]}" title="${cat} ${day.categories[cat]}单"></span>`;
    }).join('') + `<span class="bar-seg app-bar-seg" style="--seg-height:${day.total ? day.merchant_app_count / day.total * 100 : 0}%" title="商家APP发起 ${day.merchant_app_count}单"></span>`;
    const sourceTitle = `天润 ${day.tianrun_count} 单 · 商家APP ${day.merchant_app_count} 单`;
    return `<div class="day-bar"><span class="bar-value">${day.total}</span><div class="bar-stack" style="--height:${day.total / max * 215}px" title="${day.date}：${day.total}单（${sourceTitle}）">${segments}</div><span class="day-label">${day.label}</span></div>`;
  }).join('');
}

function renderMerchantApp(app) {
  app = app || {};
  const syncedAt = app.day_synced_at ? app.day_synced_at.replace('T', ' ') : '未同步';
  const queueAt = app.open_synced_at ? app.open_synced_at.replace('T', ' ') : '';
  const queueTimestamp = app.open_synced_at ? Date.parse(/[Zz]|[+-]\d\d:\d\d$/.test(app.open_synced_at) ? app.open_synced_at : `${app.open_synced_at}+08:00`) : NaN;
  const queueStale = !Number.isFinite(queueTimestamp) || Date.now() - queueTimestamp > 15 * 60 * 1000;
  const queueStatus = !app.open_synced_at ? '未成功读取当前未完成队列' : queueStale ? `队列数据可能过旧，最近同步 ${queueAt}` : `当前队列已核对 · ${queueAt}`;
  $('merchantAppSummary').innerHTML = `<div class="merchant-app-kpis">
    <div><span>所选日期新增</span><strong>${Number(app.count || 0).toLocaleString()}</strong><small>前日 ${Number(app.previous || 0).toLocaleString()} · <b class="${signClass(app.delta || 0)}">${signed(app.delta || 0)}</b></small></div>
    <div><span>当前未完成</span><strong>${Number(app.current_pending_count || 0).toLocaleString()}</strong><small>不受所选分析日期限制</small></div>
    <div class="${app.overdue_24h_count ? 'is-overdue' : ''}"><span>超24小时仍未稽查</span><strong>${Number(app.overdue_24h_count || 0).toLocaleString()}</strong><small>只统计当前未完成且状态未稽查</small></div>
    <div><span>所选日期数据同步</span><strong class="sync-time">${escapeHtml(syncedAt)}</strong><small>${app.loaded ? 'APP新增明细已读取' : '该日期尚未确认完成同步'}</small></div>
  </div>
  <div class="merchant-sync-note ${queueStale ? 'stale' : 'fresh'}"><b>${queueStale ? '需核实队列时效' : '未完成队列同步正常'}</b><span>${escapeHtml(queueStatus)}</span><small>新增量按入口计数，同一事项若同时进入天润与APP会分别计数；超时从APP创建时间起算。</small></div>`;

  const overdue = app.overdue_24h_records || [];
  $('merchantOverdueCount').textContent = app.open_synced_at && !queueStale ? `${overdue.length} 单` : '待确认';
  $('merchantOverdueList').innerHTML = overdue.length ? overdue.map(row => `<article class="merchant-overdue-ticket">
    <div class="merchant-overdue-ticket-head"><div><a href="${escapeHtml(row.source_url)}" target="_blank" rel="noopener">稽查单 ${escapeHtml(row.inspection_id)}</a><span>事件 ${escapeHtml(row.event_id)} · 店铺 ${escapeHtml(row.shop_id)}</span></div><strong>${escapeHtml(row.age_hours ?? '—')} 小时</strong></div>
    <p>${escapeHtml(row.description)}</p><div class="merchant-ticket-meta"><span>${escapeHtml(row.brand)}</span><span>${escapeHtml(row.inspect_status)}</span><span>创建 ${escapeHtml(row.created_at)}</span>${row.risk_hits?.map(hit => `<b class="risk-tag">${escapeHtml(hit.label)}</b>`).join('') || ''}</div>
  </article>`).join('') : app.open_synced_at && !queueStale
    ? '<div class="merchant-overdue-empty"><strong>0 单</strong><span>本次已同步的当前未完成队列中，没有创建满24小时且仍未稽查的商家APP稽查单。</span></div>'
    : '<div class="merchant-overdue-empty uncertain"><strong>暂不能确认</strong><span>当前未完成队列同步缺失或已超过15分钟，不能据此判断超24小时工单为0。</span></div>';

  $('merchantPendingList').innerHTML = (app.pending_records || []).length ? app.pending_records.map(row => `<article class="merchant-pending-ticket"><div><a href="${escapeHtml(row.source_url)}" target="_blank" rel="noopener">稽查单 ${escapeHtml(row.inspection_id)}</a><strong class="${row.overdue_24h ? 'overdue-label' : ''}">${escapeHtml(row.age_hours ?? '—')} 小时 · ${escapeHtml(row.inspect_status)}</strong></div><p>${escapeHtml(row.description)}</p><small>店铺 ${escapeHtml(row.shop_id)} · ${escapeHtml(row.brand)} · 创建 ${escapeHtml(row.created_at)}</small></article>`).join('') : '<p class="empty">本次队列没有未完成单据；请结合上方同步状态核实。</p>';
  const records = app.records || [];
  $('merchantRecordHint').textContent = `${records.length} 单 · 未完成优先，同状态按创建时间排列`;
  $('merchantTypeBreakdown').innerHTML = (app.type_breakdown || []).map(item => `<span>${escapeHtml(item.name)} <b>${item.count}</b><small>前日${item.previous} · ${signed(item.delta)}</small></span>`).join('') || '<span>暂无结果类型数据</span>';
  $('merchantRecordTable').innerHTML = records.length ? records.map(row => `<tr>
    <td><a class="merchant-id" href="${escapeHtml(row.source_url)}" target="_blank" rel="noopener">${escapeHtml(row.inspection_id)}</a><small>事件 ${escapeHtml(row.event_id)}<br>店铺 ${escapeHtml(row.shop_id)}</small></td>
    <td>${escapeHtml(row.brand)}${row.is_ka ? '<small class="ka-mark">KA</small>' : ''}</td>
    <td class="merchant-description">${escapeHtml(row.description)}${row.risk_hits?.length ? `<div class="merchant-risk-tags">${row.risk_hits.map(hit => `<b>${escapeHtml(hit.label)}</b>`).join('')}</div>` : ''}</td>
    <td><span class="merchant-state ${row.is_open ? 'pending' : ''}">${escapeHtml(row.inspect_status)}</span></td>
    <td>${escapeHtml(row.abnormal_state)}<small>${escapeHtml(row.abnormal_type)}</small></td>
    <td>${escapeHtml(row.created_at)}<small>${row.completed_at ? `完成 ${escapeHtml(row.completed_at)}` : '尚未完成'}</small></td>
    <td>${row.is_open ? `${escapeHtml(row.age_hours ?? '—')} 小时` : '—'}${row.overdue_24h ? '<small class="overdue-label">已超24小时</small>' : ''}</td>
  </tr>`).join('') : '<tr><td colspan="7" class="empty">所选日期暂无商家APP发起的稽查单。</td></tr>';
}

function renderCategoryInsights(rows) {
  if (!rows.length) return;
  if (selectedCategoryIndex >= rows.length) selectedCategoryIndex = 0;
  $('categoryCards').innerHTML = rows.map((r,i) => `<button type="button" class="category-card ${i===selectedCategoryIndex?'active':''}" data-category-index="${i}">
    <span>${escapeHtml(r.category)}</span><strong>${r.current}</strong><small class="${signClass(r.delta)}">较前日 ${signed(r.delta)} 单（${signed(r.pct)}%）</small><em>占当日 ${r.share}%</em>
  </button>`).join('');
  $('categoryCards').querySelectorAll('.category-card').forEach(card => card.addEventListener('click', () => {
    selectedCategoryIndex = Number(card.dataset.categoryIndex);
    renderCategoryInsights(rows);
  }));
  renderCategoryDetail(rows[selectedCategoryIndex]);
}

function renderCategoryDetail(row) {
  const topDemand = row.demands[0];
  $('categoryName').textContent = row.category;
  $('categoryConclusion').textContent = topDemand ? `今日主要诉求：${topDemand.demand}（${topDemand.count}单，占${topDemand.ratio}%）` : '今日暂无工单';
  $('categoryMetrics').innerHTML = `<span>今日 <b>${row.current}</b></span><span>前日 <b>${row.previous}</b></span><span class="${signClass(row.delta)}">增减 <b>${signed(row.delta)}</b></span><span>敏感风险 <b>${row.sensitive_risk_count}</b></span><span>处理卡点 <b>${row.processing_anomaly_count}</b></span>`;
  $('categoryCommon').innerHTML = row.common_points.length ? row.common_points.map(point => `<p>${escapeHtml(point)}</p>`).join('') : '<p>暂无明显共同点。</p>';
  const max = Math.max(1,...row.demands.map(d => d.count));
  $('categoryDemands').innerHTML = row.demands.length ? row.demands.map((d,i) => `<details class="demand-item" ${i===0?'open':''}>
    <summary><span class="demand-name">${escapeHtml(d.demand)}</span><span class="demand-bar"><i style="width:${d.count/max*100}%"></i></span><strong>${d.count}单</strong><small class="${signClass(d.delta)}">前日${d.previous} · ${signed(d.delta)}</small></summary>
    <div class="demand-samples">${d.samples.map(s => `<p><a href="${escapeHtml(s.source_url)}" target="_blank" rel="noopener">${escapeHtml(s.ticket_id)}</a>${escapeHtml(s.description)}</p>`).join('')}</div>
  </details>`).join('') : '<div class="empty">今日暂无该类工单</div>';
  $('categoryAttention').innerHTML = row.attention_records.length ? row.attention_records.map(r => `<article class="attention-card">
    <div><a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">工单 ${escapeHtml(r.ticket_id)}</a><span>${escapeHtml(r.shop_id)} · ${escapeHtml(r.brand)}</span></div>
    <p>${escapeHtml(r.description)}</p><div class="reason-list">${r.reasons.map(reason => `<span class="reason">${escapeHtml(reason)}</span>`).join('')}</div><small>${escapeHtml(r.created_at.slice(11,16))} · ${escapeHtml(r.status)}</small>
  </article>`).join('') : '<div class="empty">该类别暂无敏感风险或处理异常工单</div>';
}

function renderAlerts(growth, riskSummary, riskRecords, operationalSummary, appRiskRecords = []) {
  const combinedRiskRecords = [...riskRecords, ...appRiskRecords.map(row => ({
    ...row, ticket_id: row.inspection_id, status: row.inspect_status, handler: '商家APP', source_label: '商家APP'
  }))].sort((a, b) => ({'紧急': 0, '高': 1, '关注': 2}[a.risk_level] ?? 3) - ({'紧急': 0, '高': 1, '关注': 2}[b.risk_level] ?? 3));
  $('growthAlerts').innerHTML = growth.length ? growth.map(a => `<div class="alert"><strong>${escapeHtml(a.category)}：${a.current} 单</strong><small>近7日均值 ${a.avg7}，高出 ${a.delta_vs_avg} 单</small></div>`).join('') : '<div class="empty">当日各类型均未明显偏离近7日常态</div>';
  const appRiskSummary = appRiskRecords.length ? `<span>商家APP风险工单 <b>${appRiskRecords.length}</b></span>` : '';
  $('riskCount').textContent = `${combinedRiskRecords.length} 单`;
  $('riskSummary').innerHTML = `${riskSummary.length ? riskSummary.map(a => `<span>${escapeHtml(a.risk)} <b>${a.count}</b></span>`).join('') : appRiskRecords.length ? '<span>风险线索来自商家APP</span>' : '<span class="muted">暂无敏感风险词命中</span>'}${appRiskSummary}`;
  $('riskTicketList').innerHTML = combinedRiskRecords.length ? combinedRiskRecords.map(r => `<article class="risk-ticket ${r.risk_level==='紧急'?'critical':''}">
    <div class="risk-ticket-head"><div><a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">${r.source_label ? '稽查单' : '工单'} ${escapeHtml(r.ticket_id)}</a><span>${r.source_label ? '商家APP · ' : ''}${escapeHtml(r.shop_id)} · ${escapeHtml(r.brand)}</span></div><span class="risk-level">${escapeHtml(r.risk_level)}</span></div>
    <div class="risk-hit-list">${r.risk_hits.map(hit => `<span><b>${escapeHtml(hit.label)}</b> · 命中“${escapeHtml(hit.matched_text)}” · ${escapeHtml(hit.source)}</span>`).join('')}</div>
    <p>${escapeHtml(r.description)}</p><small>${escapeHtml((r.created_at || '').slice(11,16))} · ${escapeHtml(r.status)} · ${escapeHtml(r.handler)}</small>
  </article>`).join('') : '<div class="empty risk-empty">当日未识别到明确的暴力、舆情、未成年人、公共卫生或人身安全风险工单</div>';
  $('anomalySummary').innerHTML = operationalSummary.length ? operationalSummary.slice(0,8).map(a => `<div class="anomaly-chip"><span>${escapeHtml(a.reason)}</span><b>${a.count}</b></div>`).join('') : '<div class="empty">暂无处理或数据质量异常</div>';
}

async function pollRealtimeStatus() {
  if (isPublicDashboard) {
    try {
      const manifest = await fetchPublicJson('data/index.json');
      publicManifest = manifest;
      if (manifest.revision !== publicLoadedRevision) await loadPublicDashboard(manifest);
      else updatePublicStatus();
    } catch (error) {
      $('statusLine').className = 'status-line error';
      $('statusLine').textContent = `自动刷新失败，保留上次数据：${error.message}`;
    }
    return;
  }
  try {
    const response = await fetch('/api/sync/status');
    const status = await response.json();
    if (!status.ok) return;
    if (status.sync?.running) {
      $('statusLine').textContent = status.sync.message;
      return;
    }
    const finishedAt = status.realtime?.last_finished_at;
    if (finishedAt && finishedAt !== lastAutoSyncFinishedAt) {
      lastAutoSyncFinishedAt = finishedAt;
      if (followsLatestDate && status.realtime.latest_date) $('datePicker').value = status.realtime.latest_date;
      loadDashboard();
    }
  } catch (_) {
    // A temporary status polling failure must not interrupt the current dashboard view.
  }
}

function renderTopics(topics) {
  const max = Math.max(1, ...topics.map(t => t.count));
  $('topicsList').innerHTML = topics.map((t,i) => `<div class="topic-row ${i===0?'active':''}" data-topic="${i}"><strong>${escapeHtml(t.topic)}</strong><div class="topic-track"><div class="topic-fill" style="width:${t.count/max*100}%"></div></div><div class="topic-value">${t.count} <small>${t.ratio}%</small></div></div>`).join('');
  $('topicsList').querySelectorAll('.topic-row').forEach(row => row.addEventListener('click', () => {
    $('topicsList').querySelectorAll('.topic-row').forEach(x => x.classList.remove('active'));
    row.classList.add('active');
    showTopicSamples(topics[Number(row.dataset.topic)]);
  }));
  if (topics[0]) showTopicSamples(topics[0]);
}

function showTopicSamples(topic) {
  $('topicSamples').innerHTML = `<h3>${escapeHtml(topic.topic)} · 描述样例</h3><ul>${topic.samples.map(s => `<li><b>${escapeHtml(s.ticket_id)}</b> · ${escapeHtml(s.brand)}：${escapeHtml(s.description)}</li>`).join('')}</ul>`;
}

function renderKA(ka) {
  $('kaDefinition').textContent = ka.definition;
  const totalDelta = ka.count - ka.previous_count;
  $('kaSummary').innerHTML = `<div><span>KA 工单</span><strong>${ka.count}</strong><small class="${signClass(totalDelta)}">较前日 ${signed(totalDelta)} 单</small></div>
    <div><span>KA 占全部新增</span><strong>${ka.ratio}%</strong><small class="${signClass(ka.ratio_delta)}">较前日 ${signed(ka.ratio_delta)} 个百分点</small></div>
    <div><span>涉及 KA 商家</span><strong>${ka.brand_details.length}</strong><small>前日 ${ka.previous_count} 单 KA 工单</small></div>`;

  const categories = [{category: '全部', current: ka.count, previous: ka.previous_count, delta: totalDelta}, ...ka.category_breakdown];
  if (!categories.some(item => item.category === selectedKaCategory)) selectedKaCategory = '全部';
  $('kaCategoryFilters').innerHTML = categories.map(item => `<button type="button" class="ka-category-filter ${item.category === selectedKaCategory ? 'active' : ''}" data-ka-category="${escapeHtml(item.category)}" role="tab" aria-selected="${item.category === selectedKaCategory}">
    <span>${escapeHtml(item.category)}</span><strong>${item.current} 单</strong><small class="${signClass(item.delta)}">较前日 ${signed(item.delta)}</small>
  </button>`).join('');
  $('kaCategoryFilters').querySelectorAll('[data-ka-category]').forEach(button => button.addEventListener('click', () => {
    selectedKaCategory = button.dataset.kaCategory;
    renderKA(ka);
  }));

  const filteredBrands = ka.brand_details.map(brand => ({
    ...brand,
    visibleTickets: selectedKaCategory === '全部' ? brand.tickets : brand.tickets.filter(ticket => ticket.category === selectedKaCategory)
  })).filter(brand => brand.visibleTickets.length);
  $('kaDetailHint').textContent = selectedKaCategory === '全部' ? `共 ${ka.count} 张 KA 工单，按商家展开` : `${selectedKaCategory}：${categories.find(item => item.category === selectedKaCategory)?.current || 0} 张工单，按商家展开`;
  $('kaBrandDetails').innerHTML = filteredBrands.length ? filteredBrands.map((brand, index) => {
    const categoryTags = ka.category_breakdown.filter(item => brand.categories[item.category]).map(item => `<span>${escapeHtml(item.category)} ${brand.categories[item.category]}</span>`).join('');
    const otherTag = brand.other_count ? `<span>其他 ${brand.other_count}</span>` : '';
    return `<details class="ka-brand-group" ${index === 0 ? 'open' : ''}>
      <summary><div><strong>${escapeHtml(brand.brand)}</strong><span>${brand.visibleTickets.length} 张${selectedKaCategory === '全部' ? ' KA 工单' : ` ${escapeHtml(selectedKaCategory)}`}</span></div><div class="ka-brand-tags">${categoryTags}${otherTag}</div></summary>
      <div class="ka-ticket-list">${brand.visibleTickets.map(ticket => `<article class="ka-ticket">
        <div class="ka-ticket-head"><a href="${escapeHtml(ticket.source_url)}" target="_blank" rel="noopener">工单 ${escapeHtml(ticket.ticket_id)}</a><span class="category-tag">${escapeHtml(ticket.category)}</span></div>
        <p>${escapeHtml(ticket.description) || '未填写问题描述'}</p>
        <small>店铺 ${escapeHtml(ticket.shop_id)} · ${escapeHtml(ticket.created_at.slice(11,16))} · ${escapeHtml(ticket.status)}${ticket.handler ? ` · ${escapeHtml(ticket.handler)}` : ''}</small>
      </article>`).join('')}</div>
    </details>`;
  }).join('') : '<div class="empty">当前类别下没有 KA 工单</div>';
}

function renderAnomalyTable(rows) {
  $('anomalyTable').innerHTML = rows.length ? rows.map(r => `<tr>
    <td><span class="ticket-id">${escapeHtml(r.ticket_id)}</span><br><span class="muted">${escapeHtml(r.created_at.slice(11))}</span></td>
    <td>${escapeHtml(r.shop_id)}<br><span class="muted">${escapeHtml(r.brand)}</span></td>
    <td>${escapeHtml(r.category)}</td><td>${escapeHtml(r.description)}</td>
    <td><div class="reason-list">${r.reasons.map(reason => `<span class="reason">${escapeHtml(reason)}</span>`).join('')}</div></td>
    <td>${escapeHtml(r.status)}<br><span class="muted">${escapeHtml(r.handler)}</span></td>
  </tr>`).join('') : '<tr><td colspan="6" class="empty">当日没有命中异常规则的工单</td></tr>';
}

async function syncData() {
  if (isPublicDashboard) return loadPublicDashboard();
  const button = $('syncBtn');
  button.disabled = true; button.textContent = '正在同步…';
  const days = Number($('windowPicker').value);
  try {
    const response = await fetch('/api/sync', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days})});
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || '启动同步失败');
    const timer = setInterval(async () => {
      const status = await (await fetch('/api/sync/status')).json();
      $('statusLine').textContent = status.sync.message;
      if (!status.sync.running) {
        clearInterval(timer); button.disabled = false; button.textContent = '同步最新数据'; loadDashboard();
      }
    }, 1500);
  } catch (error) {
    button.disabled = false; button.textContent = '同步最新数据';
    $('statusLine').className = 'status-line error'; $('statusLine').textContent = error.message;
  }
}

async function syncEvidence() {
  if (isPublicDashboard) return loadRootCauses();
  const button = $('evidenceBtn');
  button.disabled = true; button.textContent = '正在更新证据…';
  try {
    const response = await fetch('/api/evidence/sync', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:$('datePicker').value})});
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || '启动逐单证据同步失败');
    const timer = setInterval(async () => {
      const status = await (await fetch('/api/evidence/status')).json();
      $('rootStatusLine').textContent = status.sync.message;
      if (!status.sync.running) {
        clearInterval(timer); button.disabled = false; button.textContent = '更新逐单证据'; loadRootCauses();
      }
    }, 1000);
  } catch (error) {
    button.disabled = false; button.textContent = '更新逐单证据';
    $('rootStatusLine').className = 'root-status-line error'; $('rootStatusLine').textContent = error.message;
  }
}


$('datePicker').addEventListener('change', () => { followsLatestDate = false; loadDashboard(); });
$('windowPicker').addEventListener('change', loadDashboard);
$('merchantRankingSearch').addEventListener('input', () => { if (merchantRankingData) renderMerchantRanking(merchantRankingData); });
$('syncBtn').addEventListener('click', syncData);
$('evidenceBtn').addEventListener('click', syncEvidence);
['rootLayer','rootStatus','rootKa','rootUnresolved'].forEach(id => $(id).addEventListener('change', () => { rootPage=1; loadRootCauses(); }));
$('rootSearch').addEventListener('input', () => { clearTimeout(rootSearchTimer); rootSearchTimer=setTimeout(() => { rootPage=1; loadRootCauses(); },300); });
$('rootPrev').addEventListener('click', () => { if (rootPage>1) { rootPage--; loadRootCauses(); $('rootStatusLine').scrollIntoView({behavior:'smooth',block:'center'}); } });
$('rootNext').addEventListener('click', () => { if (rootPage<rootPages) { rootPage++; loadRootCauses(); $('rootStatusLine').scrollIntoView({behavior:'smooth',block:'center'}); } });
async function fetchPublicJson(path) {
  const response = await fetch(`${path}?v=${Date.now()}`, {cache: 'no-store'});
  if (!response.ok) throw new Error(`数据读取失败（${response.status}）`);
  return response.json();
}

function updatePublicStatus() {
  if (!publicSnapshot || !publicManifest) return;
  const sourceAt = publicManifest.source_synced_at;
  const ageMinutes = (Date.now() - Date.parse(sourceAt)) / 60000;
  const stale = !Number.isFinite(ageMinutes) || ageMinutes > 10;
  $('statusLine').className = stale ? 'status-line error' : 'status-line';
  const cutoff = dashboardData?.cutoff_time ? ` · 同时段对比截至 ${dashboardData.cutoff_time}` : '';
  $('statusLine').textContent = `${dashboardData.selected_date}${cutoff} · 工单更新：${sourceAt.replace('T', ' ').slice(0,19)}（北京时间） · 快照发布：${publicManifest.published_at.replace('T', ' ').slice(11,19)} · 每5分钟采集发布，页面自动检查更新${stale ? ' · 数据已超过10分钟未更新，请等待采集端恢复' : ''}`;
}

async function loadPublicDashboard(manifest) {
  const requestId = ++publicRequest;
  const button = $('syncBtn');
  button.disabled = true;
  $('statusLine').textContent = '正在读取最新公开数据…';
  try {
    manifest = manifest || await fetchPublicJson('data/index.json');
    const selected = followsLatestDate ? manifest.latest_date : $('datePicker').value;
    if (!manifest.dates.includes(selected)) throw new Error('该日期没有已发布的数据，请选择日期范围内的数据');
    const snapshot = await fetchPublicJson(`data/${selected}.json`);
    if (requestId !== publicRequest) return;
    publicManifest = manifest;
    publicSnapshot = snapshot;
    publicLoadedRevision = manifest.revision;
    $('datePicker').min = manifest.dates[manifest.dates.length - 1];
    $('datePicker').max = manifest.latest_date;
    $('datePicker').value = selected;
    const windowSize = Number($('windowPicker').value);
    dashboardData = {...snapshot.dashboard, daily_trend: snapshot.dashboard.daily_trend.slice(-windowSize)};
    render(dashboardData, null, null);
    updatePublicStatus();
    rootPage = 1;
    loadPublicRootCauses();
  } catch (error) {
    if (requestId !== publicRequest) return;
    $('statusLine').className = 'status-line error';
    $('statusLine').textContent = `${error.message}${publicSnapshot ? '；保留上次成功读取的数据' : ''}`;
  } finally {
    if (requestId === publicRequest) button.disabled = false;
  }
}

function loadPublicRootCauses() {
  if (!publicSnapshot) return;
  const ledger = publicSnapshot.ledger;
  const params = rootQuery();
  const keyword = (params.get('q') || '').toLowerCase();
  const filtered = ledger.records.filter(record => {
    const text = ['ticket_id','shop_id','brand','merchant_claim','customer_behavior','root_cause'].map(key => record[key] || '').join(' ').toLowerCase();
    return (!keyword || text.includes(keyword)) &&
      (!params.get('layer') || record.root_layer === params.get('layer')) &&
      (!params.get('status') || record.verification_status === params.get('status')) &&
      (params.get('ka') !== 'ka' || record.is_ka) &&
      (params.get('ka') !== 'non-ka' || !record.is_ka) &&
      (!params.get('unresolved') || record.verification_status === '待核实');
  });
  const pageSize = Number(params.get('page_size'));
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  rootPage = Math.min(rootPage, pages);
  renderRootCauses({...ledger, filtered_total: filtered.length, page: rootPage, pages,
    records: filtered.slice((rootPage - 1) * pageSize, rootPage * pageSize)}, null);
}

if (isPublicDashboard) {
  $('syncBtn').textContent = '刷新公开数据';
  $('evidenceBtn').hidden = true;
}
loadDashboard();
window.setInterval(pollRealtimeStatus, 30000);
