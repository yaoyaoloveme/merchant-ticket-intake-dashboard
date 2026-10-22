#!/usr/bin/env python3
"""Export and optionally publish the public, read-only GitHub Pages dashboard."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import app as dashboard

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / 'docs'
ZONE = ZoneInfo('Asia/Shanghai')
EXPECTED_REMOTE = 'https://github.com/yaoyaoloveme/merchant-ticket-intake-dashboard.git'
WORKFLOW_RUNS_URL = ('https://api.github.com/repos/yaoyaoloveme/'
                     'merchant-ticket-intake-dashboard/actions/workflows/pages.yml/runs?per_page=1')
GITHUB_DIRECT_IPS = ('140.82.112.3', '140.82.113.3', '140.82.114.3', '140.82.116.3')
PRIVATE_SNAPSHOT_MARKERS = ('/complaint-audit', 'complained_agent', 'responsibility_subject', 'complaint_audit_records',
                            '/api/tianrun', 'DashboardDispatch', 'dispatchScript', 'tianrunCookie')
PUBLIC_REPORT_FORBIDDEN_MARKERS = (
    'source_url', 'description', 'handler', 'employee_name', '"employees":[', '"tickets":[',
    'screenshots', 'merchant-capture', '172.25.', '127.0.0.1', '/dispatch', '/complaint-audit',
)
ACTIVE_COMMAND: subprocess.Popen | None = None


def terminate_active_command(_signum=None, _frame=None) -> None:
    """Ensure a daemon stop also stops an in-flight git helper process group."""
    if ACTIVE_COMMAND is not None and ACTIVE_COMMAND.poll() is None:
        try:
            os.killpg(ACTIVE_COMMAND.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if _signum is not None:
        raise SystemExit(128 + _signum)


def run_command(command: list[str], timeout: int) -> str:
    """Run a child in its own process group so a timeout cannot leave helpers behind."""
    global ACTIVE_COMMAND
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    ACTIVE_COMMAND = process
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout) from exc
    finally:
        ACTIVE_COMMAND = None
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, output=stdout)
    return stdout.strip()


def git(*args: str) -> str:
    # This publisher only talks to GitHub. Bypass the machine-wide loopback
    # proxy because intermittent TLS failures there otherwise leave the public
    # dashboard stale even though private-source collection has succeeded.
    network_command = bool(args and args[0] in {'push', 'fetch', 'ls-remote'})
    attempts = len(GITHUB_DIRECT_IPS) if network_command else 1
    for attempt in range(1, attempts + 1):
        command = ['git', '-c', 'http.proxy=', '-c', 'https.proxy=']
        if network_command:
            command.extend(['-c', f'http.curloptResolve=github.com:443:{GITHUB_DIRECT_IPS[attempt - 1]}'])
        command.extend(args)
        try:
            return run_command(command, timeout=35 if network_command else 120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == attempts:
                raise
            delay = attempt * 3
            logging.warning('GitHub %s route %d/%d failed; retrying in %ds', args[0], attempt, attempts, delay)
            time.sleep(delay)
    raise RuntimeError('unreachable')


def public_value(value, key=''):
    """Keep ticket/shop IDs; mask common contact identifiers in free text."""
    if isinstance(value, dict):
        return {k: public_value(v, k) for k, v in value.items() if k != 'source'}
    if isinstance(value, list):
        return [public_value(item, key) for item in value]
    if not isinstance(value, str) or key in {'ticket_id', 'shop_id', 'source_url'}:
        return value
    if key == 'handler':
        return '已分配' if value and '未分配' not in value else '未分配'
    value = re.sub(r'(?<!\d)(1[3-9]\d)(?:[ -]?\d){4}[ -]?(\d{4})(?!\d)', r'\1****\2', value)
    value = re.sub(r'(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)', r'\1********\2', value)
    value = re.sub(r'(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)', '[电话已隐藏]', value)
    value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[邮箱已隐藏]', value)
    value = re.sub(r'((?:微信号|微信|wx|WX|联系人|联系电话|联系方式|姓名)\s*[:：为]\s*)[^，,；;。\s]+', r'\1[已隐藏]', value)
    # Public snapshots contain analysis text, never playable/signed source assets.
    value = re.sub(r'https?://[^\s；，。]+', '[链接已隐藏]', value)
    return value


def write_if_changed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(path)


def write_json(path: Path, value) -> str:
    content = json.dumps(public_value(value), ensure_ascii=False, separators=(',', ':')) + '\n'
    write_if_changed(path, content)
    return hashlib.sha256(content.encode()).hexdigest()


def public_report_summary(payload: dict, report_date: str) -> dict:
    """Select only approved aggregate fields from a private full report."""
    merchant = payload.get('merchant') if isinstance(payload.get('merchant'), dict) else {}
    deep = merchant.get('deep') if isinstance(merchant.get('deep'), dict) else {}
    summary = deep.get('summary') if isinstance(deep.get('summary'), dict) else {}
    completion = deep.get('completion_rate') if isinstance(deep.get('completion_rate'), dict) else {}
    ka = deep.get('ka') if isinstance(deep.get('ka'), dict) else {}
    merchant_app = deep.get('merchant_app') if isinstance(deep.get('merchant_app'), dict) else {}
    employee = payload.get('employee') if isinstance(payload.get('employee'), dict) else {}
    totals = employee.get('totals') if isinstance(employee.get('totals'), dict) else {}
    created_daily = totals.get('created_daily') if isinstance(totals.get('created_daily'), dict) else {}
    completed_daily = totals.get('completed_daily') if isinstance(totals.get('completed_daily'), dict) else {}

    def value(mapping: dict, key: str, default=0):
        selected = mapping.get(key, default)
        return selected if isinstance(selected, (int, float)) and not isinstance(selected, bool) else default

    return {
        'merchant': {
            'total': value(summary, 'total'),
            'tianrun_count': value(summary, 'tianrun_count'),
            'merchant_app_count': value(summary, 'merchant_app_count'),
            'previous': value(summary, 'previous'),
            'delta': value(summary, 'delta'),
            'pct': value(summary, 'pct'),
            'completion': {
                'completed': value(completion, 'completed'),
                'within_24h': value(completion, 'within_24h'),
                'rate_24h': value(completion, 'rate_24h'),
                'not_completed_count': value(completion, 'not_completed_count'),
                'overdue_not_completed_count': value(completion, 'overdue_not_completed_count'),
                'pending_verification': value(completion, 'pending_verification'),
            },
            'ka': {
                'count': value(ka, 'count'),
                'ratio': value(ka, 'ratio'),
            },
            'merchant_app': {
                'count': value(merchant_app, 'count'),
                'pending_count': value(merchant_app, 'current_pending_count', value(merchant_app, 'pending_count')),
                'overdue_24h_count': value(merchant_app, 'overdue_24h_count'),
            },
        },
        'employee': {
            'generated_at': str(employee.get('generated_at') or '')[:32],
            'tickets': value(totals, 'tickets'),
            'employees': value(totals, 'employees'),
            'over48': value(totals, 'over48'),
            'over120': value(totals, 'over120'),
            'created_today': value(created_daily, report_date),
            'completed_today': value(completed_daily, report_date),
        },
    }


def export_public_reports() -> str:
    """Write a compact report archive without copying private report artifacts."""
    reports = []
    for report in dashboard.list_daily_reports():
        completed = dashboard.completed_report(report['date'], report['slot'])
        if completed is None:
            continue
        run_dir, _metadata = completed
        try:
            payload = json.loads((run_dir / 'analysis-data.json').read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            logging.warning('Skipping unreadable public report summary %s', report['id'])
            continue
        reports.append({
            'id': report['id'],
            'date': report['date'],
            'slot': report['slot'],
            'slot_label': report['slot_label'],
            'finished_at': report['finished_at'],
            'status': report['status'],
            'status_label': report['status_label'],
            'summary': public_report_summary(payload, report['date']),
        })
    exported = {
        'schema_version': 1,
        'schedule': ['18:00', '23:59'],
        'reports': reports,
    }
    serialized = json.dumps(exported, ensure_ascii=False, separators=(',', ':'))
    forbidden = next((marker for marker in PUBLIC_REPORT_FORBIDDEN_MARKERS if marker in serialized), None)
    if forbidden:
        raise RuntimeError(f'Public report archive contains private marker {forbidden!r}')
    return write_json(DOCS / 'data' / 'reports' / 'index.json', exported)


def export_snapshot() -> None:
    latest = dashboard.latest_ticket_date()
    conn = dashboard.get_db()
    dates = [row[0] for row in conn.execute(
        '''SELECT created_date FROM (
             SELECT created_date FROM tickets WHERE created_date>=?
             UNION
             SELECT created_date FROM merchant_inspections WHERE created_date>=?
           ) WHERE created_date<>'' ORDER BY created_date DESC''',
        ((date.fromisoformat(latest) - timedelta(days=29)).isoformat(),) * 2)]
    sync = conn.execute("SELECT finished_at FROM sync_runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
    updated_by_day = {}
    for day_string, fetched_at in conn.execute(
        '''SELECT created_date, MAX(fetched_at) FROM (
             SELECT created_date, fetched_at FROM tickets
             UNION ALL
             SELECT created_date, fetched_at FROM merchant_inspections
           ) GROUP BY created_date'''):
        updated_by_day[day_string] = fetched_at or ''
    for day_string, fetched_at in conn.execute(
        '''SELECT t.created_date, MAX(e.fetched_at)
           FROM tickets t JOIN ticket_evidence e ON e.ticket_id=t.ticket_id
           GROUP BY t.created_date'''):
        updated_by_day[day_string] = max(updated_by_day.get(day_string, ''), fetched_at or '')
    conn.close()
    if not dates or not sync:
        raise RuntimeError('No successfully synchronized ticket data to publish')
    source_at = datetime.fromisoformat(sync[0]).replace(tzinfo=ZONE).isoformat(timespec='seconds')
    html = (ROOT / 'templates/index.html').read_text()
    html = html.replace('<body{% if embedded %} class="workspace-embedded"{% endif %}>', '<body class="workspace-embedded">')
    html = re.sub(r'\s*<a id="dispatchPageLink"[^>]*>.*?</a>', '', html, count=1)
    html = re.sub(r'\s*<script id="dispatchScript"[^>]*></script>', '', html)
    html = re.sub(r'\s*<link id="dispatchStyles"[^>]*>', '', html)
    html = html.replace("{{ url_for('static', filename='style.css') }}", 'assets/style.css')
    app_version = hashlib.sha256((ROOT / 'static' / 'app.js').read_bytes()).hexdigest()[:12]
    html = html.replace("{{ url_for('static', filename='app.js') }}", f'assets/app.js?v={app_version}')
    html = html.replace('{{ today }}', latest)
    html = html.replace('<script src=', '<script>window.TICKET_PUBLIC = true;</script>\n  <script src=')
    html = html.replace('从总量下钻到每张工单：商家主张、现场事实、根因、责任与证据缺口',
        '公开版 · 每5分钟采集发布 · 工单详情可直接查看，源系统链接需内网访问')
    html = html.replace('<title>', '<meta name="robots" content="noindex,nofollow">\n  <title>')
    html = re.sub(r'\n    <section id="dispatchPanel".*?</section>\n\n    <section class="metric-grid"',
        '\n    <section class="metric-grid"', html, count=1, flags=re.S)
    write_if_changed(DOCS / 'daily.html', html)
    workspace = (ROOT / 'templates' / 'public_workspace.html').read_text(encoding='utf-8')
    for name in ('public-workspace.js', 'public-workspace.css'):
        version = hashlib.sha256((ROOT / 'static' / name).read_bytes()).hexdigest()[:12]
        workspace = workspace.replace(f'assets/{name}', f'assets/{name}?v={version}')
    write_if_changed(DOCS / 'index.html', workspace)
    write_if_changed(DOCS / '.nojekyll', '')
    for name in ('app.js', 'style.css', 'public-workspace.js', 'public-workspace.css'):
        content = (ROOT / 'static' / name).read_text()
        if name == 'app.js':
            content = re.sub(r'\nasync function adminJson\(.*?\n\}\n\n\$\(\'datePicker\'\)',
                "\n$('datePicker')", content, count=1, flags=re.S)
            content = re.sub(r"\n\} else \{\n  window\.DashboardDispatch\?\.init\(\);\n\}", '\n}', content, count=1)
        write_if_changed(DOCS / 'assets' / name, content)
    hashes = []
    for day_string in dates:
        day = date.fromisoformat(day_string)
        # Stable historical files keep each five-minute push small.
        historical_at = updated_by_day.get(day_string) or sync[0]
        day_source_at = source_at if day >= date.today() - timedelta(days=1) else datetime.fromisoformat(
            historical_at).replace(tzinfo=ZONE).isoformat(timespec='seconds')
        data = dashboard.build_dashboard(day, 30)
        data['last_sync'] = {'finished_at': day_source_at, 'message': '该日工单快照'}
        hashes.append(write_json(DOCS / 'data' / f'{day_string}.json', {
            'source_synced_at': day_source_at,
            'dashboard': data,
            'ledger': dashboard.build_root_cause_ledger(day),
        }))
    latest_day = date.fromisoformat(latest)
    current_week_start = dashboard.natural_week_start(date.today())
    current_week_end = min(current_week_start + timedelta(days=6), max(current_week_start, latest_day))
    current_week_path = DOCS / 'data' / f'completion-week-{current_week_start.isoformat()}.json'
    should_write_current_week = True
    if current_week_path.exists():
        try:
            should_write_current_week = not json.loads(current_week_path.read_text()).get('complete', False)
        except (OSError, ValueError):
            should_write_current_week = True
    if should_write_current_week:
        current_week = dashboard.build_completion_week_snapshot(current_week_start, current_week_end)
        hashes.append(write_json(current_week_path, current_week))
    prior_week_start = current_week_start - timedelta(days=7)
    prior_week_path = DOCS / 'data' / f'completion-week-{prior_week_start.isoformat()}.json'
    if prior_week_path.exists():
        try:
            prior_week = json.loads(prior_week_path.read_text())
        except (OSError, ValueError):
            prior_week = {}
        if not prior_week.get('complete', False):
            finalized = dashboard.build_completion_week_snapshot(prior_week_start, prior_week_start + timedelta(days=6))
            finalized['complete'] = True
            hashes.append(write_json(prior_week_path, finalized))
    week_files = sorted(DOCS.glob('data/completion-week-*.json'))
    completion_weeks = []
    for week_file in week_files:
        week_start = week_file.stem.removeprefix('completion-week-')
        completion_weeks.append(week_start)
        hashes.append(hashlib.sha256(week_file.read_bytes()).hexdigest())
    hashes.append(write_json(DOCS / 'data/completion-records.json', dashboard.build_completion_records(
        latest_day - timedelta(days=6), latest_day)))
    hashes.append(write_json(DOCS / 'data/merchant-ranking.json', dashboard.build_merchant_complaint_ranking()))
    hashes.append(export_public_reports())
    write_json(DOCS / 'data/index.json', {
        'schema_version': 1,
        'latest_date': latest,
        'dates': dates,
        'completion_weeks': completion_weeks,
        'interval_seconds': 300,
        'source_synced_at': source_at,
        'published_at': datetime.now(ZONE).isoformat(timespec='seconds'),
        'revision': hashlib.sha256(''.join(hashes).encode()).hexdigest(),
    })
    for path in DOCS.rglob('*'):
        if path.is_file() and path.suffix.lower() in {'.html', '.js', '.json', '.css'}:
            content = path.read_text(encoding='utf-8')
            marker = next((value for value in PRIVATE_SNAPSHOT_MARKERS if value in content), None)
            if marker:
                raise RuntimeError(f'Public snapshot contains private complaint-audit marker {marker!r}: {path}')
    logging.info('Exported %d dates; source updated %s', len(dates), source_at)


def sync_with_retry(attempts: int = 3) -> None:
    """Retry the collection cycle after short private-network interruptions."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            dashboard.sync_days(days=2)
            return
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = attempt * 5
            logging.warning('Collection attempt %d/%d failed: %s; retrying in %ds', attempt, attempts, exc, delay)
            time.sleep(delay)
    raise RuntimeError(f'Collection failed after {attempts} attempts: {last_error}') from last_error


