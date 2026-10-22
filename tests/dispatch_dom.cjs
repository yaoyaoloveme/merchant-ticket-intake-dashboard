// DOM-only workflow tests. Every request is mocked; no browser or production writes.
// Run: NODE_PATH=<directory containing jsdom> node --test tests/dispatch_dom.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'templates/dispatch.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'static/dispatch.js'), 'utf8');
const flush = async () => {for (let i=0;i<15;i++) await new Promise(resolve => setTimeout(resolve,0));};

async function setup({remembered = false} = {}) {
  const dom = new JSDOM(html, {url:'http://127.0.0.1:8090/', runScripts:'outside-only'});
  const w = dom.window, d = w.document;
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.confirm = () => {throw Error('A second confirmation is forbidden');};
  const intervals = [];
  w.setInterval = (callback, ms) => {intervals.push({callback,ms}); return intervals.length;};
  w.clearInterval = () => {};
  const roster = ['楚家浩','曹彩月'];
  const members = roster.map(name => ({name,assigned_today:0,verified_today:0,pending_today:0,category_counts:{'多付/漏付':0,'介入问题':0}}));
  let tickets = [
    {ticket_id:'100001',shop_id:'2001',brand:'KA测试',is_ka:true,category:'多付/漏付',category_raw:'多付/漏付问题核实',description:'顾客要砸店 <img src=x onerror=alert(1)>',demand:'核实支付',risk_level:'紧急',risk_hits:[{label:'人身安全/暴力威胁',level:'紧急',matched_text:'砸店'}],duration_minutes:180,duration_text:'3小时',created_at:'2026-10-20 10:00:00',source_url:'http://example.invalid/1',assignment_blocked:''},
    {ticket_id:'100002',shop_id:'2002',brand:'',is_ka:false,category:'介入问题',category_raw:'介入问题',description:'请核实提醒情况 稽查单888',demand:'核实介入',risk_level:'',risk_hits:[],duration_minutes:30,duration_text:'30分钟',created_at:'2026-10-20 10:00:00',source_url:'http://example.invalid/2',assignment_blocked:''},
  ];
  let audit = [], token = 0;
  const calls = [];
  if (remembered) {
    const date = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date());
    w.localStorage.setItem('ticket-dispatch-roster', JSON.stringify({date,names:['曹彩月']}));
  }
  w.fetch = async (url, options={}) => {
    const route = url.replace('/api/tianrun/',''), body = options.body ? JSON.parse(options.body) : {};
    calls.push({route,body});
    let data;
    if (route.startsWith('status')) data = {connection:'ok',assign_enabled:true};
    else if (route === 'team') data = members;
    else if (route.startsWith('pending-tickets')) data = {tickets,warning:null,count:tickets.length,read_at:'2026-10-20T10:00:00',refreshed_at:'2026-10-20T10:00:00'};
    else if (route === 'assignment/audit') data = audit;
    else if (route === 'assignment/batch-preview') {
      const assignments = body.ticket_ids.map((id,index) => ({ticket:tickets.find(row => row.ticket_id === id),user:{name:body.overrides[id] || body.members[index%body.members.length],cno:'05435'}}));
      data = {confirm_token:`token-${++token}`,expires_in:300,assign_enabled:true,members:body.members,assignments,distribution:body.members.map(name => ({name,before:0,count:assignments.filter(row => row.user.name===name).length,after:assignments.filter(row => row.user.name===name).length}))};
    } else if (route === 'assignment/batch-execute') {
      const results = [{audit_id:1,ticket_id:'100001',category:'多付/漏付',recipient:'曹彩月',state:'已提交待核验',message:'已受理'}, {audit_id:2,ticket_id:'100002',category:'介入问题',recipient:'曹彩月',state:'失败',message:'明确失败'}];
      audit = results.map(row => ({id:row.audit_id,ticket_id:row.ticket_id,category:row.category,target_name:row.recipient,requested_at:'2026-10-20T10:00:00',state:row.state,message:row.message,old_handler:'值守客服问题处理组'}));
      tickets[0].assignment_blocked = '已提交待核验';
      data = {results};
    } else if (route === 'assignment/verify') data = {verified:[],remaining:1,warnings:[],source:'源工单库处理人，可能存在同步延迟'};
    else throw Error(`Unexpected mocked route: ${route}`);
    return {ok:true,status:200,json:async () => ({ok:true,data:structuredClone(data)})};
  };
  w.eval(script); w.DashboardDispatch.init(); await flush();
  const click = selector => {const el=d.querySelector(selector);assert.ok(el, selector);el.click();};
  const change = (selector,value) => {const el=d.querySelector(selector);assert.ok(el,selector);el.value=value;el.dispatchEvent(new w.Event('change',{bubbles:true}));};
  return {dom,w,d,calls,intervals,click,change};
}

