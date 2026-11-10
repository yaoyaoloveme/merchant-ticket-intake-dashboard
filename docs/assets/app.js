'use strict';

const colors = {
  '多付/漏付': '#e76f3c', '介入问题': '#006b63', '批量核实': '#3678a8',
  '回访工单': '#d9a11e', '其他': '#7968a8'
};
let dashboardData = null;
let completionRecordsCache = null;
let completionDayRequest = 0;
let completionDateChanged = false;
let completionPage = 1;
let completionPages = 1;
let lastAutoSyncFinishedAt = null;
let followsLatestDate = true;
let dashboardRequest = 0;
let dashboardPendingRequest = null;
let dashboardRefreshTimer = null;
let dashboardRenderedKey = null;
let dashboardRenderedVersion = null;
const $ = (id) => document.getElementById(id);
const isPublicDashboard = window.TICKET_PUBLIC === true;
const isReadOnlyDashboard = window.TICKET_READONLY === true;
let publicManifest = null;
let publicSnapshot = null;
let publicLoadedRevision = null;
let publicRequest = 0;
let publicVersionCheckRunning = false;
let publicReloading = false;
const publicPageOpenedAt = Date.now();
const signed = (n) => `${n > 0 ? '+' : ''}${n}`;
const signClass = (n) => n > 0 ? 'positive' : n < 0 ? 'negative' : '';
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

function loadDashboard(options = {}) {
  if (isPublicDashboard) return loadPublicDashboard();
  const date = $('datePicker').value;
  const windowSize = $('windowPicker').value;
  const key = `${date}|${windowSize}`;
  const refresh = options.refresh === true;
  if (dashboardPendingRequest?.key === key) {
    if (refresh) dashboardPendingRequest.refreshAfter = true;
    return dashboardPendingRequest.promise;
  }
  window.clearTimeout(dashboardRefreshTimer);
  dashboardPendingRequest?.controller.abort();
  const requestId = ++dashboardRequest;
  const pending = {key, controller: new AbortController(), refreshAfter: false};
  dashboardPendingRequest = pending;
  $('statusLine').className = 'status-line';
  $('statusLine').textContent = dashboardData
    ? (dashboardRenderedKey === key ? '正在更新，保留当前分析结果…' : `正在读取 ${date} 的分析结果，当前仍显示 ${dashboardData.selected_date}…`)
    : '正在读取分析结果…';
  pending.promise = (async () => {
    try {
      const response = await fetch(`/api/dashboard?date=${encodeURIComponent(date)}&window=${windowSize}${refresh ? '&refresh=1' : ''}`, {signal: pending.controller.signal, cache: 'no-store'});
      const result = await response.json();
      if (requestId !== dashboardRequest) return;
      if (!response.ok || !result.ok) throw new Error(result.error || '读取失败');
      const sameResult = dashboardRenderedKey === key && result.analysis?.version && dashboardRenderedVersion === result.analysis.version;
      dashboardData = result.data;
      dashboardRenderedKey = key;
      dashboardRenderedVersion = result.analysis?.version;
      if (sameResult) renderDashboardStatus(result.data, result.sync, result.realtime, result.analysis);
      else render(result.data, result.sync, result.realtime, result.analysis);
      // Poll the replacement while stale, and periodically revalidate fresh results.
      const delay = Math.max(2, Math.min(60, Number(result.analysis?.retry_after_seconds) || 60));
      dashboardRefreshTimer = window.setTimeout(() => loadDashboard(), delay * 1000);
    } catch (error) {
      if (requestId !== dashboardRequest || error.name === 'AbortError') return;
      $('statusLine').className = 'status-line error';
      $('statusLine').textContent = `${error.message}${dashboardData ? '；保留上次成功读取的结果，将自动重试' : '，稍后自动重试'}`;
      dashboardRefreshTimer = window.setTimeout(() => loadDashboard(), 10000);
    } finally {
      if (dashboardPendingRequest === pending) {
        dashboardPendingRequest = null;
        if (pending.refreshAfter) loadDashboard({refresh: true});
      }
    }
  })();
  return pending.promise;
}