def deployment_pending() -> bool:
    """Avoid replacing a queued Pages deployment with a newer push."""
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(WORKFLOW_RUNS_URL, timeout=(5, 10))
        response.raise_for_status()
        runs = response.json().get('workflow_runs', [])
        return bool(runs and runs[0].get('status') != 'completed')
    except (requests.RequestException, ValueError) as exc:
        logging.warning('Cannot check Pages deployment queue: %s', exc)
        return False
    finally:
        session.close()


def publish() -> None:
    if git('remote', 'get-url', 'origin') != EXPECTED_REMOTE or git('branch', '--show-current') != 'main':
        raise RuntimeError('Publishing requires the independent intake dashboard origin/main')
    # Only export files can be included by the unattended publisher.
    if git('diff', '--cached', '--name-only'):
        raise RuntimeError('Unrelated staged changes found; refusing automatic commit')
    if deployment_pending():
        logging.warning('Latest Pages deployment is still pending; keeping the newest snapshot locally')
        return
    git('add', '--', 'docs')
    if git('diff', '--cached', '--name-only'):
        git('commit', '-m', 'data: refresh public ticket dashboard snapshot')
    # Retry a previously committed push on the next invocation after network failure.
    try:
        git('push', 'origin', 'HEAD:main')
    except subprocess.CalledProcessError:
        # A launchd interval and a manual recovery can briefly overlap. The
        # repository lock prevents duplicate export work, but GitHub can still
        # report a ref race after another invocation has pushed this exact
        # commit. Treat identical local/remote commits as success.
        git('fetch', 'origin', 'main')
        if git('rev-parse', 'HEAD') != git('rev-parse', 'origin/main'):
            raise
    logging.info('Public snapshot pushed to the independent GitHub repository')


def main() -> None:
    signal.signal(signal.SIGTERM, terminate_active_command)
    signal.signal(signal.SIGINT, terminate_active_command)
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync', action='store_true', help='Fetch today and yesterday from the internal source')
    parser.add_argument('--push', action='store_true', help='Commit docs only and push to the dedicated repository')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.environ['TZ'] = 'Asia/Shanghai'
    time.tzset()
    dashboard.DATA_DIR.mkdir(exist_ok=True)
    with (dashboard.DATA_DIR / 'pages-publish.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info('A publication is already running; skipping duplicate invocation')
            return
        if args.sync:
            sync_with_retry()
            # Refresh current evidence, including changed outcomes on existing tickets.
            for day in (date.today() - timedelta(days=1), date.today()):
                dashboard.sync_evidence_for_day(day, force=True)
        export_snapshot()
        if args.push:
            publish()


if __name__ == '__main__':
    main()
