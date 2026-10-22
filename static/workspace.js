const $ = id => document.getElementById(id);
const number = value => Number(value || 0).toLocaleString('zh-CN');
const displayNumber = value => value == null ? '—' : Number(value).toLocaleString('zh-CN');
const percent = (part, total) => total ? `${(Number(part || 0) / Number(total) * 100).toFixed(1)}%` : '—';
const safe = value => String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

const views = {
  employee: {
    title: '员工工单卡点',
    eyebrow: 'EMPLOYEE BOTTLENECK',
    description: '查看员工存量、账龄、阶段卡点和完结效率',
    url: '/employee-dashboard?embed=1'
  },
  daily: {
    title: '每日新增工单分析',
    eyebrow: 'DAILY INTAKE',
    description: '查看新增趋势、问题类型、异常记录、KA 商家和逐单证据',
    url: '/daily?embed=1'
  }
};
let reportArchive = [];
let selectedReportId = '';
let reportLoading = false;

function renderTrend(target, rows, keys) {
  const el = $(target);
  if (!rows || !rows.length) {
    el.innerHTML = '<div class="empty-chart">暂无趋势数据</div>';
    return;
  }
  const max = Math.max(1, ...rows.flatMap(row => keys.map(key => Number(row[key.name] || 0))));
  el.innerHTML = rows.map(row => `
    <div class="chart-group">
      <div class="chart-bars">
        ${keys.map(key => `<span class="chart-bar ${key.className || ''}" style="height:${Math.max(3, Number(row[key.name] || 0) / max * 100)}%" data-value="${safe(key.label)} ${number(row[key.name])}"></span>`).join('')}
      </div>
      <label>${safe(String(row.date || '').slice(5))}</label>
    </div>`).join('');
}

