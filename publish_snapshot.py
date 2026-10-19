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
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import app as dashboard

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / 'docs'
ZONE = ZoneInfo('Asia/Shanghai')
EXPECTED_REMOTE = 'https://github.com/yaoyaoloveme/merchant-ticket-intake-dashboard.git'


def git(*args: str) -> str:
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, timeout=120).strip()


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


def export_snapshot() -> None:
    latest = dashboard.latest_ticket_date()
    conn = dashboard.get_db()
    dates = [row[0] for row in conn.execute(
        'SELECT DISTINCT created_date FROM tickets WHERE created_date>=? ORDER BY created_date DESC',
        ((date.fromisoformat(latest) - timedelta(days=29)).isoformat(),))]
    sync = conn.execute("SELECT finished_at FROM sync_runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
    updated_by_day = {row[0]: max(row[1] or '', row[2] or '') for row in conn.execute(
        '''SELECT t.created_date, MAX(t.fetched_at), MAX(e.fetched_at)
           FROM tickets t LEFT JOIN ticket_evidence e ON e.ticket_id=t.ticket_id
           GROUP BY t.created_date''')}
    conn.close()
    if not dates or not sync:
        raise RuntimeError('No successfully synchronized ticket data to publish')
    source_at = datetime.fromisoformat(sync[0]).replace(tzinfo=ZONE).isoformat(timespec='seconds')
    html = (ROOT / 'templates/index.html').read_text()
    html = html.replace("{{ url_for('static', filename='style.css') }}", 'assets/style.css')
    html = html.replace("{{ url_for('static', filename='app.js') }}", 'assets/app.js')
    html = html.replace('{{ today }}', latest)
    html = html.replace('<script src=', '<script>window.TICKET_PUBLIC = true;</script>\n  <script src=')
    html = html.replace('从总量下钻到每张工单：商家主张、现场事实、根因、责任与证据缺口',
        '公开版 · 每5分钟采集发布 · 工单详情可直接查看，源系统链接需内网访问')
    html = html.replace('<title>', '<meta name="robots" content="noindex,nofollow">\n  <title>')
    write_if_changed(DOCS / 'index.html', html)
    write_if_changed(DOCS / '.nojekyll', '')
    for name in ('app.js', 'style.css'):
        write_if_changed(DOCS / 'assets' / name, (ROOT / 'static' / name).read_text())
    hashes = []
    for day_string in dates:
        day = date.fromisoformat(day_string)
        # Stable historical files keep each five-minute push small.
        day_source_at = source_at if day >= date.today() - timedelta(days=1) else datetime.fromisoformat(
            updated_by_day[day_string]).replace(tzinfo=ZONE).isoformat(timespec='seconds')
        data = dashboard.build_dashboard(day, 30)
        data['last_sync'] = {'finished_at': day_source_at, 'message': '该日工单快照'}
        hashes.append(write_json(DOCS / 'data' / f'{day_string}.json', {
            'source_synced_at': day_source_at,
            'dashboard': data,
            'ledger': dashboard.build_root_cause_ledger(day),
        }))
    write_json(DOCS / 'data/index.json', {
        'schema_version': 1,
        'latest_date': latest,
        'dates': dates,
        'interval_seconds': 300,
        'source_synced_at': source_at,
        'published_at': datetime.now(ZONE).isoformat(timespec='seconds'),
        'revision': hashlib.sha256(''.join(hashes).encode()).hexdigest(),
    })
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


def publish() -> None:
    if git('remote', 'get-url', 'origin') != EXPECTED_REMOTE or git('branch', '--show-current') != 'main':
        raise RuntimeError('Publishing requires the independent intake dashboard origin/main')
    # Only export files can be included by the unattended publisher.
    if git('diff', '--cached', '--name-only'):
        raise RuntimeError('Unrelated staged changes found; refusing automatic commit')
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