function renderDashboardStatus(data, sync, realtime, analysis) {
  if (!analysis?.stale) lastAutoSyncFinishedAt = realtime?.last_finished_at || lastAutoSyncFinishedAt;
  const syncText = data.last_sync ? `本地数据更新：${data.last_sync.finished_at.replace('T',' ')} · ${data.last_sync.message}` : '尚未同步数据';
  const cutoffText = data.cutoff_time ? ` · 新增量对比统一截至 ${data.cutoff_time}（重复反馈历史日按全天）` : '';
  const refreshText = realtime?.next_run_at ? ` · 自动同步：每5分钟，下一次 ${realtime.next_run_at.slice(11,16)}` : ' · 自动同步：每5分钟';
  const analysisText = analysis?.stale
    ? (analysis.refreshing ? '已显示最近结果，后台更新中' : '已显示最近结果，后台更新失败，稍后自动重试')
    : '分析完成';
  $('statusLine').className = analysis?.stale && !analysis.refreshing ? 'status-line error' : 'status-line';
  $('statusLine').textContent = `${data.selected_date} ${analysisText}${cutoffText} · ${syncText}${refreshText}${sync?.running ? ` · ${sync.message}` : ''}`;
}

function render(data, sync, realtime, analysis) {
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
  renderDashboardStatus(data, sync, realtime, analysis);
  renderTrend(data.daily_trend);
  renderFeedbackAnalysis(data.feedback_analysis);
  renderShoppingBagUnderpayment(data.shopping_bag_underpayment);
  completionRecordsCache = null;
  completionPage = 1;
  if (!completionDateChanged) $('completionDatePicker').value = data.selected_date;
  loadCompletionDay();
  renderMerchantApp(data.merchant_app);
  renderKA(data.ka);
}

function renderCompletionRate(rate) {
  if (!rate) return;
  const rateLabel = rate.completed ? `${rate.rate_24h}%` : '暂无可核验样本';
  const selectedLabel = rate.selected_date ? `${rate.selected_date} 创建` : '当前统计周期';
  $('completionSummary').innerHTML = `<div class="completion-main"><span>${selectedLabel} · 24小时内完成率</span><strong>${rateLabel}</strong><small>完成数 ÷ 纳入分母；当前未完成也计入分母</small></div>
    <div><span>24小时内完成 / 分母</span><strong>${Number(rate.within_24h || 0).toLocaleString()} / ${Number(rate.completed || 0).toLocaleString()}</strong><small>未完成和已超时均计入分母</small></div>
    <div><span>当前未完成</span><strong>${Number(rate.not_completed_count || 0).toLocaleString()}</strong><small>其中已超过24小时 ${Number(rate.overdue_not_completed_count || 0).toLocaleString()} 单</small></div>
    <div><span>仍待核验</span><strong>${Number(rate.pending_verification || 0).toLocaleString()}</strong><small>详情异常或时间冲突，不纳入分母</small></div>`;
  $('completionCategories').innerHTML = (rate.categories || []).map(item => { const itemRate = item.completed ? `${item.rate_24h}%` : '暂无样本'; return `<article class="completion-category"><div class="completion-category-head"><strong>${escapeHtml(item.category)}</strong><span>${itemRate}</span></div><div class="completion-category-grid"><span>完成 / 分母 <b>${item.within_24h} / ${item.completed}</b></span><span>未完成 <b>${item.not_completed_count}</b>（超24h ${item.overdue_not_completed_count}）</span><span>待核验 <b>${item.pending_verification}</b></span></div></article>`; }).join('');
  const excluded = rate.excluded_breakdown || [];
  $('completionQuality').innerHTML = `<span>${escapeHtml(rate.source_definition || '')}</span>${excluded.length ? `<span class="completion-excluded">待核验：${excluded.map(item => `${escapeHtml(item.reason)} ${item.count} 单`).join(' · ')}</span>` : '<span>当日没有待核验工单</span>'}`;
}

