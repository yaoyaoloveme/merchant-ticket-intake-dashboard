'use strict';

// Private, local-only assignment workflow. Public snapshots exclude this file.
window.DashboardDispatch = (() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const categories = ['多付/漏付', '批量核实', '回访工单', '介入问题', '其他'];
  const short = value => ({'多付/漏付':'多付/漏付','批量核实':'批量','回访工单':'回访','介入问题':'介入'}[value] || value);
  const today = () => new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date());
  let rosterDate = today();
  let tickets = [], members = [], selectedTickets = new Set(), selectedMembers = new Set();
  let connected = false, enabled = false, busy = false, verifying = false, loadingPool = false;
  let preview = null, results = null, overrides = {}, expiresAt = 0, queueFresh = false;
  let auditRows = [], initialized = false;

  async function api(path, options = {}) {
    const response = await fetch(`/api/tianrun/${path}`, {cache:'no-store', ...options});
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error('未收到完整响应，请刷新分派记录核对提交情况。'); }
    if (!response.ok || !body.ok) throw new Error(body.error || `请求失败（${response.status}）`);
    return body.data;
  }
  const post = (path, body) => api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const counts = rows => rows.reduce((out, row) => {out[row.category] = (out[row.category] || 0) + 1; return out;}, {});
  const chips = values => Object.entries(values).filter(([, n]) => n).map(([key, n]) => `<span>${esc(short(key))} <b>${n}</b></span>`).join('');
  const chosenTickets = () => tickets.filter(row => selectedTickets.has(row.ticket_id));
  const chosenMembers = () => members.filter(row => selectedMembers.has(row.name));

  function rememberRoster() {
    try { localStorage.setItem('ticket-dispatch-roster', JSON.stringify({date:rosterDate, names:[...selectedMembers]})); } catch (_) { /* Storage is optional. */ }
  }
  function checkDay() {
    if (rosterDate === today()) return;
    rosterDate = today(); selectedMembers.clear(); rememberRoster(); invalidate();
    renderMembers(); updateSelection();
  }
  function freeze(value) {
    busy = value;
    document.querySelectorAll('#dispatchPanel input, #dispatchPanel textarea, #dispatchPanel select, #dispatchPanel button').forEach(node => {
      if (value) { node.dataset.wasDisabled = String(node.disabled); node.disabled = true; }
      else { node.disabled = node.dataset.wasDisabled === 'true'; delete node.dataset.wasDisabled; }
    });
  }
  function invalidate() {
    preview = null; overrides = {}; expiresAt = 0;
    if (!results) $('assignmentPreview').innerHTML = '<p class="empty">选择已更新，请生成新方案后核对接收人。</p>';
  }
  function renderStatus(data) {
    enabled = Boolean(data.assign_enabled); connected = data.connection === 'ok';
    $('tianrunConnectionBadge').className = `connection-badge ${connected ? 'ok' : 'locked'}`;
    $('tianrunConnectionBadge').textContent = connected ? '天润已连接' : data.connection === 'expired' ? '登录已失效' : '连接待恢复';
    $('tianrunConnectionText').textContent = connected ? '可生成方案并提交 · 每 5 分钟刷新工单与记录' : data.message || '请更新登录后检测连接';
    $('assignModeBadge').className = `connection-badge ${enabled ? 'ok' : 'locked'}`;
    $('assignModeBadge').textContent = enabled ? '真实分派已开启' : '真实分派关闭';
    if (!connected) $('connectionSettings').open = true;
    updateSelection();
  }
  async function loadStatus() {
    try { renderStatus(await api('status?test=1')); }
    catch (error) { renderStatus({message:error.message}); }
  }
  async function saveCookie() {
    const cookie = $('tianrunCookie').value.trim();
    if (!cookie) { $('tianrunConnectionText').textContent = '请先粘贴新 Cookie。'; return; }
    freeze(true);
    try {
      const data = await api('cookie', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cookie})});
      $('tianrunCookie').value = ''; renderStatus(data); $('connectionSettings').open = false;
    } catch (error) { $('tianrunConnectionText').textContent = error.message; }
    finally { freeze(false); updateSelection(); }
  }
  async function loadMembers() {
    try {
      members = await api('team');
      selectedMembers = new Set([...selectedMembers].filter(name => members.some(row => row.name === name)));
      renderMembers(); updateSelection();
    } catch (error) { $('teamMemberList').textContent = error.message; }
  }
  function renderMembers() {
    $('teamMemberList').innerHTML = [...members].sort((a,b) => a.assigned_today - b.assigned_today).map(row => `<label class="member-choice">
      <input type="checkbox" data-member="${esc(row.name)}" ${selectedMembers.has(row.name) ? 'checked' : ''} ${busy ? 'disabled' : ''}>
      <span class="member-card-body"><span class="member-card-head"><strong>${esc(row.name)}</strong><b>今日提交 ${Number(row.assigned_today)} 单</b></span>
      <span class="member-verification">已核验 ${Number(row.verified_today || 0)} · 待核验 ${Number(row.pending_today || 0)}</span>
      <span class="member-category-stats">${chips(row.category_counts)}${row.assigned_today ? '' : '<span>今日暂无提交</span>'}</span></span></label>`).join('');
  }
  function filteredTickets() {
    const category = $('dispatchCategory').value, wait = Number($('dispatchWait').value), risk = $('dispatchRisk').value;
    const ka = $('dispatchKa').value, query = $('dispatchSearch').value.trim().toLowerCase();
    return tickets.filter(row => (!category || row.category === category) && (!wait || Number(row.duration_minutes) >= wait) &&
      (!risk || (risk === 'any' ? row.risk_hits.length : row.risk_level === risk)) &&
      (!ka || (ka === 'ka' ? row.is_ka : !row.is_ka)) &&
      (!query || [row.ticket_id,row.shop_id,row.brand,row.description,row.category_raw].join(' ').toLowerCase().includes(query)));
  }
  function renderPool() {
    const visible = filteredTickets();
    $('pendingTicketCount').textContent = `${tickets.length} 单`;
    $('dispatchVisibleCount').textContent = `筛选显示 ${visible.length} 单 · 可选 ${visible.filter(row => !row.assignment_blocked).length} 单`;
    $('dispatchPoolStats').innerHTML = categories.map(category => `<button type="button" data-category="${esc(category)}" class="pool-stat ${$('dispatchCategory').value === category ? 'active' : ''}"><strong>${tickets.filter(row => row.category === category).length}</strong><span>${esc(short(category))}</span></button>`).join('');
    $('pendingTicketList').innerHTML = visible.length ? visible.map(row => `<article class="dispatch-ticket ${selectedTickets.has(row.ticket_id) ? 'selected' : ''}">
      <label class="ticket-choice"><input type="checkbox" data-ticket="${esc(row.ticket_id)}" ${selectedTickets.has(row.ticket_id) ? 'checked' : ''} ${row.assignment_blocked || busy ? 'disabled' : ''}>
      <span class="dispatch-ticket-body"><span class="dispatch-ticket-head"><span class="ticket-identity"><strong>${esc(row.ticket_id)}</strong><small>店铺 ${esc(row.shop_id) || '—'}${row.is_ka ? ` · KA ${esc(row.brand)}` : ''}</small></span><span class="ticket-category">${esc(row.category)}</span><span class="ticket-age">等待 ${esc(row.duration_text) || '时长未知'}</span></span>
      <strong class="dispatch-demand">${esc(row.demand)}</strong><span class="ticket-description">${esc(row.description) || '未填写问题描述'}</span>
      <span class="dispatch-chips risk-evidence">${row.risk_hits.map(hit => `<span class="risk-${hit.level === '紧急' ? 'urgent' : 'high'}">${esc(hit.label)} · 原文“${esc(hit.matched_text)}”</span>`).join('')}${row.assignment_blocked ? `<span class="risk-high">${esc(row.assignment_blocked)} · 请先核验</span>` : ''}</span>
      <small class="ticket-meta">创建 ${esc(row.created_at)} · ${esc(row.category_raw)}</small></span></label>
      <details class="dispatch-original"><summary>查看完整原文</summary><p>${esc(row.description) || '未填写'}</p><a href="${esc(row.source_url)}" target="_blank" rel="noopener">打开源工单 ↗</a></details></article>`).join('') : '<p class="empty">当前筛选下没有工单，可调整筛选条件。</p>';
  }
  async function loadPool(refresh = true) {
    if (loadingPool) return;
    loadingPool = true; queueFresh = false; updateSelection();
    $('pendingTicketStatus').textContent = '正在读取最新待领取队列，保留尚可分派的选择…';
    try {
      const data = await api(`pending-tickets${refresh ? '?refresh=1' : ''}`);
      tickets = data.tickets; queueFresh = !data.warning;
      const before = selectedTickets.size;
      selectedTickets = new Set([...selectedTickets].filter(id => tickets.some(row => row.ticket_id === id && !row.assignment_blocked)));
      const removed = before - selectedTickets.size;
      if (removed && preview) invalidate();
      $('pendingTicketStatus').textContent = data.warning || `本次读取 ${data.read_at?.replace('T',' ') || '—'} · 数据采集 ${data.refreshed_at?.replace('T',' ') || '当前为空'}${removed ? ` · ${removed} 张已离开队列或待核验的工单已移出选择` : ''}`;
      $('pendingTicketStatus').classList.toggle('dispatch-error', !queueFresh);
      renderPool();
    } catch (error) { $('pendingTicketStatus').textContent = error.message; }
    finally { loadingPool = false; updateSelection(); }
  }
  function estimate() {
    const staff = chosenMembers(), rows = chosenTickets();
    const totals = Object.fromEntries(staff.map(row => [row.name, row.assigned_today]));
    const types = Object.fromEntries(staff.map(row => [row.name, {...row.category_counts}]));
    const added = Object.fromEntries(staff.map(row => [row.name, 0]));
    [...rows].sort((a,b) => ({'紧急':0,'高':1}[a.risk_level] ?? 3) - ({'紧急':0,'高':1}[b.risk_level] ?? 3) || (b.duration_minutes || 0) - (a.duration_minutes || 0)).forEach(row => {
      if (!staff.length) return;
      const fixed = row.risk_level ? $('riskRecipient').value : '';
      const name = fixed || [...staff].sort((a,b) => totals[a.name] - totals[b.name] || (types[a.name][row.category] || 0) - (types[b.name][row.category] || 0))[0].name;
      added[name]++; totals[name]++; types[name][row.category] = (types[name][row.category] || 0) + 1;
    });
    return staff.map(row => ({name:row.name, count:added[row.name]}));
  }
  function updateSelection() {
    const rows = chosenTickets(), staff = chosenMembers();
    $('selectedMemberCount').textContent = `已选 ${staff.length} 人`;
    $('selectionSummary').textContent = `已选 ${rows.length} 单 · ${staff.length} 人`;
    $('dispatchSelectedTypes').innerHTML = chips(counts(rows));
    $('dispatchSelectedNames').textContent = staff.length ? staff.map(row => row.name).join('、') : '请勾选实际当班人员';
    const oldRiskRecipient = $('riskRecipient').value;
    $('riskRecipient').innerHTML = '<option value="">参与均衡推荐</option>' + staff.map(row => `<option value="${esc(row.name)}">${esc(row.name)}</option>`).join('');
    if (staff.some(row => row.name === oldRiskRecipient)) $('riskRecipient').value = oldRiskRecipient;
    $('dispatchEstimate').innerHTML = rows.length && staff.length ? `<small>预计本批新增 · 以服务端方案为准</small><div class="dispatch-chips">${estimate().map(row => `<span>${esc(row.name)} +${row.count}</span>`).join('')}</div>` : '';
    const hint = busy ? '正在处理，请稍候…' : !connected ? '请先恢复天润连接' : !queueFresh ? '请先刷新并成功读取最新队列' : !rows.length ? '请先勾选待分派工单' : rows.length > 100 ? '单次最多分派 100 单，请减少选择' : !staff.length ? '请勾选实际当班人员' : !$('assignReason').value.trim() ? '请填写分派理由' : !enabled ? '真实分派未开启，可生成方案查看' : '';
    const hidden = rows.length - filteredTickets().filter(row => selectedTickets.has(row.ticket_id)).length;
    $('dispatchSelectionHint').textContent = hint || `参考今日提交总量和同类型数量均衡推荐${hidden ? ` · 包含 ${hidden} 张筛选外已选工单` : ''}`;
    $('previewAssignBtn').disabled = busy || !connected || !queueFresh || !rows.length || rows.length > 100 || !staff.length || !$('assignReason').value.trim();
  }
  async function generatePlan(keepOverrides = false) {
    checkDay();
    const previousToken = preview?.confirm_token;
    if (!keepOverrides) overrides = {};
    const payload = {ticket_ids:[...selectedTickets], members:chosenMembers().map(row => row.name), reason:$('assignReason').value.trim(), risk_recipient:$('riskRecipient').value, overrides, previous_token:previousToken};
    preview = null; results = null; freeze(true);
    $('assignmentPreview').innerHTML = '<p class="empty">正在核对最新队列与员工账号，并参考今日提交量生成方案…</p>';
    try {
      preview = await post('assignment/batch-preview', payload); expiresAt = Date.now() + preview.expires_in * 1000;
    } catch (error) { $('assignmentPreview').innerHTML = `<p class="dispatch-error">${esc(error.message)} · 请刷新连接与工单池后重新生成方案。</p>`; }
    finally { freeze(false); updateSelection(); }
    if (preview) { renderPlan(); $('assignmentPreview').scrollIntoView({behavior:'smooth', block:'nearest'}); }
  }
  function renderPlan() {
    if (!preview) return;
    $('assignmentPreview').innerHTML = `<article class="preview-card"><div class="dispatch-block-head"><h3>分派方案 · ${preview.assignments.length} 单</h3><span id="dispatchExpiry" class="dispatch-help">方案 5 分钟内有效</span></div>
      <p class="dispatch-help">今日记录 → 本批新增 → 提交后合计（均为本看板记录）。可在下方逐单更换接收人，调整后须更新方案。</p>
      <div class="batch-preview-summary">${preview.distribution.map(row => `<span>${esc(row.name)} ${row.before} + ${row.count} → ${row.after}</span>`).join('')}</div>
      <div class="batch-preview-table"><table class="dispatch-plan-table"><thead><tr><th>工单 / 类型</th><th>商家问题与风险线索</th><th>接收人</th></tr></thead><tbody>${preview.assignments.map(item => `<tr><td><strong>${esc(item.ticket.ticket_id)}</strong><br>${esc(item.ticket.category)}<br><small>店铺 ${esc(item.ticket.shop_id)}</small></td><td><strong>${esc(item.ticket.demand)}</strong><p>${esc(item.ticket.description)}</p>${item.ticket.risk_hits.map(hit => `<small class="dispatch-error">${esc(hit.label)}：“${esc(hit.matched_text)}”</small>`).join('<br>')}</td><td><select aria-label="工单 ${esc(item.ticket.ticket_id)} 接收人" data-recipient="${esc(item.ticket.ticket_id)}">${preview.members.map(name => `<option value="${esc(name)}" ${name === item.user.name ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select></td></tr>`).join('')}</tbody></table></div>
      <div class="confirm-row"><span id="dispatchPlanHint">核对完成后点击一次执行，将真实提交到天润。</span><button id="executeAssignBtn" type="button" ${enabled ? '' : 'disabled'}>${enabled ? '确认并执行分派' : '真实分派未开启'}</button></div></article>`;
    $('executeAssignBtn').addEventListener('click', () => preview?.dirty ? generatePlan(true) : executePlan());
  }
  async function executePlan() {
    if (!preview?.confirm_token || busy) return;
    if (Date.now() >= expiresAt) { $('dispatchPlanHint').textContent = '方案已过期，请重新生成。'; $('executeAssignBtn').disabled = true; return; }
    const token = preview.confirm_token;
    preview = null; freeze(true); $('executeAssignBtn').textContent = '逐单提交中，请勿重复操作…';
    // Read the durable audit while the one-use request is executing.
    const poll = window.setInterval(loadAudit, 6000);
    try {
      results = (await post('assignment/batch-execute', {confirm_token:token})).results;
      selectedTickets.clear(); renderResults();
    } catch (error) {
      $('assignmentPreview').innerHTML = `<div class="status-line error">${esc(error.message)}<br>若发生连接中断，部分请求可能已发出。请查看分派记录核验结果，勿直接重复提交。</div>`;
      document.querySelector('.dispatch-history').open = true;
    } finally {
      clearInterval(poll); freeze(false);
      await Promise.allSettled([loadPool(), loadMembers(), loadAudit(), loadStatus()]); updateSelection();
    }
    await verifyResults();
  }
  function tone(state) { return ['已核验','幂等完成'].includes(state) ? 'success' : ['失败','未提交'].includes(state) ? 'error' : 'pending'; }
  function renderResults() {
    if (!results) return;
    const states = results.reduce((out,row) => {out[row.state] = (out[row.state] || 0) + 1; return out;}, {});
    const retry = results.filter(row => ['失败','未提交'].includes(row.state));
    $('assignmentPreview').innerHTML = `<article class="preview-card"><h3>本批结果 · ${results.length} 单</h3><div class="dispatch-chips">${Object.entries(states).map(([state,count]) => `<span class="audit-state ${tone(state)}">${esc(state)} ${count}</span>`).join('')}</div>
      <p class="dispatch-help">受理不代表已分派完成。系统会核验源工单库处理人；待核验或结果不明的工单禁止重复提交。</p>
      <div class="batch-preview-table"><table class="dispatch-result-table"><thead><tr><th>工单 / 类型</th><th>接收人</th><th>结果</th><th>说明</th></tr></thead><tbody>${results.map(row => `<tr><td>${esc(row.ticket_id)}<br>${esc(row.category)}</td><td>${esc(row.recipient)}</td><td><span class="audit-state ${tone(row.state)}">${esc(row.state)}</span></td><td>${esc(row.message)}</td></tr>`).join('')}</tbody></table></div>
      ${retry.length ? `<button id="retryFailedBtn" class="secondary-btn" type="button">仅为失败 / 未提交的 ${retry.length} 单重新生成方案</button>` : ''}</article>`;
    $('retryFailedBtn')?.addEventListener('click', async () => {
      const ids = retry.map(row => row.ticket_id);
      await loadPool();
      selectedTickets = new Set(ids.filter(id => tickets.some(row => row.ticket_id === id && !row.assignment_blocked)));
      results = null; invalidate(); renderPool(); updateSelection();
      if (selectedTickets.size) await generatePlan();
      else $('assignmentPreview').innerHTML = '<p class="empty">这些工单已不在可分派队列中，请核对最新记录。</p>';
    });
  }
  async function loadAudit() {
    try {
      auditRows = await api('assignment/audit');
      $('assignmentAudit').innerHTML = auditRows.length ? auditRows.map(row => `<tr><td>${esc(row.requested_at?.replace('T',' '))}</td><td>${esc(row.ticket_id)}<br><small>${esc(row.category)}</small></td><td>${esc(row.old_handler)}</td><td>${esc(row.target_name)}</td><td><span class="audit-state ${tone(row.state)}">${esc(row.state)}</span></td><td>${esc(row.message)}</td></tr>`).join('') : '<tr><td colspan="6" class="empty">暂无分派记录</td></tr>';
      const pending = auditRows.filter(row => !['失败','未提交','已核验','幂等完成'].includes(row.state)).length;
      $('dispatchAuditCount').textContent = pending ? `· 最近记录中 ${pending} 单待核验` : '';
      if (results) {
        results.forEach(row => { const audit = auditRows.find(item => item.id === row.audit_id); if (audit) {row.state = audit.state; row.message = audit.message;} });
        renderResults();
      }
    } catch (error) { $('dispatchVerifyStatus').textContent = error.message; }
  }
  async function verifyResults() {
    if (verifying || busy) return;
    verifying = true; $('verifyAssignBtn').disabled = true;
    $('dispatchVerifyStatus').textContent = '正在读取源工单库，逐单核对目标处理人…';
    try {
      const data = await post('assignment/verify', {});
      $('dispatchVerifyStatus').textContent = `本次核验确认 ${data.verified.length} 单，仍待确认 ${data.remaining} 单。${data.source}${data.warnings.length ? ' · ' + data.warnings.join('；') : ''}`;
      await Promise.allSettled([loadAudit(), loadMembers(), loadPool()]);
    } catch (error) { $('dispatchVerifyStatus').textContent = error.message; }
    finally { verifying = false; $('verifyAssignBtn').disabled = busy; }
  }
  function selectionChanged() { results = null; invalidate(); renderPool(); updateSelection(); }
  function init() {
    if (initialized || window.TICKET_PUBLIC || location.protocol === 'file:') return;
    initialized = true; $('dispatchPanel').hidden = false;
    try { const saved = JSON.parse(localStorage.getItem('ticket-dispatch-roster') || '{}'); if (saved.date === rosterDate && Array.isArray(saved.names)) selectedMembers = new Set(saved.names); } catch (_) { /* Start with no roster. */ }
    $('teamMemberList').addEventListener('change', event => {
      if (busy || !event.target.dataset.member) return;
      checkDay(); const name = event.target.dataset.member;
      event.target.checked ? selectedMembers.add(name) : selectedMembers.delete(name);
      rememberRoster(); selectionChanged();
    });
    $('pendingTicketList').addEventListener('change', event => {
      if (busy || !event.target.dataset.ticket) return;
      event.target.checked ? selectedTickets.add(event.target.dataset.ticket) : selectedTickets.delete(event.target.dataset.ticket);
      selectionChanged();
    });
    $('dispatchPoolStats').addEventListener('click', event => {
      const button = event.target.closest('[data-category]'); if (!button || busy) return;
      $('dispatchCategory').value = $('dispatchCategory').value === button.dataset.category ? '' : button.dataset.category;
      renderPool(); updateSelection();
    });
    ['dispatchCategory','dispatchWait','dispatchRisk','dispatchKa'].forEach(id => $(id).addEventListener('change', () => {renderPool(); updateSelection();}));
    $('dispatchSearch').addEventListener('input', () => {renderPool(); updateSelection();});
    $('riskRecipient').addEventListener('change', selectionChanged);
    $('assignReason').addEventListener('input', selectionChanged);
    $('selectAllMembersBtn').addEventListener('click', () => {selectedMembers = new Set(members.map(row => row.name)); rememberRoster(); renderMembers(); selectionChanged();});
    $('clearMembersBtn').addEventListener('click', () => {selectedMembers.clear(); rememberRoster(); renderMembers(); selectionChanged();});
    $('selectAllTicketsBtn').addEventListener('click', () => {filteredTickets().filter(row => !row.assignment_blocked).forEach(row => selectedTickets.add(row.ticket_id)); selectionChanged();});
    $('clearTicketsBtn').addEventListener('click', () => {selectedTickets.clear(); selectionChanged();});
    $('refreshPendingBtn').addEventListener('click', () => {invalidate(); loadPool();});
    $('previewAssignBtn').addEventListener('click', () => generatePlan());
    $('saveCookieBtn').addEventListener('click', saveCookie);
    $('testCookieBtn').addEventListener('click', loadStatus);
    $('refreshAuditBtn').addEventListener('click', loadAudit);
    $('verifyAssignBtn').addEventListener('click', verifyResults);
    $('assignmentPreview').addEventListener('change', event => {
      if (!preview || !event.target.dataset.recipient) return;
      overrides[event.target.dataset.recipient] = event.target.value;
      // Preserve every other recipient when regenerating an edited plan.
      preview.assignments.forEach(item => { if (!Object.hasOwn(overrides, item.ticket.ticket_id)) overrides[item.ticket.ticket_id] = item.user.name; });
      preview.dirty = true;
      $('dispatchPlanHint').textContent = '接收人已调整，请先更新方案；更新方案不会执行分派。';
      $('executeAssignBtn').disabled = false; $('executeAssignBtn').textContent = '保存调整并更新方案';
    });
    Promise.allSettled([loadStatus(), loadMembers(), loadPool(), loadAudit()]);
    window.setInterval(() => {
      if (!busy && !verifying) {
        checkDay();
        Promise.allSettled([loadStatus(), loadPool(), loadMembers(), loadAudit()]).then(verifyResults);
      }
    }, 5 * 60 * 1000);
    window.setInterval(() => {
      if (!busy) checkDay();
      if (preview && Date.now() >= expiresAt && !busy) {
        $('dispatchExpiry').textContent = '方案已过期'; $('dispatchPlanHint').textContent = '请重新生成方案后执行。';
        $('executeAssignBtn').disabled = true;
      }
    }, 15000);
  }
  return {init};
})();