async function loadOverview() {
  const button = $('overviewRefresh');
  button.disabled = true;
  button.textContent = '正在刷新…';
  $('overviewNotice').className = 'overview-notice';
  $('overviewNotice').textContent = '正在读取两个看板的数据…';
  try {
    const response = await fetch('/api/workspace/overview?t=' + Date.now(), {cache: 'no-store'});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || '读取失败');
    const daily = payload.daily || {};
    const employee = payload.employee || {};
    const employeeAvailable = employee.available !== false;
    $('metricStock').textContent = displayNumber(employee.tickets);
    $('metricEmployees').textContent = employeeAvailable ? `覆盖 ${displayNumber(employee.employees)} 人` : '员工快照未生成';
    $('metricOver48').textContent = displayNumber(employee.over48);
    $('metricOver48Rate').textContent = employeeAvailable ? `占当前存量 ${percent(employee.over48, employee.tickets)}` : '等待员工数据';
    $('metricOver120').textContent = displayNumber(employee.over120);
    $('metricOver120Rate').textContent = employeeAvailable ? `占当前存量 ${percent(employee.over120, employee.tickets)}` : '等待员工数据';
    $('metricDaily').textContent = number(daily.total);
    const change = Number(daily.total || 0) - Number(daily.previous || 0);
    $('metricDailyCompare').textContent = `较昨日 ${change > 0 ? '+' : ''}${number(change)}（${Number(daily.pct || 0).toFixed(1)}%）`;
    $('metricRisk').textContent = number(daily.risk_ticket_count);
    $('metricAnomaly').textContent = `需关注记录 ${number(daily.record_anomaly_count)}`;
    $('employeeBadge').textContent = displayNumber(employee.tickets);
    $('dailyBadge').textContent = number(daily.total);
    $('focusEmployee').textContent = employeeAvailable ? (employee.busiest?.name || '—') : '—';
    $('focusEmployeeCount').textContent = employeeAvailable ? `当前 ${displayNumber(employee.busiest?.total)} 张` : '员工快照未生成';
    $('focusCompleted').textContent = displayNumber(employee.completed_today);
    $('focusCreated').textContent = employeeAvailable ? `今日创建 ${displayNumber(employee.created_today)}` : '员工快照未生成';
    $('focusDriver').textContent = daily.top_driver?.category || '暂无明显增长';
    $('focusDriverDelta').textContent = daily.top_driver?.category ? `较昨日 ${Number(daily.top_driver.delta || 0) >= 0 ? '+' : ''}${number(daily.top_driver.delta)}` : '当前结构相对稳定';
    $('focusDate').textContent = daily.selected_date || '—';
    $('focusDailyUpdate').textContent = daily.generated_at ? `源数据 ${daily.generated_at}` : '源数据时间待确认';
    $('overviewSubtitle').textContent = `${daily.selected_date || '当前'} · 员工卡点与每日新增统一视图`;
    $('sidebarUpdate').textContent = employeeAvailable ? (employee.generated_at ? `员工快照 ${employee.generated_at}` : '员工快照时间待确认') : '员工快照未生成';
    $('overviewNotice').textContent = employeeAvailable
      ? `员工看板更新于 ${employee.generated_at || '待确认'}；每日新增数据日期 ${daily.selected_date || '待确认'}。`
      : `员工快照未生成；每日新增数据日期 ${daily.selected_date || '待确认'}。`;
    renderTrend('employeeTrend', employee.trend, [{name:'created', label:'创建'}, {name:'completed', label:'完结', className:'completed'}]);
    renderTrend('dailyTrend', daily.trend, [{name:'total', label:'新增'}]);
  } catch (error) {
    $('overviewNotice').className = 'overview-notice error';
    $('overviewNotice').textContent = `综合数据暂时无法读取：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = '刷新数据';
  }
}

function formatReportTime(value) {
  if (!value) return '完成时间待记录';
  return String(value).replace('T', ' ').replace(/\+08:00$/, '').slice(0, 19);
}

function selectReport(reportId) {
  const report = reportArchive.find(item => item.id === reportId);
  if (!report) return;
  selectedReportId = report.id;
  document.querySelectorAll('.report-list-item').forEach(item => item.classList.toggle('active', item.dataset.reportId === report.id));
  $('reportTitle').textContent = `${report.date} · ${report.slot_label}`;
  $('reportMeta').textContent = `${formatReportTime(report.finished_at)} · ${number(report.screenshot_count)} 张看板截图`;
  $('reportStatus').hidden = false;
  $('reportStatus').className = `report-status ${report.status === 'success' ? 'success' : 'partial'}`;
  $('reportStatus').textContent = report.status_label;
  $('reportSeparate').href = report.report_url;
  $('reportSeparate').hidden = false;
  $('reportDownload').href = report.data_url;
  $('reportDownload').hidden = false;
  $('reportMarkdown').href = report.markdown_url;
  $('reportMarkdown').hidden = false;
  $('reportCopy').hidden = false;
  $('reportCopy').textContent = '复制正文';
  $('reportCopy').dataset.url = report.markdown_url.replace('?download=1', '');
  $('reportEmpty').hidden = true;
  $('reportFrame').hidden = false;
  if ($('reportFrame').dataset.revision !== `${report.id}:${report.revision}`) {
    $('reportFrame').src = `${report.embed_url}&v=${encodeURIComponent(report.revision)}`;
    $('reportFrame').dataset.revision = `${report.id}:${report.revision}`;
  }
}

function renderReportList() {
  const visible = reportArchive.filter(report => !$('reportDateFilter').value || report.date === $('reportDateFilter').value);
  if (!visible.length) {
    $('reportList').innerHTML = '<div class="report-list-empty">暂无正式日报</div>';
    $('reportTitle').textContent = '暂无日报';
    $('reportMeta').textContent = '日报生成后会自动出现在这里';
    $('reportEmpty').hidden = false;
    $('reportFrame').hidden = true;
    $('reportStatus').hidden = true;
    $('reportSeparate').hidden = true;
    $('reportDownload').hidden = true;
    $('reportMarkdown').hidden = true;
    $('reportCopy').hidden = true;
    return;
  }
  let lastDate = '';
  $('reportList').innerHTML = visible.map(report => {
    const dateHeading = report.date !== lastDate ? `<div class="report-date-heading">${safe(report.date)}</div>` : '';
    lastDate = report.date;
    return `${dateHeading}<button class="report-list-item" data-report-id="${safe(report.id)}" type="button">
      <span class="report-list-main"><strong>${safe(report.slot_label)}</strong><small>${safe(formatReportTime(report.finished_at).slice(11) || '待记录')}</small></span>
      <span class="report-list-side"><i class="${report.status === 'success' ? 'success' : 'partial'}"></i><small>${safe(report.status_label)}</small></span>
    </button>`;
  }).join('');
  document.querySelectorAll('.report-list-item').forEach(item => item.addEventListener('click', () => selectReport(item.dataset.reportId)));
  const preferred = visible.find(item => item.id === selectedReportId) || visible[0];
  selectReport(preferred.id);
}

function renderReportArchive(payload) {
  reportArchive = Array.isArray(payload.reports) ? payload.reports : [];
  const summary = payload.summary || {};
  const selectedDate = $('reportDateFilter').value;
  const dates = [...new Set(reportArchive.map(report => report.date))];
  $('reportDateFilter').innerHTML = '<option value="">全部日期</option>' + dates.map(day => `<option value="${safe(day)}">${safe(day)}</option>`).join('');
  $('reportDateFilter').value = dates.includes(selectedDate) ? selectedDate : '';
  $('reportsBadge').textContent = number(summary.report_count || 0);
  $('reportArchiveCount').textContent = `${number(summary.day_count || 0)} 天 · ${number(summary.report_count || 0)} 份`;
  $('reportsSubtitle').textContent = `每日 ${Array.isArray(summary.schedule) ? summary.schedule.join('、') : '18:00、23:59'} 自动归档，保留两个时段的日报`;
  renderReportList();
}

async function loadReports(showLoading = true) {
  if (reportLoading) return;
  reportLoading = true;
  const button = $('reportsRefresh');
  if (showLoading) {
    button.disabled = true;
    button.textContent = '正在刷新…';
    $('reportsNotice').className = 'overview-notice';
    $('reportsNotice').textContent = '正在读取日报归档…';
  }
  try {
    const response = await fetch('/api/reports?t=' + Date.now(), {cache: 'no-store'});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || '读取失败');
    renderReportArchive(payload);
    $('reportsNotice').className = 'overview-notice';
    const latest = payload.summary?.latest;
    $('reportsNotice').textContent = latest
      ? `最新归档：${latest.date} ${latest.slot_label}，${latest.status_label}。后续定时生成完成后会自动出现在此处。`
      : '尚未发现正式日报；下一次定时生成完成后会自动归档。';
  } catch (error) {
    $('reportsNotice').className = 'overview-notice error';
    $('reportsNotice').textContent = `日报归档暂时无法读取：${error.message}`;
  } finally {
    reportLoading = false;
    if (showLoading) {
      button.disabled = false;
      button.textContent = '刷新归档';
    }
  }
}

async function copyReport() {
  const button = $('reportCopy');
  button.disabled = true;
  try {
    const response = await fetch(button.dataset.url, {cache: 'no-store'});
    if (!response.ok) throw new Error('读取失败');
    const markdown = await response.text();
    const text = markdown.split('\n').filter(line => !line.trim().startsWith('![')).join('\n')
      .replace(/^#{1,6}\s+/gm, '').replace(/`/g, '').replace(/\n{3,}/g, '\n\n').trim();
    await navigator.clipboard.writeText(text);
    button.textContent = '已复制';
  } catch (_error) {
    button.textContent = '复制失败，请下载报告';
  } finally {
    button.disabled = false;
  }
}

