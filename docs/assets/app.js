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
let lastAutoSyncFinishedAt = null;
let followsLatestDate = true;
const $ = (id) => document.getElementById(id);
const isPublicDashboard = window.TICKET_PUBLIC === true;
let publicManifest = null;
let publicSnapshot = null;
let publicLoadedRevision = null;
let publicRequest = 0;
let assignmentConfirmToken = null;
let assignmentEnabled = false;
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
  $('totalMetric').textContent = s.total.toLocaleString();
  $('totalCompare').className = signClass(s.delta);
  $('totalCompare').textContent = `较前一日 ${signed(s.delta)}（${signed(s.pct)}%），近7日均值 ${s.avg7}`;
  const driver = s.top_driver;
  $('driverMetric').textContent = driver ? driver.category : '—';
  $('driverCompare').className = driver ? signClass(driver.delta) : '';
  $('driverCompare').textContent = driver ? (s.delta > 0 ? `较前一日 ${signed(driver.delta)} 单，贡献总增量 ${driver.contribution}%` : `较前一日 ${signed(driver.delta)} 单；全量净变化 ${signed(s.delta)} 单`) : '暂无数据';
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
  renderCategoryInsights(data.category_insights);
  renderAlerts(data.growth_anomalies, data.risk_summary, data.risk_records, data.operational_anomaly_summary);
  renderTopics(data.topics);
  renderKA(data.ka);
  renderAnomalyTable(data.anomaly_records);
}