function weekStartForIso(dayString) {
  const day = new Date(`${dayString}T00:00:00`);
  day.setDate(day.getDate() - ((day.getDay() + 6) % 7));
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
}

async function loadCompletionDay() {
  const requestId = ++completionDayRequest;
  const selectedDate = $('completionDatePicker').value || dashboardData?.selected_date;
  if (!selectedDate) return;
  $('completionDatePicker').value = selectedDate;
  $('completionDetailCount').textContent = '正在读取所选日期…';
  $('completionRecordTable').innerHTML = '<tr><td colspan="6">正在读取逐单结果…</td></tr>';
  try {
    let payload;
    if (isPublicDashboard) {
      const weekStart = weekStartForIso(selectedDate);
      const weekPath = `data/completion-week-${weekStart}.json`;
      if (!completionRecordsCache || completionRecordsCache.revision !== publicLoadedRevision || completionRecordsCache.week !== weekStart) {
        const week = await fetchPublicJson(weekPath);
        completionRecordsCache = {revision: publicLoadedRevision, week: weekStart, days: week.days};
      }
      payload = completionRecordsCache.days.find(day => day.date === selectedDate);
      if (!payload) throw new Error('该日期不在已发布的最近一周或周快照中');
    } else {
      const response = await fetch(`/api/completion-day?date=${encodeURIComponent(selectedDate)}`);
      const result = await response.json();
      if (!result.ok) throw new Error(result.error || '当日完成率读取失败');
      payload = result.data;
      completionRecordsCache = {revision: selectedDate, week: '', days: [payload]};
    }
    if (requestId !== completionDayRequest) return;
    payload.stats.selected_date = selectedDate;
    renderCompletionRate(payload.stats);
    completionRecordsCache.rows = payload.records || [];
    completionRecordsCache.date = selectedDate;
    completionPage = 1;
    renderCompletionRecords();
  } catch (error) {
    if (requestId !== completionDayRequest) return;
    $('completionSummary').innerHTML = `<div class="completion-main"><span>${escapeHtml(selectedDate)} 创建</span><strong>暂不可用</strong><small>${escapeHtml(error.message)}</small></div>`;
    $('completionCategories').innerHTML = '';
    $('completionQuality').textContent = '';
    $('completionRecordTable').innerHTML = `<tr><td colspan="6">${escapeHtml(error.message)}</td></tr>`;
    $('completionDetailCount').textContent = '当日数据不可用';
  }
}