test('filters retain hidden selections and show risk evidence without rendering injected HTML', async () => {
  const {dom,d,click,change} = await setup();
  assert.equal(d.querySelector('#previewAssignBtn').disabled,true);
  assert.equal(d.querySelector('#connectionSettings').open,false);
  click('[data-member="楚家浩"]'); click('[data-ticket="100001"]');
  assert.equal(d.querySelector('#previewAssignBtn').disabled,false);
  assert.equal(d.querySelector('#pendingTicketList img'),null);
  assert.match(d.querySelector('.risk-evidence').textContent,/原文“砸店”/);
  change('#dispatchCategory','介入问题');
  assert.equal(d.querySelectorAll('[data-ticket]').length,1);
  assert.match(d.querySelector('#dispatchSelectionHint').textContent,/1 张筛选外已选工单/);
  click('#selectAllTicketsBtn');
  assert.match(d.querySelector('#selectionSummary').textContent,/已选 2 单/);
  change('#dispatchCategory','');
  assert.equal(d.querySelectorAll('[data-ticket]:checked').length,2);
  dom.window.close();
});

test('manual plan edits regenerate the server token, execute once, and retry only failures', async () => {
  const {dom,d,calls,click,change} = await setup();
  click('#selectAllMembersBtn'); click('#selectAllTicketsBtn'); click('#previewAssignBtn'); await flush();
  assert.ok(d.querySelector('#executeAssignBtn'));
  change('[data-recipient="100001"]','曹彩月');
  assert.match(d.querySelector('#executeAssignBtn').textContent,/更新方案/);
  click('#executeAssignBtn'); await flush();
  assert.equal(calls.filter(row => row.route==='assignment/batch-execute').length,0);
  assert.equal(calls.filter(row => row.route==='assignment/batch-preview').length,2);
  assert.equal(calls.filter(row => row.route==='assignment/batch-preview')[1].body.previous_token,'token-1');
  assert.equal(d.querySelector('[data-recipient="100001"]').value,'曹彩月');
  click('#executeAssignBtn'); await flush();
  assert.equal(calls.filter(row => row.route==='assignment/batch-execute').length,1);
  assert.equal(calls.find(row => row.route==='assignment/batch-execute').body.confirm_token,'token-2');
  assert.match(d.querySelector('#assignmentPreview').textContent,/已提交待核验 1/);
  assert.match(d.querySelector('#assignmentPreview').textContent,/失败 1/);
  assert.equal(d.querySelector('[data-ticket="100001"]').disabled,true);
  click('#retryFailedBtn'); await flush();
  const retry = calls.filter(row => row.route==='assignment/batch-preview').at(-1);
  assert.deepEqual(retry.body.ticket_ids,['100002']);
  assert.equal(calls.filter(row => row.route==='assignment/batch-execute').length,1);
  dom.window.close();
});

test('today roster is restored and periodic refresh keeps the selected staff', async () => {
  const {dom,d,intervals} = await setup({remembered:true});
  assert.equal(d.querySelector('[data-member="曹彩月"]').checked,true);
  assert.equal(d.querySelector('[data-member="楚家浩"]').checked,false);
  intervals.find(row => row.ms===300000).callback(); await flush();
  assert.equal(d.querySelector('[data-member="曹彩月"]').checked,true);
  dom.window.close();
});