function renderTrend(days) {
  const categories = Object.keys(colors);
  $('trendLegend').innerHTML = categories.map(c => `<span><i style="background:${colors[c]}"></i>${c}</span>`).join('');
  const max = Math.max(1, ...days.map(d => d.total));
  $('trendChart').style.setProperty('--bars', days.length);
  $('trendChart').style.minWidth = `${Math.max(640, days.length * 58)}px`;
  $('trendChart').innerHTML = days.map(day => {
    const segments = categories.map(cat => {
      const height = day.total ? day.categories[cat] / day.total * 100 : 0;
      return `<span class="bar-seg" style="--seg-height:${height}%;background:${colors[cat]}" title="${cat} ${day.categories[cat]}单"></span>`;
    }).join('');
    return `<div class="day-bar"><span class="bar-value">${day.total}</span><div class="bar-stack" style="--height:${day.total / max * 215}px" title="${day.date}：${day.total}单">${segments}</div><span class="day-label">${day.label}</span></div>`;
  }).join('');
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

function renderAlerts(growth, riskSummary, riskRecords, operationalSummary) {
  $('growthAlerts').innerHTML = growth.length ? growth.map(a => `<div class="alert"><strong>${escapeHtml(a.category)}：${a.current} 单</strong><small>近7日均值 ${a.avg7}，高出 ${a.delta_vs_avg} 单</small></div>`).join('') : '<div class="empty">当日各类型均未明显偏离近7日常态</div>';
  $('riskCount').textContent = `${riskRecords.length} 单`;
  $('riskSummary').innerHTML = riskSummary.length ? riskSummary.map(a => `<span>${escapeHtml(a.risk)} <b>${a.count}</b></span>`).join('') : '<span class="muted">暂无敏感风险词命中</span>';
  $('riskTicketList').innerHTML = riskRecords.length ? riskRecords.map(r => `<article class="risk-ticket ${r.risk_level==='紧急'?'critical':''}">
    <div class="risk-ticket-head"><div><a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">工单 ${escapeHtml(r.ticket_id)}</a><span>${escapeHtml(r.shop_id)} · ${escapeHtml(r.brand)}</span></div><span class="risk-level">${escapeHtml(r.risk_level)}</span></div>
    <div class="risk-hit-list">${r.risk_hits.map(hit => `<span><b>${escapeHtml(hit.label)}</b> · 命中“${escapeHtml(hit.matched_text)}” · ${escapeHtml(hit.source)}</span>`).join('')}</div>
    <p>${escapeHtml(r.description)}</p><small>${escapeHtml(r.created_at.slice(11,16))} · ${escapeHtml(r.status)} · ${escapeHtml(r.handler)}</small>
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

async function adminJson(url, options = {}) {
  const response = await fetch(url, options);
  let result;
  try { result = await response.json(); }
  catch (_) { throw new Error(`管理接口返回异常（HTTP ${response.status}）`); }
  if (!response.ok || !result.ok) throw new Error(result.error || `请求失败（HTTP ${response.status}）`);
  return result;
}

function renderTianrunStatus(data) {
  assignmentEnabled = Boolean(data.assign_enabled);
  const badge = $('tianrunConnectionBadge');
  badge.className = `connection-badge ${data.connection === 'ok' ? 'ok' : data.connection === 'missing' ? 'locked' : data.connection}`;
  badge.textContent = data.connection === 'ok' ? '连接正常' : data.connection === 'expired' ? '登录失效' : data.connection === 'error' ? '连接异常' : data.configured ? '待检测' : '未配置';
  const names = data.cookie_names?.length ? ` · 已保存字段：${data.cookie_names.join('、')}` : '';
  const updated = data.updated_at ? ` · 更新于 ${data.updated_at.replace('T',' ')}` : '';
  $('tianrunConnectionText').textContent = `${data.message || '尚未检测'}${updated}${names}`;
  $('assignModeBadge').className = `connection-badge ${assignmentEnabled ? 'error' : 'locked'}`;
  $('assignModeBadge').textContent = assignmentEnabled ? '真实分派已开启' : '真实分派关闭';
}

async function loadTianrunStatus(test = false) {
  try {
    const result = await adminJson(`/api/tianrun/status${test ? '?test=1' : ''}`);
    renderTianrunStatus(result.data);
  } catch (error) {
    renderTianrunStatus({connection:'error',message:error.message,configured:false,assign_enabled:false});
  }
}

async function saveTianrunCookie() {
  const button = $('saveCookieBtn');
  const cookie = $('tianrunCookie').value.trim();
  if (!cookie) { $('tianrunConnectionText').textContent = '请先粘贴重新登录后取得的新Cookie。'; return; }
  button.disabled = true; button.textContent = '正在验证…';
  try {
    const result = await adminJson('/api/tianrun/cookie', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie})});
    $('tianrunCookie').value = '';
    renderTianrunStatus(result.data);
  } catch (error) {
    $('tianrunConnectionBadge').className = 'connection-badge error';
    $('tianrunConnectionBadge').textContent = '保存失败';
    $('tianrunConnectionText').textContent = error.message;
  } finally { button.disabled = false; button.textContent = '保存并检测'; }
}

async function searchTianrunUsers() {
  const query = $('tianrunUserQuery').value.trim();
  const container = $('tianrunUserResults');
  if (query.length < 2) { container.innerHTML = '<p class="empty">请输入至少2个字符的姓名或工号</p>'; return; }
  container.innerHTML = '<p class="empty">正在查询天润员工…</p>';
  try {
    const result = await adminJson(`/api/tianrun/users?q=${encodeURIComponent(query)}`);
    container.innerHTML = result.data.length ? result.data.map(row => `<button type="button" class="user-result" data-cno="${escapeHtml(row.cno)}" data-name="${escapeHtml(row.name)}"><strong>${escapeHtml(row.name)}</strong><span>工号 ${escapeHtml(row.cno) || '—'}</span></button>`).join('') : '<p class="empty">没有找到员工</p>';
    container.querySelectorAll('.user-result').forEach(button => button.addEventListener('click', () => {
      $('assignRecipient').value = button.dataset.cno || button.dataset.name;
    }));
  } catch (error) { container.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`; }
}

async function previewAssignment() {
  assignmentConfirmToken = null;
  const container = $('assignmentPreview');
  const payload = {ticket_id:$('assignTicketId').value.trim(),recipient:$('assignRecipient').value.trim(),reason:$('assignReason').value.trim()};
  container.innerHTML = '<p class="empty">正在读取工单和接收人…</p>';
  try {
    const result = await adminJson('/api/tianrun/assignment/preview', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const {ticket, recipient, reason, confirm_token: token, assign_enabled: enabled} = result.data;
    assignmentConfirmToken = token; assignmentEnabled = enabled;
    const phrase = `确认分派 ${ticket.ticket_id}`;
    container.innerHTML = `<article class="preview-card"><h4>工单 ${escapeHtml(ticket.ticket_id)} → ${escapeHtml(recipient.name)}（${escapeHtml(recipient.cno)}）</h4>
      <div class="preview-meta"><span>${escapeHtml(ticket.category)}</span><span>店铺 ${escapeHtml(ticket.shop_id)}</span><span>当前处理人：${escapeHtml(ticket.handler) || '未显示'}</span><span>状态：${escapeHtml(ticket.status)}</span></div>
      <p>${escapeHtml(ticket.description) || '未填写问题描述'}</p><p><b>分派理由：</b>${escapeHtml(reason)}</p>
      <div class="confirm-row"><label class="dispatch-field"><span>二次确认：请输入“${escapeHtml(phrase)}”</span><input id="assignmentConfirmation" autocomplete="off" placeholder="${escapeHtml(phrase)}"></label><button id="executeAssignBtn" type="button" ${enabled ? '' : 'disabled'}>${enabled ? '执行真实分派' : '真实分派尚未开启'}</button></div></article>`;
    if (enabled) $('executeAssignBtn').addEventListener('click', executeAssignment);
  } catch (error) { container.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`; }
}

async function executeAssignment() {
  const button = $('executeAssignBtn');
  const confirmation = $('assignmentConfirmation').value.trim();
  button.disabled = true; button.textContent = '正在提交…';
  try {
    const result = await adminJson('/api/tianrun/assignment/execute', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_token:assignmentConfirmToken,confirmation})});
    assignmentConfirmToken = null;
    $('assignmentPreview').innerHTML = `<div class="status-line">${escapeHtml(result.data.state)}：${escapeHtml(result.data.message)}</div>`;
    loadAssignmentAudit();
  } catch (error) {
    button.disabled = false; button.textContent = '执行真实分派';
    const old = $('assignmentPreview').querySelector('.status-line.error'); if (old) old.remove();
    $('assignmentPreview').insertAdjacentHTML('afterbegin', `<div class="status-line error">${escapeHtml(error.message)}</div>`);
  }
}

async function loadAssignmentAudit() {
  try {
    const result = await adminJson('/api/tianrun/assignment/audit');
    $('assignmentAudit').innerHTML = result.data.length ? result.data.map(row => {
      const tone = row.state === '失败' ? 'error' : row.state.includes('待核验') ? 'pending' : 'success';
      return `<tr><td>${escapeHtml(row.requested_at.replace('T',' '))}</td><td class="ticket-id">${escapeHtml(row.ticket_id)}</td><td>${escapeHtml(row.old_handler) || '—'}</td><td>${escapeHtml(row.target_name)}<br><span class="muted">${escapeHtml(row.target_cno)}</span></td><td><span class="audit-state ${tone}">${escapeHtml(row.state)}</span></td><td>${escapeHtml(row.message)}</td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty">暂无分派记录</td></tr>';
  } catch (error) { $('assignmentAudit').innerHTML = `<tr><td colspan="6" class="empty">${escapeHtml(error.message)}</td></tr>`; }
}

$('datePicker').addEventListener('change', () => { followsLatestDate = false; loadDashboard(); });
$('windowPicker').addEventListener('change', loadDashboard);
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
} else {
  $('dispatchPanel').hidden = false;
  $('saveCookieBtn').addEventListener('click', saveTianrunCookie);
  $('testCookieBtn').addEventListener('click', () => loadTianrunStatus(true));
  $('searchUserBtn').addEventListener('click', searchTianrunUsers);
  $('previewAssignBtn').addEventListener('click', previewAssignment);
  $('refreshAuditBtn').addEventListener('click', loadAssignmentAudit);
  loadTianrunStatus();
  loadAssignmentAudit();
}
loadDashboard();
window.setInterval(pollRealtimeStatus, 30000);