function showView(name, updateHash = true) {
  const view = name === 'overview' || name === 'reports' || views[name] ? name : 'overview';
  document.querySelectorAll('.nav-item[data-view]').forEach(item => item.classList.toggle('active', item.dataset.view === view));
  $('overviewView').hidden = view !== 'overview';
  $('reportsView').hidden = view !== 'reports';
  $('frameView').hidden = view === 'overview' || view === 'reports';
  if (view === 'overview') {
    if (updateHash) history.replaceState(null, '', '#overview');
    loadOverview();
  } else if (view === 'reports') {
    if (updateHash) history.replaceState(null, '', '#reports');
    loadReports();
  } else {
    const config = views[view];
    $('frameEyebrow').textContent = config.eyebrow;
    $('frameTitle').textContent = config.title;
    $('frameDescription').textContent = config.description;
    if ($('dashboardFrame').dataset.view !== view) {
      $('dashboardFrame').src = config.url;
      $('dashboardFrame').dataset.view = view;
    }
    $('openSeparate').dataset.url = config.url.replace('?embed=1', '');
    if (updateHash) history.replaceState(null, '', '#' + view);
  }
  closeSidebar();
}

function closeSidebar() {
  $('workspaceSidebar').classList.remove('open');
  $('sidebarMask').classList.remove('open');
}

document.querySelectorAll('.nav-item[data-view]').forEach(item => item.addEventListener('click', () => showView(item.dataset.view)));
document.querySelectorAll('[data-open-view]').forEach(item => item.addEventListener('click', () => showView(item.dataset.openView)));
$('overviewRefresh').addEventListener('click', loadOverview);
$('reportsRefresh').addEventListener('click', () => loadReports(true));
$('reportDateFilter').addEventListener('change', renderReportList);
$('reportCopy').addEventListener('click', copyReport);
$('openSeparate').addEventListener('click', () => window.open($('openSeparate').dataset.url, '_blank', 'noopener'));
$('menuToggle').addEventListener('click', () => { $('workspaceSidebar').classList.add('open'); $('sidebarMask').classList.add('open'); });
$('sidebarMask').addEventListener('click', closeSidebar);
window.addEventListener('hashchange', () => showView(location.hash.slice(1) || 'overview', false));
const initialView = location.hash.slice(1) || 'overview';
showView(initialView, false);
if (initialView !== 'overview') loadOverview();
if (initialView !== 'reports') loadReports(false);
setInterval(() => {
  if (!document.hidden) loadReports(false);
}, 60000);