function renderCompletionRecords() {
  const body = $('completionRecordTable');
  if (!completionRecordsCache?.rows || completionRecordsCache.date !== $('completionDatePicker').value) {
    body.innerHTML = '<tr><td colspan="6">正在读取逐单结果…</td></tr>';
    return;
  }
  const category = $('completionCategoryFilter').value;
  const outcome = $('completionResultFilter').value;
  const rows = completionRecordsCache.rows.filter(row => (category === '全部' || row.category === category) && (outcome === '全部' || row.result === outcome));
  $('completionDetailCount').textContent = `${rows.length.toLocaleString()} 单 · ${$('completionDatePicker').value} 创建`;
  completionPages = Math.max(1, Math.ceil(rows.length / 50));
  completionPage = Math.min(completionPage, completionPages);
  $('completionPageLabel').textContent = `${completionPage} / ${completionPages} 页，每页50单`;
  $('completionPrev').disabled = completionPage <= 1;
  $('completionNext').disabled = completionPage >= completionPages;
  const shown = rows.slice((completionPage - 1) * 50, completionPage * 50);
  body.innerHTML = shown.length ? shown.map(row => {
    const seconds = row.elapsed_seconds;
    const duration = seconds == null ? '—' : `${Math.floor(seconds / 3600)}小时${Math.floor(seconds % 3600 / 60)}分${seconds % 60}秒`;
    const resultClass = row.result === '超时' || row.result === '超时未完成' ? 'late' : row.result === '待核验' ? 'pending' : row.result === '未完成' ? 'incomplete' : '';
    return `<tr><td><a href="${escapeHtml(row.source_url)}" target="_blank" rel="noopener">${escapeHtml(row.ticket_id)}</a></td><td>${escapeHtml(row.category)}</td><td>${escapeHtml(row.created_at)}</td><td>${escapeHtml(row.first_inspect_completed_at || '—')}</td><td>${duration}</td><td><strong class="completion-result ${resultClass}">${escapeHtml(row.result)}</strong>${row.reason ? `<small>${escapeHtml(row.reason)}</small>` : ''}</td></tr>`;
  }).join('') : '<tr><td colspan="6">所选日期没有符合筛选条件的工单</td></tr>';
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

function renderShoppingBagUnderpayment(data) {
  data = data || {};
  const count = Number(data.count || 0);
  const previous = Number(data.previous || 0);
  const delta = Number(data.delta || 0);
  const daily = data.daily || [];
  const records = data.records || [];
  $('shoppingBagSummary').innerHTML = `<div class="shopping-bag-main"><span>所选日期反馈</span><strong>${count.toLocaleString()}</strong><small>按工单ID去重</small></div>
    <div><span>前一日</span><strong>${previous.toLocaleString()}</strong><small class="${signClass(delta)}">变化 ${signed(delta)} 单</small></div>
    <div><span>前7日均值</span><strong>${Number(data.avg7 || 0).toLocaleString()}</strong><small>不含所选日期</small></div>`;
  const max = Math.max(1, ...daily.map(day => Number(day.count || 0)));
  $('shoppingBagChart').style.setProperty('--bag-days', daily.length || 1);
  $('shoppingBagChart').style.minWidth = `${Math.max(560, daily.length * 52)}px`;
  $('shoppingBagChart').innerHTML = daily.map(day => {
    const value = Number(day.count || 0);
    const height = value ? Math.max(8, value / max * 108) : 2;
    return `<div class="shopping-bag-day" title="${escapeHtml(day.date)}：${value} 单"><span>${value}</span><i style="height:${height}px"></i><small>${escapeHtml(day.label)}</small></div>`;
  }).join('');
  $('shoppingBagDefinition').textContent = data.definition || '';
  $('shoppingBagDetailSummary').textContent = `查看所选日期命中工单（${records.length} 单）`;
  $('shoppingBagRecords').innerHTML = records.length ? records.map(row => `<article class="shopping-bag-record">
    <div><a href="${escapeHtml(row.source_url)}" target="_blank" rel="noopener">${escapeHtml(row.ticket_id)}</a><span>店铺 ${escapeHtml(row.shop_id || '—')} · ${escapeHtml(row.brand)}</span><b>${escapeHtml(row.status)}</b></div>
    <p>${escapeHtml(row.description)}</p>
    <small>${escapeHtml(row.created_at)} · ${escapeHtml(row.category)}${row.is_ka ? ' · KA' : ''}</small>
  </article>`).join('') : '<p class="empty">所选日期没有描述中包含“购物袋”的工单。</p>';
}

function renderFeedbackAnalysis(data) {
  data = data || {};
  const selected = data.selected || {};
  const confirmed = Number(selected.confirmed_repeat_count || 0);
  const suspected = Number(selected.suspected_repeat_count || 0);
  const total = Number(selected.total || 0);
  const peakCount = Number(selected.peak_count || 0);
  $('feedbackSummary').innerHTML = `<div class="feedback-summary-main"><span>规则确认再次反馈</span><strong>${confirmed.toLocaleString()}</strong><small>涉及原事项 ${Number(selected.linked_issue_count || 0).toLocaleString()} 个</small></div>
    <div><span>重复反馈率</span><strong>${Number(selected.repeat_rate || 0).toFixed(1)}%</strong><small>${confirmed} / ${total}，原始新增不扣减</small></div>
    <div><span>跨渠道再次反馈</span><strong>${Number(selected.cross_channel_count || 0).toLocaleString()}</strong><small>APP与天润之间的明确关联</small></div>
    <div><span>疑似重复待复核</span><strong>${suspected.toLocaleString()}</strong><small>不纳入正式重复率</small></div>
    <div><span>反馈高峰</span><strong class="feedback-period">${escapeHtml(selected.peak_period || '—')}</strong><small>${peakCount} 单，占当日 ${total ? (peakCount * 100 / total).toFixed(1) : '0.0'}%</small></div>`;

  const daily = data.daily || [];
  const dailyMax = Math.max(1, ...daily.flatMap(item => [Number(item.confirmed_repeat_count || 0), Number(item.suspected_repeat_count || 0)]));
  $('feedbackDailyChart').style.setProperty('--feedback-days', daily.length || 1);
  $('feedbackDailyChart').style.minWidth = `${Math.max(380, daily.length * 44)}px`;
  $('feedbackDailyChart').innerHTML = daily.map(item => {
    const count = Number(item.confirmed_repeat_count || 0);
    const suspectedCount = Number(item.suspected_repeat_count || 0);
    const title = `${item.date}：确认 ${count} 次，疑似 ${suspectedCount} 次；新增 ${item.total} 单；高峰 ${item.peak_period || '暂无'} ${item.peak_count || 0} 单`;
    return `<button type="button" class="feedback-day ${item.date === data.end_date ? 'selected' : ''}" data-date="${escapeHtml(item.date)}" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}，点击查看当日"><span>${count}<small> / ${suspectedCount}</small></span><div class="feedback-day-bars"><i style="height:${Math.max(2, count / dailyMax * 96)}px"></i><i class="suspected" style="height:${Math.max(2, suspectedCount / dailyMax * 96)}px"></i></div><small>${escapeHtml(item.date.slice(5))}</small></button>`;
  }).join('');
  $('feedbackDailyChart').querySelectorAll('button[data-date]').forEach(button => button.addEventListener('click', () => {
    followsLatestDate = false;
    $('datePicker').value = button.dataset.date;
    loadDashboard();
  }));
  requestAnimationFrame(() => {
    const chart = $('feedbackDailyChart');
    chart.parentElement.scrollLeft = chart.scrollWidth;
  });
  $('feedbackTrendScope').textContent = `${data.start_date || '—'} 至 ${data.end_date || '—'}；历史日按全天，所选日${data.cutoff_time ? `截至 ${data.cutoff_time}` : '按全天'}。疑似不计入重复率。`;
  const selectedWeek = data.selected_week || {};
  $('feedbackWeekSummary').innerHTML = selectedWeek.week_start ? `<span>所选日期所在周 · ${escapeHtml(selectedWeek.start_date)} 至 ${escapeHtml(selectedWeek.end_date)}</span><strong>确认 ${Number(selectedWeek.confirmed_repeat_count || 0)} 次 <small>疑似 ${Number(selectedWeek.suspected_repeat_count || 0)} 次</small></strong><span>重复率 ${Number(selectedWeek.repeat_rate || 0).toFixed(1)}% · ${selectedWeek.full_week ? '整周' : `截至所选日${selectedWeek.cutoff_time ? ` ${escapeHtml(selectedWeek.cutoff_time)}` : ''}`}</span>` : '';
  const weeks = (data.weeks || []).slice().reverse();
  $('feedbackWeekList').innerHTML = weeks.length ? `<table><thead><tr><th>统计范围</th><th>确认</th><th>疑似</th><th>重复率</th></tr></thead><tbody>${weeks.map(item => `<tr><td>${escapeHtml(item.start_date.slice(5))}—${escapeHtml(item.end_date.slice(5))}<small>${item.full_week ? '整周' : '部分周'}</small></td><td>${Number(item.confirmed_repeat_count || 0)}</td><td>${Number(item.suspected_repeat_count || 0)}</td><td>${Number(item.repeat_rate || 0).toFixed(1)}%</td></tr>`).join('')}</tbody></table>` : '<p class="empty">暂无周统计</p>';

  const hours = data.two_hour || [];
  const max = Math.max(1, ...hours.map(item => Number(item.total || 0)));
  $('feedbackPeakChart').innerHTML = hours.map(item => {
    const tianrun = Number(item.tianrun || 0);
    const merchantApp = Number(item.merchant_app || 0);
    const totalCount = Number(item.total || 0);
    const height = totalCount ? Math.max(5, totalCount / max * 140) : 2;
    const appHeight = totalCount ? merchantApp / totalCount * height : 0;
    return `<div class="feedback-hour" title="${escapeHtml(item.period)}：${totalCount} 单（天润 ${tianrun}、APP ${merchantApp}）"><span>${totalCount}</span><div class="feedback-hour-stack" style="height:${height}px"><i class="feedback-hour-tianrun"></i><i class="feedback-hour-app" style="height:${appHeight}px"></i></div><small>${escapeHtml(item.period.slice(0,5))}</small></div>`;
  }).join('');
  const rangePeak = data.range_peak || {};
  $('feedbackPeakDefinition').textContent = `${selected.peak_period || '暂无高峰'} · 天润与APP合并，条纹为APP；趋势窗口峰值 ${rangePeak.period || '—'}（${Number(rangePeak.total || 0)} 单）`;

  const sourceRows = Array.isArray(selected.source_breakdown) ? selected.source_breakdown : (data.source_breakdown || []);
  const sourceMax = Math.max(1, ...sourceRows.map(item => Number(item.count || 0)));
  $('feedbackSourceList').innerHTML = sourceRows.length ? `<div class="feedback-source-title">来源分布</div>${sourceRows.map(item => `<div class="feedback-reason"><span>${escapeHtml(item.source || item.source_label || '未分类')}</span><i><b style="width:${Number(item.count || 0) / sourceMax * 100}%"></b></i><strong>${Number(item.count || 0)}</strong></div>`).join('')}` : '';
  const reasons = Array.isArray(selected.reason_breakdown) ? selected.reason_breakdown : (data.reason_breakdown || []);
  const reasonMax = Math.max(1, ...reasons.map(item => Number(item.count || 0)));
  const rules = Array.isArray(selected.rule_breakdown) ? selected.rule_breakdown : (data.rule_breakdown || []);
  const ruleMax = Math.max(1, ...rules.map(item => Number(item.count || 0)));
  const ruleHtml = rules.length ? `<div class="feedback-source-title">命中规则</div>${rules.map(item => `<div class="feedback-reason"><span>${escapeHtml(item.rule)}</span><i><b style="width:${Number(item.count || 0) / ruleMax * 100}%"></b></i><strong>${Number(item.count || 0)}</strong></div>`).join('')}` : '';
  const reasonHtml = reasons.length ? `<div class="feedback-source-title">原因归类</div>${reasons.map(item => `<div class="feedback-reason"><span>${escapeHtml(item.reason)}</span><i><b style="width:${Number(item.count || 0) / reasonMax * 100}%"></b></i><strong>${Number(item.count || 0)}</strong></div>`).join('')}` : '';
  $('feedbackReasonList').innerHTML = ruleHtml || reasonHtml ? `${ruleHtml}${reasonHtml}` : '<p class="empty">所选日期没有规则确认的再次反馈。</p>';
  $('feedbackDefinition').textContent = data.definition || '';
  const records = data.records || [];
  if ($('feedbackDetailSummary')) $('feedbackDetailSummary').textContent = `查看所选日期重复/关联反馈（确认 ${confirmed} · 疑似 ${suspected}）`;
  if ($('feedbackRecords')) $('feedbackRecords').innerHTML = records.length ? records.map(row => `<article class="feedback-record ${row.confidence === '规则确认' ? 'confirmed' : 'suspected'}">
    <div class="feedback-record-head"><div><a href="${escapeHtml(row.source_url)}" target="_blank" rel="noopener">${escapeHtml(row.source)} ${escapeHtml(row.record_id)}</a><span>店铺 ${escapeHtml(row.shop_id || '—')} · ${escapeHtml(row.brand)}</span></div><b>${escapeHtml(row.confidence)}</b></div>
    <p>${escapeHtml(row.description)}</p>
    <div class="feedback-record-meta"><span>${escapeHtml(row.created_at)}</span><span>命中：${escapeHtml(row.match_rule || row.reason)}</span><span>原因：${escapeHtml(row.reason)}</span><span>关联：${(row.related || []).map(item => `${escapeHtml(item.source)} ${escapeHtml(item.record_id)}（${escapeHtml(item.created_at || '—')}）`).join('、')}</span>${['R1','R2','S1'].includes(row.rule_id) ? `<span>文本匹配度 ${Math.round(Number(row.match_score || 0) * 100)}%</span>` : ''}</div>
  </article>`).join('') : '<p class="empty">所选日期没有规则确认或疑似重复记录。</p>';
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
    <div class="${app.overdue_24h_count ? 'is-overdue' : ''}"><span>超24小时未完成稽查</span><strong>${Number(app.overdue_24h_count || 0).toLocaleString()}</strong><small>商家APP发起、当前未完成且创建已满24小时</small></div>
    <div><span>所选日期数据同步</span><strong class="sync-time">${escapeHtml(syncedAt)}</strong><small>${app.loaded ? 'APP新增明细已读取' : '该日期尚未确认完成同步'}</small></div>
  </div>
  <div class="merchant-sync-note ${queueStale ? 'stale' : 'fresh'}"><b>${queueStale ? '需核实队列时效' : '未完成队列同步正常'}</b><span>${escapeHtml(queueStatus)}</span><small>新增量按入口计数，同一事项若同时进入天润与APP会分别计数；超时从APP创建时间起算。</small></div>`;

  const overdue = app.overdue_24h_records || [];
  $('merchantOverdueCount').textContent = app.open_synced_at && !queueStale ? `${overdue.length} 单` : '待确认';
  $('merchantOverdueList').innerHTML = overdue.length ? overdue.map(row => `<article class="merchant-overdue-ticket">
    <div class="merchant-overdue-ticket-head"><div><a href="${escapeHtml(row.source_url)}" target="_blank" rel="noopener">稽查单 ${escapeHtml(row.inspection_id)}</a><span>事件 ${escapeHtml(row.event_id)} · 店铺 ${escapeHtml(row.shop_id)}</span></div><strong>${escapeHtml(row.age_hours ?? '—')} 小时</strong></div>
    <p>${escapeHtml(row.description)}</p><div class="merchant-ticket-meta"><span>${escapeHtml(row.brand)}</span><span>${escapeHtml(row.inspect_status)}</span><span>创建 ${escapeHtml(row.created_at)}</span>${row.risk_hits?.map(hit => `<b class="risk-tag">${escapeHtml(hit.label)}</b>`).join('') || ''}</div>
  </article>`).join('') : app.open_synced_at && !queueStale
    ? '<div class="merchant-overdue-empty"><strong>0 单</strong><span>本次已同步的当前未完成队列中，没有创建满24小时且仍未完成稽查的商家APP稽查单。</span></div>'
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

async function pollRealtimeStatus() {
  if (isPublicDashboard) {
    if (!publicLoadedRevision || publicVersionCheckRunning || publicReloading) return;
    publicVersionCheckRunning = true;
    try {
      const manifest = await fetchPublicJson('data/index.json');
      if (manifest.revision !== publicLoadedRevision) {
        publicReloading = true;
        const target = new URL(window.location.href);
        target.searchParams.set('refresh', Date.now().toString());
        window.location.replace(target.toString());
        return;
      }
      publicManifest = manifest;
      updatePublicStatus();
    } catch (error) {
      $('statusLine').className = 'status-line error';
      $('statusLine').textContent = `自动刷新失败，保留上次数据：${error.message}`;
    } finally {
      publicVersionCheckRunning = false;
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
      if (followsLatestDate && status.realtime.latest_date) $('datePicker').value = status.realtime.latest_date;
      loadDashboard({refresh: true});
    }
  } catch (_) {
    // A temporary status polling failure must not interrupt the current dashboard view.
  }
}

function renderKA(ka) {
  const totalDelta = ka.count - ka.previous_count;
  $('kaSummary').innerHTML = `<div><span>KA 工单</span><strong>${ka.count}</strong><small class="${signClass(totalDelta)}">较前日 ${signed(totalDelta)} 单</small></div>
    <div><span>KA 占全部新增</span><strong>${ka.ratio}%</strong><small class="${signClass(ka.ratio_delta)}">较前日 ${signed(ka.ratio_delta)} 个百分点</small></div>
    <div><span>涉及 KA 商家</span><strong>${ka.brand_count || 0}</strong><small>前日 ${ka.previous_count} 单 KA 工单</small></div>`;
}

async function syncData() {
  if (isPublicDashboard) return loadPublicDashboard();
  if (isReadOnlyDashboard) return;
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
        clearInterval(timer); button.disabled = false; button.textContent = '同步最新数据'; loadDashboard({refresh: true});
      }
    }, 1500);
  } catch (error) {
    button.disabled = false; button.textContent = '同步最新数据';
    $('statusLine').className = 'status-line error'; $('statusLine').textContent = error.message;
  }
}

$('datePicker').addEventListener('change', () => { followsLatestDate = false; loadDashboard(); });
$('windowPicker').addEventListener('change', loadDashboard);
$('completionDatePicker').addEventListener('change', () => { completionDateChanged = true; completionPage = 1; loadCompletionDay(); });
['completionCategoryFilter', 'completionResultFilter'].forEach(id => $(id).addEventListener('change', () => { completionPage = 1; renderCompletionRecords(); }));
$('completionPrev').addEventListener('click', () => { if (completionPage > 1) { completionPage--; renderCompletionRecords(); } });
$('completionNext').addEventListener('click', () => { if (completionPage < completionPages) { completionPage++; renderCompletionRecords(); } });
document.querySelector('.completion-details').addEventListener('toggle', event => { if (event.currentTarget.open) renderCompletionRecords(); });
if ($('syncBtn')) $('syncBtn').addEventListener('click', syncData);
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
    const completionWeeks = manifest.completion_weeks || [];
    if (completionWeeks.length) $('completionDatePicker').min = completionWeeks[0];
    $('completionDatePicker').max = manifest.latest_date;
    $('datePicker').min = manifest.dates[manifest.dates.length - 1];
    $('datePicker').max = manifest.latest_date;
    $('datePicker').value = selected;
    const windowSize = Number($('windowPicker').value);
    dashboardData = {...snapshot.dashboard, daily_trend: snapshot.dashboard.daily_trend.slice(-windowSize)};
    render(dashboardData, null, null);
    updatePublicStatus();
  } catch (error) {
    if (requestId !== publicRequest) return;
    $('statusLine').className = 'status-line error';
    $('statusLine').textContent = `${error.message}${publicSnapshot ? '；保留上次成功读取的数据' : ''}`;
  } finally {
    if (requestId === publicRequest) button.disabled = false;
  }
}

if (isPublicDashboard && $('syncBtn')) {
  $('syncBtn').textContent = '刷新公开数据';
}
loadDashboard();
if (isPublicDashboard) {
  const schedulePublicVersionCheck = () => {
    const elapsed = Date.now() - publicPageOpenedAt;
    const delay = elapsed < 10 * 60 * 1000 ? 10 * 1000 : 60 * 1000;
    window.setTimeout(async () => {
      await pollRealtimeStatus();
      schedulePublicVersionCheck();
    }, delay);
  };
  const checkAfterResume = () => { void pollRealtimeStatus(); };
  window.addEventListener('focus', checkAfterResume);
  window.addEventListener('pageshow', checkAfterResume);
  window.addEventListener('online', checkAfterResume);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkAfterResume();
  });
  schedulePublicVersionCheck();
} else {
  window.setInterval(pollRealtimeStatus, 30000);
}
