#!/usr/bin/env python3
"""每日新增工单分析看板。

从现有 Forge 同步看板的只读列表页面采集工单，保存到本地 SQLite，
并提供趋势、类型增量、异常记录、问题主题和 KA 占比分析。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request, send_file
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from complaint_audit import register_complaint_audit, start_auto_sync as start_complaint_audit_sync


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "tickets.db"
TIANRUN_COOKIE_PATH = DATA_DIR / "tianrun_cookie.txt"
DEFAULT_SOURCE = "http://172.25.11.31:8080/ticket-dashboard/"
SOURCE_BASE = os.environ.get("TICKET_DASHBOARD_URL", DEFAULT_SOURCE).rstrip("/") + "/"
TIANRUN_BASE = "https://ticket-sh.clink.cn"
TIANRUN_ASSIGN_ENABLED = os.environ.get("TIANRUN_ASSIGN_ENABLED", "").lower() in {"1", "true", "yes"}
EMPLOYEE_DASHBOARD_HTML = Path(
    os.environ.get("EMPLOYEE_DASHBOARD_OUTPUT", ROOT.parent / "员工工单卡点看板.html")
)
EMPLOYEE_DASHBOARD_DATA = Path(
    os.environ.get("EMPLOYEE_DASHBOARD_DATA_OUTPUT", ROOT.parent / "员工工单卡点看板数据.json")
)
EMPLOYEE_DASHBOARD_SERVICE = os.environ.get(
    "EMPLOYEE_DASHBOARD_SERVICE", "http://127.0.0.1:8765"
).rstrip("/")
REPORT_ARCHIVE_ROOT = Path(
    os.environ.get("DASHBOARD_DAILY_REPORT_ROOT", ROOT.parent / "outputs/dashboard-daily-analysis")
)
REPORT_SLOTS = {"1800": "18:00版", "2359": "23:59版"}
REPORT_DOWNLOAD_FILES = {"analysis.md", "analysis-data.json", "run-metadata.json"}

app = Flask(__name__)
register_complaint_audit(app)
SYNC_LOCK = threading.Lock()
SYNC_STATE: dict[str, Any] = {"running": False, "message": "尚未同步", "finished_at": None}
EVIDENCE_LOCK = threading.Lock()
EVIDENCE_STATE: dict[str, Any] = {"running": False, "message": "尚未补齐稽查证据", "finished_at": None}
TIANRUN_LOCK = threading.Lock()
TIANRUN_PREVIEWS: dict[str, dict[str, Any]] = {}
TIANRUN_USER_CACHE: dict[str, dict[str, Any]] = {}
AUTO_SYNC_INTERVAL_SECONDS = 5 * 60
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
MERCHANT_PENDING_STATUSES = {"未稽查", "稽查中", "待稽查"}
AUTO_SYNC_STATE: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": AUTO_SYNC_INTERVAL_SECONDS,
    "last_started_at": None,
    "last_finished_at": None,
    "next_run_at": None,
    "last_error": None,
}


CATEGORY_ORDER = ["多付/漏付", "介入问题", "批量核实", "回访工单", "其他"]
CORE_CATEGORY_ORDER = ["多付/漏付", "批量核实", "回访工单", "介入问题"]
GUARD_QUEUE_HANDLER = "值守客服问题处理组"
PENDING_ASSIGN_STATUSES = {"待领取", "待接单"}
TEAM_MEMBERS = [
    "楚家浩", "曹彩月", "曹静宇", "陈小如", "龚臣", "谷玉", "黄琳莉01", "黄晓01",
    "黄雨晴", "李东颖", "刘可婷", "马婧瑶", "谭森林", "张琳01", "周丽君", "周雪鑫",
]
TOPIC_RULES = [
    ("少付/漏付", r"少付|漏付|未付款|没有付款|未支付|少扫|漏扫"),
    ("偷盗/疑似拿取", r"偷盗|偷拿|盗窃|拿走|顺走|疑似.{0,8}(拿|取)|未结账"),
    ("多付/重复支付", r"多付|重复支付|重复扣|多扫|重复扫码"),
    ("未介入/响应不及时", r"介入|未提醒|未通知|响应不及时|客服.{0,8}(没|未|直接)"),
    ("监控/回放/视频", r"监控|回放|视频|录像|机位|导出"),
    ("退款/追讨/赔付", r"退款|追讨|赔付|垫付|追回|收款|支付失败|付款失败"),
    ("稽查/定损/资料补充", r"稽查单|定损|填写|补数|补材料|待补充"),
    ("网络/设备/系统", r"网络|断网|设备|系统|APP|小程序|二维码|扫码不开|无法扫码"),
    ("商品损坏/破损", r"损坏|破损|摔破|打碎|变形|掉落|洒了"),
    ("终止/取消", r"终止|取消|撤销|关闭订单"),
    ("门锁/门禁/断电", r"门锁|门禁|停电|断电|门磁|锁不起"),
    ("商品补充/条码/价格", r"补充商品|商品信息|条码|价格|货架位置|摆放位置"),
    ("顾客身份/联系方式", r"手机号|电话|身份|实名|未成年|人脸"),
]
RISK_RULES = [
    {
        "label": "人身安全/暴力威胁",
        "level": "紧急",
        "patterns": [
            r"砸店|砸门|打砸|打人|殴打|持刀|拿刀|挥刀|刀具威胁",
            r"人身安全|人身威胁|生命安全|杀人|捅人|放火|纵火|报复商家|威胁店主|威胁店员",
        ],
    },
    {
        "label": "极端/不文明行为",
        "level": "紧急",
        "patterns": [r"撒尿|随地小便|小便在|尿在|随地便溺|大小便|排泄物"],
    },
    {
        "label": "情绪或冲突升级",
        "level": "高",
        "patterns": [
            r"情绪激动|情绪失控|大吵大闹|辱骂|谩骂|恐吓|闹事|肢体冲突",
            r"威胁.{0,8}(商家|店主|店员|客服)|(?:商家|店主|店员|客服).{0,8}受到威胁",
        ],
    },
    {
        "label": "舆情曝光",
        "level": "高",
        "patterns": [r"要曝光|进行曝光|发到网上|发网上|发布到网上|发到抖音|抖音曝光|媒体曝光|联系媒体|自媒体|上热搜|舆情"],
    },
    {
        "label": "监管举报",
        "level": "高",
        "patterns": [r"要举报|已举报|向.{0,8}(?:举报|12315|市场监督|市场监管|市监局|工商局|消协|消费者协会|烟草局)|举报.{0,8}(?:平台|商家|问题)|投诉监管"],
    },
    {
        "label": "报警/治安升级",
        "level": "高",
        "patterns": [r"已报警|拨打\s*110|联系\s*110|要报警|派出所|公安机关"],
    },
    {
        "label": "未成年人合规",
        "level": "高",
        "patterns": [
            r"未成年|未满\s*(?:十八|18|十六|16)\s*岁?",
            r"(?:小孩|儿童|学生).{0,8}(?:买烟|购烟|香烟|卷烟|酒)|(?:买烟|购烟|香烟|卷烟|酒).{0,8}(?:小孩|儿童|学生)",
        ],
    },
    {
        "label": "食品安全",
        "level": "高",
        "patterns": [r"过期商品|食品过期|变质|发霉|食品安全|食物中毒|吃坏|保质期异常|商品异物"],
    },
    {
        "label": "现金/重大财产风险",
        "level": "高",
        "patterns": [
            r"现金被盗|现金丢失|偷现金|盗取现金",
            r"收银台.{0,8}(?:现金|钱).{0,8}(?:丢失|被偷|被盗)|保险柜.{0,8}(?:撬|被盗|丢失)",
        ],
    },
    {
        "label": "连续/重复盗窃",
        "level": "高",
        "patterns": [r"连续偷盗|多次偷盗|批量偷盗|惯偷|反复偷盗|两次偷盗|多次盗窃"],
    },
    {
        "label": "隐私/信息安全",
        "level": "高",
        "patterns": [r"泄露隐私|隐私泄露|个人信息泄露|监控泄露|偷拍视频|偷拍视频"],
    },
]
RISK_LEVEL_ORDER = {"紧急": 0, "高": 1, "关注": 2}
MERCHANT_DEMAND_RULES = {
    "多付/漏付": [
        ("纠正错误追讨/解除限制", r"追(?:讨|缴)错|错人|终止|取消|解除拉黑|解除黑名单|无法进店|拉黑"),
        ("追讨、定损或资料补充", r"追讨|追缴|追回|定损|垫付|赔付|待支付|补付|进度|跟进处理|补充|填写|驳回|具体数量|重新提供商品数量|条码|无条码|无码|辛苦跟进|尽快稽查|尽快核实|一直在核实|商家催促"),
        ("复核稽查结果", r"不认可|重新核实|再次核实|重新确认|复核|监控核实|明确结果|结果不满|值守未发现异常|没看到异常|监控没看到异常"),
        ("调取视频或顾客全量记录", r"调取|导出|视频|回放|监控(?:同步|查看|延长|看不了|无法查看)|进出记录|手机号|联系方式|所有订单|所有稽查单|统计.{0,30}(?:物品|商品)|都偷了什么"),
        ("订单、顾客或商品归属核实", r"上一单顾客|上一个顾客|图中订单|订单.{0,12}(?:顾客|商品).{0,8}(?:不对|归属|拿的)|拿烟是"),
        ("多付、退款、差价或到账处理", r"多付|重复支付|扣了两次|退多|退款|退\s*\d+(?:\.\d+)?\s*元|差价|退客人|加的钱|多扫|未收到|没有收到|没收到|么有收到|金额.{0,8}(?:未|没|么有)收到|到账|收款码|款项|金额不符|标价.{0,8}元"),
        ("漏付、少付或拿多付少核实", r"漏付|露付|少付|少漏|漏扫|没有扫码|没扫码|只扫|仅扫|未付款|没付款|没付钱|没有付款|没给钱|未支付|未结账|付款未成功|没有付款成功|只付|只有付|仅支付|少结算|少算|拿走|偷窃|偷盗|偷拿|被盗|丢失|少了|少\s*\d+包|商品中少|少一|拿错|一瓶一瓶扫码|个人收款码|多拿|数量.{0,8}核实|是否少扫"),
        ("值守介入或网络服务核实", r"值守问题|值守.{0,8}介入|客服.{0,8}(?:叫|未|没)|没网络|无网络|网络异常"),
        ("商品或现场异常核实", r"摔破|砸破|弄破|损坏|破损|乱桶|猫偷吃|未购买成功|没有购买成功|购买失败|订单异常|存在异常|异常订单|购物情况|购物付款情况|留袋子|包装袋|异常情况|冰箱.{0,12}(?:开启|未关|持续)|冷饮柜门没关|门磁|顾客被困|未取走|订单货物已取走|显示关闭"),
    ],
    "批量核实": [
        ("同一顾客全量订单回溯", r"所有订单|所有稽查单|所有进店|批量回溯|全部订单|历史全部|所有异常|其他异常|这个顾客.{0,18}(?:其他|所有|全部)|该顾客.{0,18}(?:其他|所有|全部)|顾客.{0,12}(?:其他|所有|全部).{0,8}异常|买了\s*\d+次"),
        ("视频、回放或进出记录导出", r"导出|发送.{0,8}(?:视频|回放)|调取.{0,8}(?:视频|监控)|稽查单回放|监控视频|发送监控|异常视频|监控录像"),
        ("长周期盘点差异或商品丢失", r"盘点|丢失|丢了|丢东西|少一|少了|缺少|被盗|偷盗|偷拿|盗走|少两|少三|商品有丢|差一包|少付|漏付|露付|其他异常"),
        ("指定时间段值守情况核查", r"值守期间|无人值守|时间段|昨天到|昨晚|昨日|期间|几点|什么时候|凌晨|早上|上午|下午|晚上|半夜|\d{1,2}[:：]\d{2}"),
        ("顾客身份、手机号或黑名单处理", r"手机号|联系方式|解除拉黑|黑名单|放出"),
        ("门店设备或现场异常追溯", r"冰箱|冰柜|门磁|玻璃门|门锁|门是开着|现金支付|瓜子皮|设备|损坏|进门的两人|是哪个顾客|那个顾客|被捡走|又放回"),
    ],
    "回访工单": [
        ("补充发生时间或核实范围", r"未提供时间|没有时间|提供时间|确认时间|询问时间|在询问.{0,16}时间|时间范围|准确时间|具体时间|等待.{0,8}时间|是否.{0,8}期间|还是什么时候"),
        ("补充商品位置或盘点信息", r"摆放位置|货架位置|商品位置|盘点时间|上次盘点|盘点结果|待商家提供商品|待提供差价|差价以及订单"),
        ("稽查结果异议或重新核实", r"不认可|重新确认|重新核实|再次核实|质疑稽查|结果"),
        ("值守未响应或未介入回访", r"呼叫客服|无人响应|未响应|没有反应|未介入|客服下线|未主动|值守人员未"),
        ("漏付、丢失或偷盗问题回访", r"漏付|少付|未付款|丢失|少一|少了|偷拿|偷盗|空盒|未下单"),
        ("设备、商品或现场异常回访", r"离线|设备|冰柜|冰箱|过期|猫|掉落|损坏|变形|未关紧"),
    ],
    "介入问题": [
        ("客服未介入、未提醒或未通知", r"未介入|无介入|没有介入|没介入|无客服|全程客服不在|未提醒|没有提醒|未通知|没有通知|未阻止|没有阻止|未及时通知|没打电话提醒|介入不及时|没报警"),
        ("客服响应慢或无人响应", r"未响应|无人响应|长时间未响应|没有反应|呼叫客服|响应不及时"),
        ("门禁、开关门或冰柜现场处置", r"门禁|门锁|开门|关门|没关门|冰箱门|冰柜|门被|强制锁门"),
        ("错误引导、判断或服务态度", r"错误告知|错误引导|误导|服务态度|判断错误|只有充电头|强制锁门"),
        ("支付、现金或特殊商品介入", r"现金支付|不能用现金|付款|支付|兑奖|退烧药|左炔诺孕酮|药品|追溯码|填写表格|特殊需求"),
        ("要求回电、解释、举证、视频或追讨", r"致电|回复|联系顾客|返还|给交代|处罚|证据|投诉|专员|需要这个视频|追讨|维修|承担.{0,8}责任|承担.{0,8}费用"),
        ("现场行为或系统显示异常核实", r"系统显示|无人进店|带狗进店|猫进店|撕了|记账本|进店情况是否异常|当时的情况|顾客进店情况"),
        ("核实一般值守介入情况", r"介入情况|介入.{0,6}情况|值守.{0,6}介入|值守情况|值守问题|核实介入|介入问题|核实.{0,10}值守"),
    ],
}


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tickets (
          ticket_id TEXT PRIMARY KEY,
          shop_id TEXT,
          brand TEXT,
          category_raw TEXT,
          category TEXT,
          description TEXT,
          status TEXT,
          inspect_status TEXT,
          tags TEXT,
          handler TEXT,
          created_at TEXT,
          created_date TEXT,
          duration_text TEXT,
          duration_minutes INTEGER,
          inspect_completed_at TEXT,
          response_text TEXT,
          response_minutes INTEGER,
          pipeline_text TEXT,
          is_ka INTEGER NOT NULL DEFAULT 0,
          fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_created_date ON tickets(created_date);
        CREATE INDEX IF NOT EXISTS idx_tickets_category ON tickets(category);
        CREATE INDEX IF NOT EXISTS idx_tickets_brand ON tickets(brand);
        CREATE TABLE IF NOT EXISTS merchant_inspections (
          event_id TEXT PRIMARY KEY,
          inspection_id TEXT,
          shop_id TEXT,
          brand TEXT,
          description TEXT,
          inspect_status TEXT,
          abnormal_state TEXT,
          abnormal_type TEXT,
          created_at TEXT,
          created_date TEXT,
          completed_at TEXT,
          duration_text TEXT,
          duration_minutes INTEGER,
          pipeline_text TEXT,
          is_ka INTEGER NOT NULL DEFAULT 0,
          is_open INTEGER NOT NULL DEFAULT 0,
          fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_merchant_created_date ON merchant_inspections(created_date);
        CREATE INDEX IF NOT EXISTS idx_merchant_open ON merchant_inspections(is_open, inspect_status);
        CREATE TABLE IF NOT EXISTS merchant_sync_state (
          state_key TEXT PRIMARY KEY,
          state_value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          date_from TEXT,
          date_to TEXT,
          ticket_count INTEGER DEFAULT 0,
          merchant_count INTEGER DEFAULT 0,
          status TEXT NOT NULL,
          message TEXT
        );
        CREATE TABLE IF NOT EXISTS ticket_evidence (
          ticket_id TEXT PRIMARY KEY,
          check_count INTEGER NOT NULL DEFAULT 0,
          abnormal_state TEXT,
          abnormal_type TEXT,
          check_result TEXT,
          check_meta TEXT,
          loss_summary TEXT,
          detail_status TEXT NOT NULL,
          fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assignment_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ticket_id TEXT NOT NULL,
          old_handler TEXT,
          target_name TEXT NOT NULL,
          target_cno TEXT,
          target_handler_id TEXT NOT NULL,
          content TEXT,
          requested_at TEXT NOT NULL,
          state TEXT NOT NULL,
          response_status INTEGER,
          message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_assignment_audit_ticket ON assignment_audit(ticket_id, requested_at);
        """
    )
    sync_columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_runs)")}
    if "merchant_count" not in sync_columns:
        conn.execute("ALTER TABLE sync_runs ADD COLUMN merchant_count INTEGER DEFAULT 0")
    return conn


def latest_ticket_date() -> str:
    """首页默认展示本地已有数据的最新日期，避免跨日后尚未同步时打开即空白。"""
    conn = get_db()
    row = conn.execute(
        "SELECT MAX(created_date) AS latest_date FROM ("
        "SELECT created_date FROM tickets UNION ALL SELECT created_date FROM merchant_inspections)"
    ).fetchone()
    conn.close()
    return row["latest_date"] if row and row["latest_date"] else date.today().isoformat()


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def local_admin_only() -> tuple[Any, int] | None:
    """The write console is deliberately unavailable to remote clients."""
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return jsonify({"ok": False, "error": "天润管理功能仅允许在看板所在电脑本机使用"}), 403
    origin = request.headers.get("Origin", "")
    allowed_origins = {f"http://{request.host}", f"https://{request.host}"}
    if request.method != "GET" and origin and origin not in allowed_origins:
        return jsonify({"ok": False, "error": "拒绝来自其他网页的管理请求"}), 403
    return None


def read_tianrun_cookie() -> str:
    if not TIANRUN_COOKIE_PATH.exists():
        return ""
    return TIANRUN_COOKIE_PATH.read_text(encoding="utf-8").strip()


def cookie_summary() -> dict[str, Any]:
    cookie = read_tianrun_cookie()
    names = []
    for part in cookie.split(";"):
        name = part.strip().partition("=")[0].strip()
        if name:
            names.append(name)
    stat = TIANRUN_COOKIE_PATH.stat() if cookie and TIANRUN_COOKIE_PATH.exists() else None
    return {
        "configured": bool(cookie),
        "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds") if stat else None,
        "cookie_names": names,
        "has_tsession": "TSESSIONID" in names,
        "assign_enabled": TIANRUN_ASSIGN_ENABLED,
    }


def save_tianrun_cookie(value: str) -> None:
    cookie = value.strip()
    if not cookie or "TSESSIONID=" not in cookie:
        raise ValueError("Cookie 中没有找到 TSESSIONID")
    if "\n" in cookie or "\r" in cookie or len(cookie) > 20000:
        raise ValueError("Cookie 格式异常")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = TIANRUN_COOKIE_PATH.with_suffix(".tmp")
    temporary.write_text(cookie, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(TIANRUN_COOKIE_PATH)
    os.chmod(TIANRUN_COOKIE_PATH, 0o600)


def tianrun_session() -> requests.Session:
    cookie = read_tianrun_cookie()
    if not cookie:
        raise RuntimeError("尚未配置天润 Cookie")
    session = requests.Session()
    # Tianrun is directly reachable; bypass a flaky desktop proxy that can
    # intermittently close authenticated POST requests.
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Cookie": cookie,
            "Origin": TIANRUN_BASE,
            "Referer": TIANRUN_BASE + "/",
            "User-Agent": "Mozilla/5.0 ticket-dashboard-local-admin",
        }
    )
    return session


class TianrunUncertainError(RuntimeError):
    """A write may have reached Tianrun; never automatically resubmit it."""


def tianrun_json(method: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    writing = method.upper() == "PUT"
    try:
        response = tianrun_session().request(
            method, TIANRUN_BASE + path, json=payload, timeout=(8, 25), allow_redirects=False
        )
    except requests.RequestException as exc:
        if writing:
            raise TianrunUncertainError("提交连接中断，结果待确认；请核验台账，勿重复提交") from exc
        raise RuntimeError(f"无法连接天润：{exc}") from exc
    if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
        raise PermissionError("天润登录已失效，请重新登录后更新 Cookie")
    try:
        data = response.json()
    except ValueError as exc:
        if writing:
            raise TianrunUncertainError("提交返回内容异常，结果待确认；请核验台账") from exc
        raise RuntimeError(f"天润返回了非 JSON 内容（HTTP {response.status_code}）") from exc
    if writing and (response.status_code >= 500 or not isinstance(data, dict)):
        raise TianrunUncertainError("天润提交返回异常，结果待确认；请核验台账")
    if response.status_code >= 400:
        message = clean(str(data.get("message") or data.get("msg") or "请求失败"))
        raise RuntimeError(f"天润请求失败（HTTP {response.status_code}）：{message}")
    business_status = data.get("status")
    if writing and isinstance(business_status, int) and business_status >= 500:
        raise TianrunUncertainError("天润处理异常，提交结果待确认；请核验台账")
    if business_status in {401, 403}:
        raise PermissionError("天润登录已失效或当前账号无权限，请重新登录后更新 Cookie")
    if isinstance(business_status, int) and business_status >= 400:
        message = clean(str(data.get("message") or data.get("msg") or "请求失败"))
        raise RuntimeError(f"天润请求失败（状态 {business_status}）：{message}")
    if writing and data.get("status") != 200 and "已是该处理人" not in str(data.get("message", "")):
        raise TianrunUncertainError("未收到明确受理状态，结果待确认；请核验台账")
    return response.status_code, data


def tianrun_users(search_name: str) -> list[dict[str, str]]:
    _, data = tianrun_json(
        "POST",
        "/api/user/page?noRepeat=true",
        {"limit": 30, "offset": 0, "searchName": search_name, "usePermission": 1, "isDisplaySchedule": 1},
    )
    rows = data.get("result", {}).get("data", [])
    if not isinstance(rows, list):
        raise RuntimeError("天润员工查询返回结构与预期不一致")
    return [
        {
            "handler_id": str(row.get("id") or ""),
            "name": clean(str(row.get("name") or "")),
            "cno": clean(str(row.get("cno") or row.get("employeeId") or "")),
        }
        for row in rows
        if row.get("id") and row.get("name")
    ]


def exact_tianrun_user(search_name: str) -> dict[str, str]:
    query = clean(search_name)
    cached = TIANRUN_USER_CACHE.get(query)
    if cached and cached["expires_at"] > time.time():
        return dict(cached["user"])
    users = tianrun_users(query)
    exact = [row for row in users if row["name"] == query or row["cno"] == query]
    if len(exact) != 1:
        if not exact:
            raise ValueError("没有找到姓名或工号完全匹配的唯一员工")
        raise ValueError("匹配到多名员工，请改用唯一工号")
    user = exact[0]
    TIANRUN_USER_CACHE[query] = {"user": dict(user), "expires_at": time.time() + 8 * 60 * 60}
    if user["name"]:
        TIANRUN_USER_CACHE[user["name"]] = {"user": dict(user), "expires_at": time.time() + 8 * 60 * 60}
    if user["cno"]:
        TIANRUN_USER_CACHE[user["cno"]] = {"user": dict(user), "expires_at": time.time() + 8 * 60 * 60}
    return user


def assignment_ticket(ticket_id: str) -> dict[str, Any]:
    conn = get_db()
    row = conn.execute(
        "SELECT ticket_id, shop_id, brand, category, description, status, handler, created_at "
        "FROM tickets WHERE ticket_id=?",
        (ticket_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError("本地看板中未找到该工单，请先同步最新数据")
    return dict(row)


def save_assignment_audit(
    ticket: dict[str, Any], user: dict[str, str], reason: str, state: str,
    status: int | None, message: str,
) -> int:
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO assignment_audit (ticket_id,old_handler,target_name,target_cno,target_handler_id,content,requested_at,state,response_status,message) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            ticket["ticket_id"], ticket.get("handler", ""), user.get("name", ""), user.get("cno", ""),
            user.get("handler_id", ""), reason, datetime.now().isoformat(timespec="seconds"),
            state, status, message,
        ),
    )
    conn.commit()
    audit_id = cursor.lastrowid
    conn.close()
    return audit_id


def team_assignment_statistics(statistics_date: str | None = None) -> list[dict[str, Any]]:
    """Summarise the latest non-failed dashboard submission for each ticket today."""
    report_date = statistics_date or date.today().isoformat()
    summary = {
        name: {
            "name": name,
            "assigned_today": 0,
            "verified_today": 0,
            "pending_today": 0,
            "category_counts": {category: 0 for category in CORE_CATEGORY_ORDER},
            "statistics_date": report_date,
        }
        for name in TEAM_MEMBERS
    }
    conn = get_db()
    rows = conn.execute(
        """
        SELECT audit.ticket_id, audit.target_name, audit.state, COALESCE(tickets.category, '其他') AS category
        FROM assignment_audit AS audit
        LEFT JOIN tickets ON tickets.ticket_id = audit.ticket_id
        WHERE substr(audit.requested_at, 1, 10) = ?
          AND audit.state IN ('已提交待核验','幂等完成','已核验')
          AND audit.id = (
            SELECT latest.id
            FROM assignment_audit AS latest
            WHERE latest.ticket_id = audit.ticket_id
              AND substr(latest.requested_at, 1, 10) = ?
              AND latest.state IN ('已提交待核验','幂等完成','已核验')
            ORDER BY latest.requested_at DESC, latest.id DESC
            LIMIT 1
          )
        """,
        (report_date, report_date),
    ).fetchall()
    conn.close()
    for row in rows:
        member = summary.get(row["target_name"])
        if not member:
            continue
        member["assigned_today"] += 1
        member["verified_today" if row["state"] in {"已核验", "幂等完成"} else "pending_today"] += 1
        if row["category"] in member["category_counts"]:
            member["category_counts"][row["category"]] += 1
    return [summary[name] for name in TEAM_MEMBERS]


ASSIGNMENT_UNRESOLVED = ("正在提交", "已提交待核验", "提交结果待确认")


def unresolved_assignments() -> dict[str, dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT a.* FROM assignment_audit a WHERE a.id=(SELECT MAX(b.id) FROM assignment_audit b "
        "WHERE b.ticket_id=a.ticket_id AND b.state NOT IN ('失败','未提交')) AND a.state IN (?,?,?)", ASSIGNMENT_UNRESOLVED,
    ).fetchall()
    conn.close()
    return {row["ticket_id"]: dict(row) for row in rows}


def enrich_dispatch_ticket(row: dict[str, Any], unresolved: dict | None = None) -> dict[str, Any]:
    item = dict(row)
    item["risk_hits"] = detect_risk_hits(item.get("description") or "")
    item["risk_level"] = min((hit["level"] for hit in item["risk_hits"]), key=lambda level: RISK_LEVEL_ORDER[level], default="")
    item["demand"] = classify_merchant_demand(item.get("category", "其他"), item.get("description") or "")
    item["is_ka"] = bool(item.get("is_ka") or item.get("brand"))
    prior = (unresolved or {}).get(item["ticket_id"])
    item["assignment_blocked"] = prior["state"] if prior else ""
    return item


def recommend_assignments(tickets: list[dict], users: list[dict], overrides: dict, risk_recipient: str) -> list[dict]:
    stats = {row["name"]: row for row in team_assignment_statistics()}
    totals = {user["name"]: stats[user["name"]]["assigned_today"] for user in users}
    categories = {user["name"]: Counter(stats[user["name"]]["category_counts"]) for user in users}
    by_name = {user["name"]: user for user in users}
    assignments = []
    for ticket in tickets:
        chosen = overrides.get(ticket["ticket_id"]) or (risk_recipient if ticket["risk_level"] else "")
        if not chosen:
            chosen = min(users, key=lambda user: (totals[user["name"]], categories[user["name"]][ticket["category"]]))["name"]
        assignments.append({"ticket": ticket, "user": by_name[chosen]})
        totals[chosen] += 1
        categories[chosen][ticket["category"]] += 1
    return assignments


def verify_assignment_results() -> dict[str, Any]:
    """Read-only secondary-source checks. Absence or lag is never proof of failure."""
    pending = unresolved_assignments()
    verified, warnings = [], []
    names = sorted({row["target_name"] for row in pending.values()})
    def read_handler(name):
        try:
            return name, {row["ticket_id"]: row for row in fetch_open_handler_rows(name)}, None
        except Exception:
            return name, {}, True
    with ThreadPoolExecutor(max_workers=4) as pool:
        observations = list(pool.map(read_handler, names))
    for name, current, error in observations:
        if error:
            warnings.append(f"{name}的处理人数据暂时无法读取")
            continue
        conn = get_db()
        for ticket_id, audit in pending.items():
            row = current.get(ticket_id)
            observed_at = row.get("fetched_at", "") if row else ""
            if audit["target_name"] != name or not row or row.get("handler") != name:
                continue
            # A read that started before submission must not certify this attempt.
            if not observed_at or observed_at < audit["requested_at"]:
                continue
            cursor = conn.execute(
                "UPDATE assignment_audit SET state='已核验', message=? WHERE id=? AND state IN (?,?,?) "
                "AND id=(SELECT MAX(b.id) FROM assignment_audit b WHERE b.ticket_id=? AND b.state NOT IN ('失败','未提交'))",
                (f"源工单库核验：当前处理人为{name}；读取时间 {observed_at}（源库同步可能延迟）", audit["id"], *ASSIGNMENT_UNRESOLVED, ticket_id),
            )
            if cursor.rowcount:
                verified.append(ticket_id)
        conn.commit()
        conn.close()
    return {"verified": verified, "remaining": len(pending) - len(verified), "warnings": warnings,
            "source": "源工单库处理人；非天润实时详情，存在同步延迟，查不到不判失败"}


def normalize_category(raw: str) -> str:
    value = clean(raw)
    if "多付/漏付" in value:
        return "多付/漏付"
    if "介入" in value:
        return "介入问题"
    if "批量" in value:
        return "批量核实"
    if "回访" in value:
        return "回访工单"
    return "其他"


def parse_duration(text: str) -> int | None:
    value = clean(text)
    if not value or value in {"—", "-"} or "反馈" in value:
        return None
    total = 0
    units = [("天", 1440), ("小时", 60), ("分钟", 1)]
    found = False
    for unit, factor in units:
        match = re.search(rf"(\d+)\s*{unit}", value)
        if match:
            total += int(match.group(1)) * factor
            found = True
    return total if found else None


def source_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": "DailyTicketDashboard/1.0 (read-only analytics)"})
    # The private-network tunnel can briefly flap while reconnecting. Retry
    # idempotent reads so one transient routing failure does not lose a whole
    # five-minute collection cycle.
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=2,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session.mount("http://", HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=12))
    return session


def fetch_ticket_day(day: date) -> list[dict[str, Any]]:
    session = source_session()
    url = urljoin(SOURCE_BASE, "list")
    first = session.get(url, params={"date": day.isoformat(), "page": 1}, timeout=(8, 30))
    first.raise_for_status()
    soup = BeautifulSoup(first.text, "html.parser")
    pager = clean(soup.select_one(".pager").get_text(" ", strip=True) if soup.select_one(".pager") else "")
    match = re.search(r"第\s*\d+\s*/\s*(\d+)\s*页", pager)
    pages = int(match.group(1)) if match else 1
    rows = parse_ticket_rows(soup)
    for page in range(2, pages + 1):
        response = session.get(url, params={"date": day.isoformat(), "page": page}, timeout=(8, 30))
        response.raise_for_status()
        rows.extend(parse_ticket_rows(BeautifulSoup(response.text, "html.parser")))
    return rows


def parse_merchant_rows(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Parse the separate merchant-app inspection list.

    Merchant rows use the event id as the source key, while the visible
    inspection number is carried by ``data-inspect`` and is the business id
    shown to operators.
    """
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    for tr in soup.select("tr.row[data-source='merchant'], tr.row"):
        if tr.get("data-source") not in {None, "merchant"}:
            continue
        cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
        if len(cells) < 13:
            continue
        event_id = clean(tr.get("data-event-id"))
        inspection_id = clean(tr.get("data-inspect")) or cells[1]
        if not event_id:
            event_id = inspection_id
        created_at = cells[8]
        created_date = created_at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", created_at) else ""
        brand = cells[3]
        inspect_status = cells[5] or "未知"
        records.append(
            {
                "event_id": event_id,
                "inspection_id": inspection_id,
                "shop_id": cells[2],
                "brand": "" if brand in {"-", "—"} else brand,
                "description": cells[4],
                "inspect_status": inspect_status,
                "abnormal_state": cells[6] if cells[6] not in {"", "—"} else "待确认",
                "abnormal_type": cells[7] if cells[7] not in {"", "—"} else "",
                "created_at": created_at,
                "created_date": created_date,
                "completed_at": cells[9] if cells[9] not in {"", "—"} else "",
                "duration_text": cells[10] if cells[10] not in {"", "—"} else "",
                "duration_minutes": parse_duration(cells[10]),
                "pipeline_text": cells[11],
                "is_ka": int(bool(brand and brand not in {"-", "—"})),
                "is_open": int(inspect_status in MERCHANT_PENDING_STATUSES),
                "fetched_at": now,
            }
        )
    return list({row["event_id"]: row for row in records}.values())


def fetch_merchant_day(day: date, open_only: bool = False) -> list[dict[str, Any]]:
    session = source_session()
    url = urljoin(SOURCE_BASE, "list")
    params: dict[str, Any] = {"tab": "merchant", "page": 1}
    if open_only:
        params["m_open"] = "1"
    else:
        params["m_date"] = day.isoformat()
    first = session.get(url, params=params, timeout=(8, 30))
    first.raise_for_status()
    soup = BeautifulSoup(first.text, "html.parser")
    pager = clean(soup.select_one(".pager").get_text(" ", strip=True) if soup.select_one(".pager") else "")
    match = re.search(r"第\s*\d+\s*/\s*(\d+)\s*页", pager)
    pages = int(match.group(1)) if match else 1
    rows = parse_merchant_rows(soup)
    for page in range(2, pages + 1):
        params["page"] = page
        response = session.get(url, params=params, timeout=(8, 30))
        response.raise_for_status()
        rows.extend(parse_merchant_rows(BeautifulSoup(response.text, "html.parser")))
    return list({row["event_id"]: row for row in rows}.values())


def save_merchant_day(conn: sqlite3.Connection, day: date, records: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM merchant_inspections WHERE created_date = ?", (day.isoformat(),))
    if records:
        columns = list(records[0].keys())
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT OR REPLACE INTO merchant_inspections ({','.join(columns)}) VALUES ({placeholders})"
        conn.executemany(sql, ([record[col] for col in columns] for record in records))
    conn.commit()


def fetch_open_handler_rows(handler: str) -> list[dict[str, Any]]:
    """Read the current open queue from the upstream dashboard, not a creation-date slice."""
    session = source_session()
    url = urljoin(SOURCE_BASE, "list")
    params = {"closed": "open", "handler": handler, "page": 1}
    first = session.get(url, params=params, timeout=(8, 30))
    first.raise_for_status()
    soup = BeautifulSoup(first.text, "html.parser")
    pager = clean(soup.select_one(".pager").get_text(" ", strip=True) if soup.select_one(".pager") else "")
    match = re.search(r"第\s*\d+\s*/\s*(\d+)\s*页", pager)
    pages = int(match.group(1)) if match else 1
    rows = parse_ticket_rows(soup)
    for page in range(2, pages + 1):
        params["page"] = page
        response = session.get(url, params=params, timeout=(8, 30))
        response.raise_for_status()
        rows.extend(parse_ticket_rows(BeautifulSoup(response.text, "html.parser")))
    return list({row["ticket_id"]: row for row in rows}.values())


def pending_guard_tickets(refresh: bool = False) -> tuple[list[dict[str, Any]], str | None]:
    warning = None
    if refresh:
        try:
            live_rows = fetch_open_handler_rows(GUARD_QUEUE_HANDLER)
            conn = get_db()
            upsert_ticket_rows(conn, live_rows)
            conn.close()
            rows = [row for row in live_rows if row["status"] in PENDING_ASSIGN_STATUSES and row.get("handler") == GUARD_QUEUE_HANDLER]
            rows.sort(key=lambda row: (-(row.get("duration_minutes") or 0), row.get("created_at") or ""))
            for row in rows:
                row["source_url"] = f"{SOURCE_BASE}events/detail?event_id={row['ticket_id']}"
            return rows, None
        except Exception as exc:
            warning = f"实时读取失败，当前展示最近一次本地同步数据：{exc}"
    conn = get_db()
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT ticket_id,shop_id,brand,category_raw,category,description,status,handler,
                      created_at,duration_text,duration_minutes,fetched_at,is_ka,tags
               FROM tickets
               WHERE status IN ('待领取','待接单') AND handler=?
               ORDER BY COALESCE(duration_minutes,0) DESC, created_at ASC""",
            (GUARD_QUEUE_HANDLER,),
        )
    ]
    conn.close()
    for row in rows:
        row["source_url"] = f"{SOURCE_BASE}events/detail?event_id={row['ticket_id']}"
    return rows, warning


def parse_ticket_rows(soup: BeautifulSoup) -> list[dict[str, Any]]:
    now = datetime.now().isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    for tr in soup.select("tr.row"):
        cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
        if len(cells) < 16:
            continue
        ticket_id = tr.get("data-event-id") or cells[1]
        created_at = cells[11]
        created_date = created_at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", created_at) else ""
        brand = cells[4]
        records.append(
            {
                "ticket_id": ticket_id,
                "shop_id": cells[3],
                "brand": "" if brand in {"-", "—"} else brand,
                "category_raw": cells[5],
                "category": normalize_category(cells[5]),
                "description": cells[6],
                "status": cells[7],
                "inspect_status": cells[8],
                "tags": cells[9],
                "handler": cells[10],
                "created_at": created_at,
                "created_date": created_date,
                "duration_text": cells[12],
                "duration_minutes": parse_duration(cells[12]),
                "inspect_completed_at": cells[13],
                "response_text": cells[14],
                "response_minutes": parse_duration(cells[14]),
                "pipeline_text": cells[15],
                "is_ka": int(bool(brand and brand not in {"-", "—"})),
                "fetched_at": now,
            }
        )
    return records


def parse_ticket_evidence(ticket_id: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    checks = soup.select(".node.n-check")
    results: list[str] = []
    metas: list[str] = []
    abnormal_types: list[str] = []
    states: list[str] = []
    for node in checks:
        result = node.select_one(".node-desc")
        if result:
            value = clean(result.get_text(" ", strip=True)).removeprefix("稽查结果：")
            if value:
                results.append(value)
        meta = node.select_one(".node-meta")
        if meta:
            meta_text = clean(meta.get_text(" ", strip=True))
            metas.append(meta_text)
            match = re.search(r"异常类型[：:]\s*([^·]+)", meta_text)
            if match:
                abnormal_types.append(clean(match.group(1)))
        flag = node.select_one(".exc-flag")
        if flag:
            states.append(clean(flag.get_text(" ", strip=True)))
    loss_items = [clean(node.get_text(" ", strip=True)) for node in soup.select(".node.n-loss .node-desc")]
    if "有异常" in states:
        abnormal_state = "有异常"
    elif "无异常" in states:
        abnormal_state = "无异常"
    elif checks:
        abnormal_state = "状态待确认"
    else:
        abnormal_state = "未稽查"
    return {
        "ticket_id": ticket_id,
        "check_count": len(checks),
        "abnormal_state": abnormal_state,
        "abnormal_type": "；".join(dict.fromkeys(abnormal_types)),
        "check_result": "；".join(dict.fromkeys(results)),
        "check_meta": "；".join(dict.fromkeys(metas)),
        "loss_summary": "；".join(dict.fromkeys(loss_items)),
        "detail_status": "ok",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def fetch_ticket_evidence(ticket_id: str) -> dict[str, Any]:
    session = source_session()
    url = urljoin(SOURCE_BASE, "events/detail")
    try:
        response = session.get(url, params={"event_id": ticket_id}, timeout=(8, 25))
        response.raise_for_status()
        return parse_ticket_evidence(ticket_id, response.text)
    except Exception as exc:
        return {
            "ticket_id": ticket_id,
            "check_count": 0,
            "abnormal_state": "读取失败",
            "abnormal_type": "",
            "check_result": "",
            "check_meta": "",
            "loss_summary": "",
            "detail_status": f"error: {exc}",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }


def save_evidence(conn: sqlite3.Connection, evidence: dict[str, Any]) -> None:
    columns = list(evidence.keys())
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT OR REPLACE INTO ticket_evidence ({','.join(columns)}) VALUES ({placeholders})",
        [evidence[col] for col in columns],
    )


def sync_evidence_for_day(selected: date, force: bool = False) -> dict[str, Any]:
    """补齐某日每张工单的稽查子链；任何失败都保留为证据缺口。"""
    with EVIDENCE_LOCK:
        EVIDENCE_STATE.update(
            {
                "running": True,
                "message": f"正在补齐 {selected.isoformat()} 的逐单稽查证据",
                "date": selected.isoformat(),
            }
        )
        done = 0
        errors = 0
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT ticket_id FROM tickets WHERE created_date=? ORDER BY created_at DESC", (selected.isoformat(),)
            ).fetchall()
            ticket_ids = [row["ticket_id"] for row in rows]
            if not force and ticket_ids:
                cached = {
                    row["ticket_id"]
                    for row in conn.execute(
                        f"SELECT ticket_id FROM ticket_evidence WHERE detail_status='ok' "
                        f"AND ticket_id IN ({','.join('?' for _ in ticket_ids)})",
                        ticket_ids,
                    ).fetchall()
                }
                ticket_ids = [ticket_id for ticket_id in ticket_ids if ticket_id not in cached]
            conn.close()
            total = len(ticket_ids)
            EVIDENCE_STATE["total"] = total
            if total:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    futures = {pool.submit(fetch_ticket_evidence, ticket_id): ticket_id for ticket_id in ticket_ids}
                    for future in as_completed(futures):
                        try:
                            evidence = future.result()
                        except Exception as exc:
                            evidence = {
                                "ticket_id": futures[future],
                                "check_count": 0,
                                "abnormal_state": "读取失败",
                                "abnormal_type": "",
                                "check_result": "",
                                "check_meta": "",
                                "loss_summary": "",
                                "detail_status": f"error: {exc}",
                                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                            }
                        db = get_db()
                        save_evidence(db, evidence)
                        db.commit()
                        db.close()
                        done += 1
                        errors += int(evidence["detail_status"] != "ok")
                        EVIDENCE_STATE.update(
                            {"message": f"逐单证据补齐中：{done}/{total}", "done": done, "errors": errors}
                        )
            finished = datetime.now().isoformat(timespec="seconds")
            message = f"逐单证据已补齐：新增 {done} 单，失败 {errors} 单"
            EVIDENCE_STATE.update({"running": False, "message": message, "finished_at": finished})
            return {"ok": True, "count": done, "errors": errors, "date": selected.isoformat()}
        except Exception as exc:
            finished = datetime.now().isoformat(timespec="seconds")
            EVIDENCE_STATE.update(
                {"running": False, "message": f"逐单证据同步失败：{exc}", "finished_at": finished}
            )
            raise


def save_day(conn: sqlite3.Connection, day: date, records: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM tickets WHERE created_date = ?", (day.isoformat(),))
    if not records:
        conn.commit()
        return
    columns = list(records[0].keys())
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT OR REPLACE INTO tickets ({','.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, ([record[col] for col in columns] for record in records))
    conn.commit()


def upsert_ticket_rows(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    columns = list(records[0].keys())
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT OR REPLACE INTO tickets ({','.join(columns)}) VALUES ({placeholders})"
    conn.executemany(sql, ([record[col] for col in columns] for record in records))
    conn.commit()


def sync_days(days: int = 14, end_day: date | None = None) -> dict[str, Any]:
    end_day = end_day or date.today()
    start_day = end_day - timedelta(days=max(2, days) - 1)
    started = datetime.now().isoformat(timespec="seconds")
    with SYNC_LOCK:
        SYNC_STATE.update({"running": True, "message": f"正在同步 {start_day} 至 {end_day}"})
        conn = get_db()
        run_id = conn.execute(
            "INSERT INTO sync_runs(started_at,date_from,date_to,status) VALUES(?,?,?,?)",
            (started, start_day.isoformat(), end_day.isoformat(), "running"),
        ).lastrowid
        conn.commit()
        total = 0
        merchant_total = 0
        try:
            current = start_day
            while current <= end_day:
                SYNC_STATE["message"] = f"正在读取 {current.isoformat()}"
                records = fetch_ticket_day(current)
                merchant_records = fetch_merchant_day(current)
                save_day(conn, current, records)
                save_merchant_day(conn, current, merchant_records)
                total += len(records)
                merchant_total += len(merchant_records)
                current += timedelta(days=1)
            # Read the entire current pending queue; a date slice cannot find
            # older overdue orders or reliably detect their later completion.
            SYNC_STATE["message"] = "正在核对商家APP当前未完成稽查队列"
            open_records = fetch_merchant_day(end_day, open_only=True)
            conn.execute("UPDATE merchant_inspections SET is_open=0")
            if open_records:
                columns = list(open_records[0].keys())
                placeholders = ",".join("?" for _ in columns)
                sql = f"INSERT OR REPLACE INTO merchant_inspections ({','.join(columns)}) VALUES ({placeholders})"
                conn.executemany(sql, ([record[col] for col in columns] for record in open_records))
            finished = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT OR REPLACE INTO merchant_sync_state(state_key,state_value) VALUES('open_synced_at',?)",
                (finished,),
            )
            for current_day in (start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)):
                conn.execute(
                    "INSERT OR REPLACE INTO merchant_sync_state(state_key,state_value) VALUES(?,?)",
                    ("day:" + current_day.isoformat(), finished),
                )
            message = f"同步完成，天润 {total} 条，商家APP {merchant_total} 条；当前APP未完成 {len(open_records)} 条"
            conn.execute(
                "UPDATE sync_runs SET finished_at=?,ticket_count=?,merchant_count=?,status='ok',message=? WHERE id=?",
                (finished, total, merchant_total, message, run_id),
            )
            conn.commit()
            SYNC_STATE.update({"running": False, "message": message, "finished_at": finished})
            return {"ok": True, "count": total + merchant_total, "tianrun_count": total,
                    "merchant_count": merchant_total, "date_from": start_day.isoformat(), "date_to": end_day.isoformat()}
        except Exception as exc:
            finished = datetime.now().isoformat(timespec="seconds")
            message = f"同步失败：{exc}"
            conn.execute(
                "UPDATE sync_runs SET finished_at=?,ticket_count=?,status='error',message=? WHERE id=?",
                (finished, total, message, run_id),
            )
            conn.commit()
            SYNC_STATE.update({"running": False, "message": message, "finished_at": finished})
            raise
        finally:
            conn.close()


def run_auto_sync_loop() -> None:
    """持续更新今天与前一天，避免定时任务反复抓取全部历史数据。"""
    while True:
        started = datetime.now()
        AUTO_SYNC_STATE.update(
            {
                "last_started_at": started.isoformat(timespec="seconds"),
                "next_run_at": None,
                "last_error": None,
            }
        )
        try:
            sync_days(days=2)
            AUTO_SYNC_STATE["last_finished_at"] = datetime.now().isoformat(timespec="seconds")
        except Exception as exc:
            AUTO_SYNC_STATE["last_error"] = str(exc)

        next_run = datetime.now() + timedelta(seconds=AUTO_SYNC_INTERVAL_SECONDS)
        AUTO_SYNC_STATE["next_run_at"] = next_run.isoformat(timespec="seconds")
        time.sleep(max(1, (next_run - datetime.now()).total_seconds()))


def rows_for_range(conn: sqlite3.Connection, start_day: date, end_day: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM tickets WHERE created_date BETWEEN ? AND ? ORDER BY created_at",
        (start_day.isoformat(), end_day.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def merchant_rows_for_range(conn: sqlite3.Connection, start_day: date, end_day: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM merchant_inspections WHERE created_date BETWEEN ? AND ? ORDER BY created_at",
        (start_day.isoformat(), end_day.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def build_merchant_complaint_ranking(end_day: date | None = None, limit: int = 50) -> dict[str, Any]:
    """Aggregate the latest rolling 90 days by shop, keeping source counts separate."""
    conn = get_db()
    latest = conn.execute(
        "SELECT MAX(created_date) FROM (SELECT created_date FROM tickets UNION ALL "
        "SELECT created_date FROM merchant_inspections)"
    ).fetchone()[0]
    end_day = end_day or (date.fromisoformat(latest) if latest else date.today())
    start_day = end_day - timedelta(days=89)
    start_key, end_key = start_day.isoformat(), end_day.isoformat()

    tianrun_rows = [dict(row) for row in conn.execute(
        "SELECT ticket_id,shop_id,brand,category,description,status,created_at,is_ka FROM tickets "
        "WHERE created_date BETWEEN ? AND ? ORDER BY created_at DESC", (start_key, end_key)
    )]
    app_events = [dict(row) for row in conn.execute(
        "SELECT event_id,inspection_id,shop_id,brand,abnormal_type,description,inspect_status,created_at,is_ka "
        "FROM merchant_inspections WHERE created_date BETWEEN ? AND ? ORDER BY created_at DESC",
        (start_key, end_key),
    )]
    # Multiple source events can point at the same inspection. Count the
    # business inspection once, retaining its most recent source record.
    inspections: dict[tuple[str, str], dict[str, Any]] = {}
    for row in app_events:
        inspection_key = clean(row["inspection_id"])
        if inspection_key in {"", "-", "—"}:
            inspection_key = row["event_id"]
        inspections.setdefault((clean(row.get("shop_id")), inspection_key), row)
    app_rows = list(inspections.values())

    by_shop: dict[str, dict[str, Any]] = {}

    def shop_for(shop_id: str) -> dict[str, Any]:
        return by_shop.setdefault(shop_id, {"shop_id": shop_id, "brand": "", "brand_at": "", "is_ka": False,
                                            "tianrun_count": 0, "merchant_app_count": 0,
                                            "last_7_count": 0, "last_ticket_at": "", "tickets": []})

    def add_ticket(shop_id: str, ticket: dict[str, Any], source: str) -> None:
        if not shop_id or shop_id.strip() in {"-", "—"}:
            return
        shop = shop_for(shop_id.strip())
        brand = (ticket.get("brand") or "").strip()
        if brand and brand not in {"-", "—"} and ticket["created_at"] > shop["brand_at"]:
            shop["brand"] = brand
            shop["brand_at"] = ticket["created_at"]
        shop["is_ka"] = shop["is_ka"] or bool(ticket.get("is_ka"))
        shop["last_ticket_at"] = max(shop["last_ticket_at"], ticket.get("created_at") or "")
        if (ticket.get("created_at") or "")[:10] >= (end_day - timedelta(days=6)).isoformat():
            shop["last_7_count"] += 1
        if source == "天润工单":
            shop["tianrun_count"] += 1
            record_id = ticket["ticket_id"]
            category = ticket.get("category") or "其他"
            status = ticket.get("status") or ""
            source_url = f"{SOURCE_BASE}events/detail?event_id={record_id}"
        else:
            shop["merchant_app_count"] += 1
            record_id = ticket.get("inspection_id") or ticket["event_id"]
            category = ticket.get("abnormal_type") or "商家APP稽查单"
            status = ticket.get("inspect_status") or ""
            source_url = f"{SOURCE_BASE}events/detail?event_id={ticket['event_id']}&source=merchant"
        shop["tickets"].append({
            "source_label": source,
            "ticket_id": record_id,
            "shop_id": shop_id.strip(),
            "brand": brand or "未匹配品牌",
            "category": category,
            "description": ticket.get("description") or "",
            "status": status,
            "created_at": ticket.get("created_at") or "",
            "source_url": source_url,
        })

    for row in tianrun_rows:
        add_ticket(row.get("shop_id") or "", row, "天润工单")
    for row in app_rows:
        add_ticket(row.get("shop_id") or "", row, "商家APP")

    merchants = []
    for merchant in by_shop.values():
        merchant.pop("brand_at")
        merchant["total"] = merchant["tianrun_count"] + merchant["merchant_app_count"]
        merchant["tickets"].sort(key=lambda row: row["created_at"], reverse=True)
        merchants.append(merchant)
    merchants.sort(key=lambda row: (-row["total"], -row["last_7_count"], row["brand"], row["shop_id"]))
    tianrun_total = sum(merchant["tianrun_count"] for merchant in merchants)
    merchant_app_total = sum(merchant["merchant_app_count"] for merchant in merchants)
    for rank, merchant in enumerate(merchants[:max(1, min(limit, 100))], start=1):
        merchant["rank"] = rank

    # A zero-count day is only trustworthy when both source queries completed for it.
    successful_ranges = conn.execute(
        "SELECT DISTINCT date_from,date_to FROM sync_runs WHERE status='ok' AND date_from<=? AND date_to>=?",
        (end_key, start_key),
    ).fetchall()
    tianrun_synced = set()
    for from_value, to_value in successful_ranges:
        first = max(start_day, date.fromisoformat(from_value))
        last = min(end_day, date.fromisoformat(to_value))
        cursor = first
        while cursor <= last:
            tianrun_synced.add(cursor.isoformat())
            cursor += timedelta(days=1)
    app_synced = {
        row[0][4:] for row in conn.execute(
            "SELECT state_key FROM merchant_sync_state WHERE state_key LIKE 'day:%' AND substr(state_key,5) BETWEEN ? AND ?",
            (start_key, end_key),
        )
    }
    conn.close()

    expected_dates = {(start_day + timedelta(days=i)).isoformat() for i in range(90)}
    fully_synced = tianrun_synced & app_synced & expected_dates
    contiguous_days = 0
    cursor = end_day
    while cursor.isoformat() in fully_synced:
        contiguous_days += 1
        cursor -= timedelta(days=1)
    missing_days = sorted(expected_dates - fully_synced)
    return {
        "period_start": start_key,
        "period_end": end_key,
        "window_days": 90,
        "coverage": {
            "fully_synced_days": len(fully_synced),
            "expected_days": 90,
            "contiguous_days_to_end": contiguous_days,
            "missing_days": len(missing_days),
            "first_missing_date": missing_days[0] if missing_days else None,
            "complete": len(fully_synced) == 90,
            "tianrun_synced_days": len(tianrun_synced),
            "merchant_app_synced_days": len(app_synced),
        },
        "merchant_count": len(merchants),
        "tianrun_count": tianrun_total,
        "merchant_app_count": merchant_app_total,
        "total_count": tianrun_total + merchant_app_total,
        "unmatched_tianrun_count": sum((row.get("shop_id") or "").strip() in {"", "-", "—"} for row in tianrun_rows),
        "unmatched_merchant_app_count": sum((row.get("shop_id") or "").strip() in {"", "-", "—"} for row in app_rows),
        "ranking": merchants[:max(1, min(limit, 100))],
        "definition": "近三个月按滚动90天统计，截止本地最新数据日，不随上方分析日期切换；按店铺ID归集，品牌仅为展示名称；提单次数不代表投诉成立次数。天润按工单ID、APP按稽查单ID计数，同一事项跨入口不去重；无店铺ID记录不参与排行。",
    }


def merchant_age_minutes(created_at: str, now: datetime | None = None) -> int | None:
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=BEIJING_TZ)
    except (TypeError, ValueError):
        return None
    current = now or datetime.now(BEIJING_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    return max(0, int((current.astimezone(BEIJING_TZ) - created).total_seconds() // 60))


def merchant_record_payload(record: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    age_minutes = merchant_age_minutes(record.get("created_at", ""), now)
    risk_hits = merge_risk_hits(
        [("问题描述", record.get("description", "")), ("异常类型", record.get("abnormal_type", ""))]
    )
    return {
        "event_id": record["event_id"],
        "inspection_id": record["inspection_id"],
        "shop_id": record["shop_id"],
        "brand": record.get("brand") or "非KA/未匹配",
        "description": record.get("description") or "未填写问题描述",
        "inspect_status": record.get("inspect_status") or "未知",
        "abnormal_state": record.get("abnormal_state") or "待确认",
        "abnormal_type": record.get("abnormal_type") or "未分类",
        "created_at": record.get("created_at") or "",
        "completed_at": record.get("completed_at") or "",
        "duration_text": record.get("duration_text") or "",
        "pipeline_text": record.get("pipeline_text") or "",
        "age_minutes": age_minutes,
        "age_hours": round(age_minutes / 60, 1) if age_minutes is not None else None,
        "is_ka": bool(record.get("is_ka")),
        "is_open": bool(record.get("is_open")),
        "overdue_24h": bool(record.get("is_open") and record.get("inspect_status") in {"未稽查", "待稽查"}
                            and not record.get("completed_at") and age_minutes is not None and age_minutes >= 1440),
        "risk_level": min(
            (hit["level"] for hit in risk_hits), key=lambda level: RISK_LEVEL_ORDER.get(level, 99)
        ) if risk_hits else "",
        "risk_hits": risk_hits,
        "source_url": f"{SOURCE_BASE}events/detail?event_id={record['event_id']}&source=merchant",
    }


def classify_topic(record: dict[str, Any]) -> str:
    description = record["description"]
    for topic, pattern in TOPIC_RULES:
        if re.search(pattern, description, re.I):
            return topic
    fallback = {
        "多付/漏付": "支付/订单核实（泛化描述）",
        "介入问题": "值守介入核实（泛化描述）",
        "批量核实": "批量回溯/资料协助",
        "回访工单": "回访待补充信息",
    }
    return fallback.get(record["category"], "其他问题")


def classify_merchant_demand(category: str, description: str) -> str:
    for demand, pattern in MERCHANT_DEMAND_RULES.get(category, []):
        if re.search(pattern, clean(description), re.I):
            return demand
    return "其他/描述不够具体"


def build_category_insights(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    evidence_by_ticket: dict[str, dict[str, Any]],
    selected: date,
) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    for category in CORE_CATEGORY_ORDER:
        current_rows = [row for row in current if row["category"] == category]
        previous_rows = [row for row in previous if row["category"] == category]
        current_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        previous_counts: Counter[str] = Counter()
        for row in current_rows:
            current_groups[classify_merchant_demand(category, row["description"])].append(row)
        for row in previous_rows:
            previous_counts[classify_merchant_demand(category, row["description"])] += 1

        demands = []
        for demand, demand_rows in sorted(current_groups.items(), key=lambda item: len(item[1]), reverse=True):
            count = len(demand_rows)
            prev_count = previous_counts.get(demand, 0)
            demands.append(
                {
                    "demand": demand,
                    "count": count,
                    "ratio": safe_pct(count, len(current_rows)),
                    "previous": prev_count,
                    "delta": count - prev_count,
                    "samples": [
                        {
                            "ticket_id": row["ticket_id"],
                            "description": row["description"],
                            "source_url": f"{SOURCE_BASE}events/detail?event_id={row['ticket_id']}",
                        }
                        for row in demand_rows[:3]
                    ],
                }
            )

        status_counts = Counter(row["status"] for row in current_rows)
        inspect_counts = Counter(
            "已确认异常"
            if "有异常" in row.get("pipeline_text", "")
            else "已核实无异常"
            if "无异常" in row.get("pipeline_text", "")
            else "尚无明确稽查结论"
            for row in current_rows
        )
        attention_records = []
        sensitive_risk_count = 0
        processing_anomaly_count = 0
        for row in current_rows:
            evidence = evidence_by_ticket.get(row["ticket_id"], {})
            risk_hits = merge_risk_hits(
                [("工单描述", row["description"]), ("稽查异常类型", evidence.get("abnormal_type", ""))]
            )
            reasons = record_anomaly_reasons(row, selected, risk_hits)
            if reasons:
                sensitive_risk_count += int(any("风险：" in reason for reason in reasons))
                processing_anomaly_count += int(any("风险：" not in reason for reason in reasons))
                attention_records.append(
                    {
                        "ticket_id": row["ticket_id"],
                        "shop_id": row["shop_id"],
                        "brand": row["brand"] or "非KA/未匹配",
                        "description": row["description"],
                        "status": row["status"],
                        "created_at": row["created_at"],
                        "reasons": reasons,
                        "source_url": f"{SOURCE_BASE}events/detail?event_id={row['ticket_id']}",
                    }
                )
        attention_records.sort(key=lambda row: (len(row["reasons"]), row["created_at"]), reverse=True)

        top_two_count = sum(item["count"] for item in demands[:2])
        pending_count = sum(status_counts.get(status, 0) for status in ("待领取", "待接单"))
        vague_count = len(current_groups.get("其他/描述不够具体", []))
        common_points = []
        if demands:
            common_points.append(
                f"诉求集中在“{demands[0]['demand']}”和“{demands[1]['demand']}”，合计占{safe_pct(top_two_count, len(current_rows))}%"
                if len(demands) > 1
                else f"主要诉求为“{demands[0]['demand']}”，占{demands[0]['ratio']}%"
            )
        if pending_count:
            common_points.append(f"当前仍有{pending_count}单待领取或待接单，需要优先分派")
        if vague_count:
            common_points.append(f"有{vague_count}单描述无法明确归入具体诉求，需补充时间、商品或处理目标")
        if inspect_counts.get("尚无明确稽查结论", 0):
            common_points.append(f"有{inspect_counts['尚无明确稽查结论']}单尚无明确稽查结论，暂不能直接判断责任")

        current_count = len(current_rows)
        previous_count = len(previous_rows)
        delta = current_count - previous_count
        insights.append(
            {
                "category": category,
                "current": current_count,
                "previous": previous_count,
                "delta": delta,
                "pct": round(delta * 100 / previous_count, 1) if previous_count else (100.0 if current_count else 0.0),
                "share": safe_pct(current_count, len(current)),
                "demands": demands,
                "status_summary": [{"name": key, "count": value} for key, value in status_counts.most_common()],
                "verification_summary": [
                    {"name": key, "count": value} for key, value in inspect_counts.most_common()
                ],
                "attention_count": len(attention_records),
                "sensitive_risk_count": sensitive_risk_count,
                "processing_anomaly_count": processing_anomaly_count,
                "attention_records": attention_records[:20],
                "common_points": common_points,
            }
        )
    return insights


def safe_pct(part: float, total: float) -> float:
    return round(part * 100 / total, 1) if total else 0.0


def natural_week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def completion_record_result(record: dict[str, Any], as_of: datetime | None = None) -> dict[str, Any]:
    """Measure submit-to-first-inspection time; never substitute ticket close/duration fields."""
    created_raw = clean(record.get("created_at"))
    try:
        created = datetime.fromisoformat(created_raw)
        if created.tzinfo is not None:
            created = created.astimezone(BEIJING_TZ).replace(tzinfo=None)
    except (TypeError, ValueError):
        created = None

    completed_values = re.findall(
        r"稽查完成\s*[:：]?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        clean(record.get("check_meta")),
    )
    parsed = []
    for value in completed_values:
        try:
            parsed.append(datetime.strptime(value, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
    parsed.sort()
    marker = clean(record.get("response_text")) == "稽查完成后反馈"
    check_meta = clean(record.get("check_meta"))
    status = "待核验"
    reason = ""
    first_completed = ""
    seconds = None

    if created is None:
        reason = "商家提单时间缺失或格式异常"
    elif parsed and parsed[0] < created:
        first_completed = parsed[0].strftime("%Y-%m-%d %H:%M:%S")
        reason = "详情存在早于提单时间的稽查完成节点，关联关系待核验"
    elif parsed:
        first = parsed[0]
        seconds = (first - created).total_seconds()
        first_completed = first.strftime("%Y-%m-%d %H:%M:%S")
        status = "24小时内" if seconds <= 24 * 60 * 60 else "超时"
    elif not clean(record.get("detail_status")):
        reason = "工单详情尚未抓取"
    elif record.get("detail_status", "") != "ok":
        reason = "工单详情读取失败"
    elif "稽查完成" in check_meta:
        reason = "存在稽查完成节点，但时间格式无法识别"
    else:
        current = as_of or datetime.now(BEIJING_TZ)
        if current.tzinfo is not None:
            current = current.astimezone(BEIJING_TZ).replace(tzinfo=None)
        seconds = int((current - created).total_seconds())
        if seconds < 0:
            seconds = None
            reason = "商家提单时间晚于当前统计时点，暂待核验"
        elif seconds < 24 * 60 * 60:
            status = "未完成"
            reason = "详情未找到稽查完成时间，按当前未完成计；尚未超过24小时"
        else:
            status = "超时未完成"
            reason = "详情未找到稽查完成时间，按当前未完成计；已超过24小时"
        if marker and status != "待核验":
            reason += "（仅有稽查完成后反馈标记）"

    return {
        "ticket_id": str(record.get("ticket_id", "")),
        "category": record.get("category") or "其他",
        "created_at": created_raw,
        "first_inspect_completed_at": first_completed,
        "elapsed_seconds": seconds,
        "elapsed_hours": round(seconds / 3600, 2) if seconds is not None else None,
        "result": status,
        "reason": reason,
        "source_url": f"{SOURCE_BASE}events/detail?event_id={record.get('ticket_id', '')}",
    }


def build_completion_stats(records: list[dict[str, Any]], as_of: datetime | None = None) -> dict[str, Any]:
    """Calculate submit-to-first-inspection completion metrics from cached detail nodes."""
    valid: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pending = Counter()
    classified = []
    for record in records:
        result = completion_record_result(record, as_of)
        classified.append((record, result))
        if result["result"] == "待核验":
            pending[result["reason"]] += 1
            continue
        valid.append((record, result))

    def one_group(group: list[tuple[dict[str, Any], dict[str, Any]]], pending_count: int = 0) -> dict[str, Any]:
        total = len(group)
        within24 = sum(result["result"] == "24小时内" for _, result in group)
        return {
            "completed": total,
            "within_24h": within24,
            "rate_24h": safe_pct(within24, total),
            "verified_time_count": sum(bool(result["first_inspect_completed_at"]) for _, result in group),
            "not_completed_count": sum(result["result"] in {"未完成", "超时未完成"} for _, result in group),
            "overdue_not_completed_count": sum(result["result"] == "超时未完成" for _, result in group),
            "pending_verification": pending_count,
        }

    overall = one_group(valid, sum(pending.values()))
    categories = []
    for category in [*CORE_CATEGORY_ORDER, "其他"]:
        group = [(record, result) for record, result in valid if record.get("category") == category]
        pending_count = sum(result["result"] == "待核验" for record, result in classified if record.get("category") == category)
        item = one_group(group, pending_count)
        item["category"] = category
        categories.append(item)
    return {
        **overall,
        "categories": categories,
        "excluded_breakdown": [{"reason": reason, "count": count} for reason, count in pending.items()],
        "source_definition": "口径：首次稽查完成时间 − 商家提单时间 ≤ 24小时；提单时间暂取天润工单创建时间。详情读取成功但未找到稽查完成时间的，按当前未完成计入分母：未满24小时列未完成，达到24小时列超时未完成；后续取得首次完成时间后按实际耗时重算。详情未抓取、读取失败、时间格式异常或节点冲突仍待核验，不纳入分母；“稽查完成后反馈”标记不替代实际完成时间。",
    }


def completion_rows_for_range(conn: sqlite3.Connection, start_day: date, end_day: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT t.*, COALESCE(e.check_meta,'') AS check_meta,
                  COALESCE(e.detail_status,'') AS detail_status
           FROM tickets t LEFT JOIN ticket_evidence e ON e.ticket_id=t.ticket_id
           WHERE t.created_date BETWEEN ? AND ? ORDER BY t.created_at""",
        (start_day.isoformat(), end_day.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def build_completion_week_snapshot(week_start: date, week_end: date) -> dict[str, Any]:
    conn = get_db()
    try:
        rows = completion_rows_for_range(conn, week_start, week_end)
    finally:
        conn.close()
    as_of = datetime.now(BEIJING_TZ)
    days = []
    day = week_start
    while day <= week_end:
        day_rows = [row for row in rows if row["created_date"] == day.isoformat()]
        days.append({
            "date": day.isoformat(),
            "stats": build_completion_stats(day_rows, as_of),
            "records": [completion_record_result(row, as_of) for row in day_rows],
        })
        day += timedelta(days=1)
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "complete": week_end == week_start + timedelta(days=6) and date.today() > week_end,
        "updated_at": as_of.isoformat(timespec="seconds"),
        "days": days,
    }


def build_completion_day_snapshot(selected: date) -> dict[str, Any]:
    conn = get_db()
    try:
        rows = completion_rows_for_range(conn, selected, selected)
    finally:
        conn.close()
    as_of = datetime.now(BEIJING_TZ)
    stats = build_completion_stats(rows, as_of)
    stats["selected_date"] = selected.isoformat()
    return {
        "date": selected.isoformat(),
        "stats": stats,
        "records": [completion_record_result(row, as_of) for row in rows],
    }


def build_completion_records(start_day: date, end_day: date) -> list[dict[str, Any]]:
    conn = get_db()
    try:
        rows = completion_rows_for_range(conn, start_day, end_day)
    finally:
        conn.close()
    return [completion_record_result(record) for record in rows]


def clip_text(value: str, limit: int = 180) -> str:
    value = clean(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def extract_observed_action(check_result: str) -> str:
    """从稽查话术中保留可观察动作，去掉问候及后续流程模板。"""
    value = clean(check_result)
    value = re.sub(r"^(老板|店主|商家)(您好|你好)[，,：:\s]*", "", value)
    value = re.split(r"[，,。](?:为了保障|辛苦您|请您|如有任何问题|若有疑问)", value, maxsplit=1)[0]
    return clip_text(value, 180)


def detect_behavior(description: str, check_result: str = "") -> str:
    evidence = extract_observed_action(check_result)
    if evidence:
        return evidence
    combined = clean(description)
    rules = [
        (r"拿.{0,20}(\d+|一|二|三|四|五|六|多)(包|盒|瓶|提|个).{0,30}(只|仅|少|未).{0,12}(付|扫)|支付.{0,18}(一|1).{0,16}(拿|带走)", "顾客拿取数量高于实际支付数量"),
        (r"未付款|没付款|没有付款|未支付|少付|漏付|少扫|漏扫", "商家主张顾客存在未支付或少支付商品"),
        (r"多付|重复支付|重复扣|多扫|多付款", "订单存在多支付或重复支付争议"),
        (r"偷盗|偷拿|盗窃|藏进|装入口袋|顺走|拿走.{0,12}(未|没)", "顾客疑似直接拿取、藏匿或带走商品"),
        (r"摔破|打碎|损坏|破损|掉落|变形", "顾客或现场行为疑似造成商品损坏"),
        (r"换了|拿错|调换|替换", "顾客存在商品调换、拿错或替换行为"),
        (r"现金支付|使用现金|收银台.{0,8}(钱|款)", "现场涉及现金支付或现金去向核实"),
        (r"未介入|介入不及时|没有介入|客服.{0,10}(错误|未|没有|直接叫)|未通知|未提醒", "值守客服被主张存在未介入、未提醒或错误引导"),
        (r"退款|退回|款项.{0,8}(去向|未到)|付款失败|支付失败", "订单发生退款、支付失败或资金去向争议"),
        (r"停电|断电|门锁|门禁|网络|断网|设备", "现场设备、网络、供电或门禁出现异常"),
        (r"调取|导出|发送.{0,8}(视频|回放)|监控视频|回放", "商家申请查看或导出监控证据，描述中尚无明确异常动作"),
        (r"补充商品|填写|定损|补数|补材料|追讨|手机号|解除拉黑", "该工单主要是资料补充、追讨或流程协作事项"),
    ]
    for pattern, label in rules:
        if re.search(pattern, combined, re.I):
            return f"待回放核实：{label}"
    return "待回放核实：工单描述未说明可确认的顾客或现场动作"


def classify_root_layer(
    description: str, behavior: str, abnormal_type: str, abnormal_state: str, check_result: str
) -> tuple[str, str]:
    evidence_text = f"{behavior} {abnormal_type}"
    process_pattern = r"调取|导出|发送.{0,8}(视频|回放)|补充商品|填写|定损|补数|补材料|追讨|手机号|解除拉黑|黑名单"
    if abnormal_state == "无异常":
        return "待核实", "回放未发现可确认异常，原主张的根因不成立"
    if not clean(check_result) and abnormal_state not in {"有异常"}:
        if re.search(process_pattern, description):
            return "流程协作", "资料、视频、定损或追讨流程协作"
        return "待核实", "缺少回放事实，暂不能定位一级根因"
    combined = evidence_text if clean(check_result) else f"{description} {behavior} {abnormal_type}"
    if abnormal_state == "未稽查" and re.search(process_pattern, description):
        return "流程协作", "资料、视频、定损或追讨流程协作"
    if re.search(r"未成年.{0,8}(烟|香烟)|香烟.{0,8}放回.{0,16}退款|放回.{0,16}退款", combined):
        return "流程协作", "未成年人购烟拦截、商品放回及退款流程"
    if "少付/漏付" in abnormal_type:
        return "顾客行为", "顾客拿取、支付或商品数量存在异常"
    if re.search(r"未介入|介入不及时|客服(?:未|没有|错误|直接).{0,10}(介入|提醒|通知|引导|处理)?|未通知|未提醒", combined):
        return "值守服务", "值守客服未介入、未提醒或引导不准确"
    if re.search(r"机位卡顿|画面卡顿|时间跳跃|监控.{0,8}(黑屏|缺失)|系统|网络|设备|二维码|小程序", combined):
        return "平台系统", "监控、网络、设备或系统链路异常"
    if re.search(r"退款|支付失败|付款失败|重复扣|多付", combined):
        return "平台系统", "支付、退款、网络或系统链路异常"
    if re.search(r"商家.{0,12}(操作错|填错|误操作)|店主.{0,12}(操作错|填错)|商品库.{0,8}(错误|缺失)|价格.{0,8}(错误|不一致)", combined):
        return "商家操作", "商家操作或商品信息维护问题"
    if re.search(r"未付款|少付|漏付|偷|拿取|带走|装入口袋|损坏|摔|调换|拿错", combined) or "少付/漏付" in abnormal_type:
        return "顾客行为", "顾客拿取、支付、藏匿、损坏或调换行为异常"
    if re.search(process_pattern, description):
        return "流程协作", "资料、视频、定损或追讨流程协作"
    return "待核实", "现有描述不足以定位一级根因"


def specific_root_cause(layer: str, behavior: str, fallback: str) -> str:
    if layer == "顾客行为":
        rules = [
            (r"装入口袋.{0,18}(又拿出|拿出).{0,18}(换|付款)", "顾客曾将商品装入口袋后取出更换并付款，是否成损待核实"),
            (r"购买.{0,24}实际带走|拿.{0,18}(只|仅).{0,10}(付|扫)|数量.{0,12}(不一致|少付)", "顾客拿取数量与支付数量不一致"),
            (r"未付款|没付款|未支付|少付|漏付|少扫|漏扫", "顾客拿取商品后未支付或少支付"),
            (r"装入口袋|藏|偷拿|偷盗|顺走", "顾客存在藏匿或疑似直接带走商品动作"),
            (r"摔破|打碎|损坏|破损", "顾客行为造成或疑似造成商品损坏"),
            (r"调换|换了|拿错", "顾客发生商品调换或拿错行为"),
        ]
        for pattern, label in rules:
            if re.search(pattern, behavior, re.I):
                return label
    return fallback


def assess_loss_status(abnormal_state: str, check_result: str, loss_summary: str) -> str:
    loss_text = clean(loss_summary)
    result_text = clean(check_result)
    if "已定损" in loss_text or re.search(r"(?:¥|￥|金额|合计).{0,8}\d", loss_text):
        return "损失已定损"
    if re.search(r"放回.{0,18}退款|退款.{0,18}放回", result_text):
        return "商品已放回并退款，未见实际损失"
    if abnormal_state == "无异常":
        return "未发现可确认损失"
    if abnormal_state == "有异常" and re.search(r"又拿出|放回|归还|换了一个付款|换一个付款|辛苦.{0,8}核实", result_text):
        return "异常动作已记录，是否成损待商家确认"
    if abnormal_state == "有异常":
        return "异常已确认，损失待定损"
    return "损失情况待核实"


def evidence_grade(evidence: dict[str, Any] | None, record: dict[str, Any]) -> tuple[str, int, str]:
    if evidence and evidence.get("detail_status") != "ok":
        return "C·子链读取失败", 25, "工单详情接口返回异常，暂未取得回放文字和稽查状态"
    if evidence and evidence.get("detail_status") == "ok" and evidence.get("check_result"):
        state = evidence.get("abnormal_state", "")
        conclusion = extract_observed_action(evidence["check_result"])
        confidence = 90 if len(conclusion) >= 12 else 65
        return "A·有文字结论", confidence, f"稽查回放结论：{conclusion}（{state}）"
    if evidence and evidence.get("detail_status") == "ok" and evidence.get("check_count", 0):
        return "B·有稽查状态", 70, f"已读取稽查子链，状态为{evidence.get('abnormal_state') or '待确认'}，但无文字结论"
    if "已稽查" in record.get("inspect_status", ""):
        return "B·有流程结果", 60, f"列表显示{record.get('inspect_status')}；尚未取得稽查文字结论"
    return "C·仅商家主张", 30, "当前只有工单描述，尚无稽查回放结论"


def build_root_cause(record: dict[str, Any], evidence: dict[str, Any] | None) -> dict[str, Any]:
    evidence = evidence or {}
    abnormal_state = evidence.get("abnormal_state") or (
        "有异常" if "有异常" in record.get("pipeline_text", "") else "无异常" if "无异常" in record.get("pipeline_text", "") else "未稽查"
    )
    abnormal_type = evidence.get("abnormal_type") or ""
    check_result = evidence.get("check_result") or ""
    behavior = detect_behavior(record["description"], check_result)
    layer, primary_cause = classify_root_layer(
        record["description"], behavior, abnormal_type, abnormal_state, check_result
    )
    primary_cause = specific_root_cause(layer, behavior, primary_cause)
    grade, confidence, evidence_summary = evidence_grade(evidence, record)
    loss_summary = evidence.get("loss_summary") or ""
    loss_status = assess_loss_status(abnormal_state, check_result, loss_summary)
    ambiguous_written_result = bool(check_result and len(extract_observed_action(check_result)) < 12)

    if abnormal_state == "无异常":
        conclusion = "回放暂未发现可确认异常"
        responsibility = "暂无责任结论；如商家不认可，需补充新时间点或新证据复核"
        status = "已核实无异常"
        confidence = max(confidence, 75)
    elif abnormal_state == "有异常":
        conclusion = (
            f"稽查标记{abnormal_type or '有异常'}，但具体现场动作未写清"
            if ambiguous_written_result
            else primary_cause
        )
        status = "已确认异常"
        if layer == "流程协作":
            status = "流程事项"
            responsibility = "已完成拦截、商品放回或退款等流程，未见需归责的新增损失事件"
        elif ambiguous_written_result:
            responsibility = "异常状态已标记，但具体行为和责任依据含混，需补充完整稽查结论"
        elif "是否成损待商家确认" in loss_status:
            responsibility = "已确认顾客侧动作，但尚不能认定已造成损失，需商家核对库存或订单"
        elif layer == "顾客行为":
            responsibility = "顾客侧为主；值守、系统是否存在次要责任需另行核验"
        elif layer == "值守服务":
            responsibility = "值守服务侧为主，需结合值守记录确认具体责任人"
        elif layer == "平台系统":
            responsibility = "平台/系统侧待技术日志进一步确认"
        elif layer == "商家操作":
            responsibility = "商家操作侧为主，需确认商品或订单配置"
        else:
            responsibility = "异常成立，但责任方向仍需补充证据"
    elif layer == "流程协作":
        conclusion = primary_cause
        responsibility = "非新增责任事件，按流程协作事项处理"
        status = "流程事项"
    else:
        conclusion = "待稽查，当前不具备根因和责任结论"
        responsibility = "待核实，暂不判责"
        status = "待核实"

    gaps: list[str] = []
    if evidence and evidence.get("detail_status") != "ok":
        gaps.append("工单详情接口读取失败，需重试")
    if not check_result:
        gaps.append("缺少稽查回放文字结论")
    elif len(extract_observed_action(check_result)) < 12:
        gaps.append("稽查文字结论过短或含混")
    if "未说明可确认" in behavior:
        gaps.append("缺少顾客具体动作")
    if len(clean(record["description"])) < 18:
        gaps.append("工单描述过短")
    if not re.search(r"\d{1,2}[:：点时]|上午|下午|凌晨|昨|今|\d{1,2}[.-]\d{1,2}", record["description"]):
        gaps.append("缺少明确发生时间")
    if abnormal_state == "有异常" and not loss_summary and "定损" not in record.get("pipeline_text", ""):
        gaps.append("异常成立但未见定损结果")

    if status == "待核实":
        next_action = "补齐时间、商品、货架位置及支付订单后发起回放稽查"
    elif status == "已核实无异常":
        next_action = "向商家反馈回放依据；存在新证据时按新时间段复核"
    elif status == "流程事项":
        next_action = "按视频、资料补充、定损或追讨流程完成闭环"
    elif ambiguous_written_result:
        next_action = "补写顾客具体拿取、支付或放回动作，再进入定损和责任确认"
    elif "是否成损待商家确认" in loss_status:
        next_action = "请商家核对对应商品库存与支付订单，确认是否实际形成损失后再定责"
    elif abnormal_state == "有异常" and ("待补充" in record.get("pipeline_text", "") or "未见定损" in "；".join(gaps)):
        next_action = "推动商家补充商品和价格，进入定损及追讨流程"
    else:
        next_action = "依据稽查结论完成定损、追讨或结案，并单独复核次要责任"

    return {
        "ticket_id": record["ticket_id"],
        "shop_id": record["shop_id"],
        "brand": record["brand"] or "非KA/未匹配",
        "is_ka": bool(record["is_ka"]),
        "category": record["category"],
        "merchant_claim": record["description"],
        "customer_behavior": behavior,
        "verification_status": status,
        "abnormal_state": abnormal_state,
        "abnormal_type": abnormal_type or "—",
        "root_layer": layer,
        "root_cause": conclusion,
        "responsibility": responsibility,
        "evidence_grade": grade,
        "confidence": confidence,
        "evidence_summary": evidence_summary,
        "evidence_gap": gaps or ["现有证据链完整"],
        "next_action": next_action,
        "loss_status": loss_status,
        "loss_summary": loss_summary or "暂无定损/赔付明细",
        "pipeline_status": record.get("pipeline_text") or "—",
        "status": record["status"],
        "handler": record["handler"],
        "created_at": record["created_at"],
        "source_url": f"{SOURCE_BASE}events/detail?event_id={record['ticket_id']}",
    }


def build_root_cause_ledger(selected: date) -> dict[str, Any]:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT t.*, e.check_count, e.abnormal_state AS ev_abnormal_state,
               e.abnormal_type AS ev_abnormal_type, e.check_result, e.check_meta,
               e.loss_summary AS ev_loss_summary, e.detail_status, e.fetched_at AS evidence_fetched_at
        FROM tickets t LEFT JOIN ticket_evidence e ON e.ticket_id=t.ticket_id
        WHERE t.created_date=? ORDER BY t.created_at DESC
        """,
        (selected.isoformat(),),
    ).fetchall()
    conn.close()
    records = []
    evidence_ready = 0
    for row in rows:
        raw = dict(row)
        evidence = None
        if raw.get("detail_status"):
            evidence = {
                "check_count": raw.get("check_count") or 0,
                "abnormal_state": raw.get("ev_abnormal_state") or "",
                "abnormal_type": raw.get("ev_abnormal_type") or "",
                "check_result": raw.get("check_result") or "",
                "check_meta": raw.get("check_meta") or "",
                "loss_summary": raw.get("ev_loss_summary") or "",
                "detail_status": raw.get("detail_status"),
            }
            evidence_ready += int(raw.get("detail_status") == "ok")
        records.append(build_root_cause(raw, evidence))
    layer_counts = Counter(record["root_layer"] for record in records)
    status_counts = Counter(record["verification_status"] for record in records)
    return {
        "date": selected.isoformat(),
        "total": len(records),
        "evidence_ready": evidence_ready,
        "evidence_ratio": safe_pct(evidence_ready, len(records)),
        "verified": sum(1 for record in records if record["verification_status"] in {"已确认异常", "已核实无异常"}),
        "unresolved": sum(1 for record in records if record["verification_status"] == "待核实"),
        "layers": [{"name": key, "count": value} for key, value in layer_counts.most_common()],
        "statuses": [{"name": key, "count": value} for key, value in status_counts.most_common()],
        "records": records,
    }


def detect_risk_hits(description: str) -> list[dict[str, str]]:
    """仅在完整高风险语义命中时返回，避免用“烟/现金/学生”等单字泛化。"""
    text = clean(description)
    hits: list[dict[str, str]] = []
    for rule in RISK_RULES:
        matched_terms: list[str] = []
        for pattern in rule["patterns"]:
            matched_terms.extend(match.group(0) for match in re.finditer(pattern, text, re.I))
        if matched_terms:
            hits.append(
                {
                    "label": rule["label"],
                    "level": rule["level"],
                    "matched_text": "、".join(dict.fromkeys(clean(term) for term in matched_terms if clean(term))),
                }
            )
    return hits


def merge_risk_hits(sources: list[tuple[str, str]]) -> list[dict[str, str]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for source_name, text in sources:
        for hit in detect_risk_hits(text):
            key = (hit["label"], hit["level"])
            item = merged.setdefault(key, {"label": hit["label"], "level": hit["level"], "terms": [], "sources": []})
            item["terms"].extend(hit["matched_text"].split("、"))
            item["sources"].append(source_name)
    return [
        {
            "label": item["label"],
            "level": item["level"],
            "matched_text": "、".join(dict.fromkeys(term for term in item["terms"] if term)),
            "source": "、".join(dict.fromkeys(item["sources"])),
        }
        for item in merged.values()
    ]


def record_anomaly_reasons(
    record: dict[str, Any], selected: date, risk_hits: list[dict[str, str]] | None = None
) -> list[str]:
    reasons: list[str] = []
    if not record["shop_id"] or record["shop_id"] in {"-", "—"}:
        reasons.append("缺少店铺ID")
    if not record["description"] or len(record["description"]) < 5:
        reasons.append("问题描述缺失或过短")
    if not record["handler"] or "未分配" in record["handler"]:
        reasons.append("未分配处理人")
    duration = record.get("duration_minutes")
    if record["status"] in {"待领取", "待接单"} and duration is not None and duration >= 120:
        reasons.append("超过2小时仍待领取")
    if record["inspect_status"] == "未稽查" and duration is not None and duration >= 1440:
        reasons.append("超过24小时仍未稽查")
    if record["response_minutes"] is not None and record["response_minutes"] >= 1440:
        reasons.append("稽查响应超过24小时")
    for risk in risk_hits if risk_hits is not None else detect_risk_hits(record["description"]):
        reasons.append(f"{risk['level']}风险：{risk['label']}")
    return reasons


def build_dashboard(selected: date, window: int) -> dict[str, Any]:
    conn = get_db()
    start_day = selected - timedelta(days=max(window, 8) - 1)
    records = rows_for_range(conn, start_day, selected)
    merchant_records = merchant_rows_for_range(conn, start_day, selected)
    merchant_pending = [dict(row) for row in conn.execute("SELECT * FROM merchant_inspections WHERE is_open=1")]
    completion_start = selected - timedelta(days=29)
    completion_previous_start = selected - timedelta(days=59)
    completion_current_rows = completion_rows_for_range(conn, completion_start, selected)
    completion_previous_rows = completion_rows_for_range(
        conn, completion_previous_start, completion_start - timedelta(days=1)
    )
    merchant_sync = dict(conn.execute("SELECT state_key,state_value FROM merchant_sync_state").fetchall())
    last_sync = conn.execute(
        "SELECT finished_at,message FROM sync_runs WHERE status='ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    evidence_by_ticket = {
        row["ticket_id"]: dict(row)
        for row in conn.execute(
            """
            SELECT t.ticket_id, COALESCE(e.check_result,'') AS check_result,
                   COALESCE(e.abnormal_type,'') AS abnormal_type
            FROM tickets t LEFT JOIN ticket_evidence e ON e.ticket_id=t.ticket_id
            WHERE t.created_date=?
            """,
            (selected.isoformat(),),
        ).fetchall()
    }
    conn.close()

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["created_date"]].append(record)
    merchant_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in merchant_records:
        merchant_grouped[record["created_date"]].append(record)

    # 当天尚未结束时，环比和近7日均值统一截取到当前时刻，避免用“今日半天”
    # 对比“昨日全天”造成虚假下降。历史日期则按完整自然日分析。
    cutoff_time = datetime.now().strftime("%H:%M") if selected == date.today() else None

    def comparable(day_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not cutoff_time:
            return day_records
        return [r for r in day_records if len(r["created_at"]) >= 16 and r["created_at"][11:16] <= cutoff_time]

    if cutoff_time:
        completion_current_rows = comparable(completion_current_rows)
        completion_previous_rows = comparable(completion_previous_rows)

    selected_key = selected.isoformat()
    previous_key = (selected - timedelta(days=1)).isoformat()
    current = comparable(grouped.get(selected_key, []))
    previous = comparable(grouped.get(previous_key, []))
    merchant_current = comparable(merchant_grouped.get(selected_key, []))
    merchant_previous = comparable(merchant_grouped.get(previous_key, []))

    trend = []
    cursor = start_day
    while cursor <= selected:
        day_records = comparable(grouped.get(cursor.isoformat(), []))
        app_records = comparable(merchant_grouped.get(cursor.isoformat(), []))
        counts = Counter(r["category"] for r in day_records)
        trend.append(
            {
                "date": cursor.isoformat(),
                "label": cursor.strftime("%m-%d"),
                "total": len(day_records) + len(app_records),
                "tianrun_count": len(day_records),
                "merchant_app_count": len(app_records),
                "categories": {cat: counts.get(cat, 0) for cat in CATEGORY_ORDER},
                "ka_ratio": safe_pct(sum(r["is_ka"] for r in day_records), len(day_records)),
            }
        )
        cursor += timedelta(days=1)

    current_cat = Counter(r["category"] for r in current)
    previous_cat = Counter(r["category"] for r in previous)
    prior_7_keys = [(selected - timedelta(days=i)).isoformat() for i in range(1, 8)]
    category_change = []
    net_change = len(current) - len(previous)
    growth_anomalies = []
    for category in CATEGORY_ORDER:
        history = [sum(1 for r in comparable(grouped.get(key, [])) if r["category"] == category) for key in prior_7_keys]
        avg7 = round(sum(history) / len(history), 1) if history else 0
        std7 = statistics.pstdev(history) if len(history) > 1 else 0
        now_count = current_cat.get(category, 0)
        prev_count = previous_cat.get(category, 0)
        delta = now_count - prev_count
        contribution = safe_pct(delta, net_change) if net_change > 0 and delta > 0 else 0
        zscore = round((now_count - avg7) / std7, 1) if std7 else 0
        category_change.append(
            {
                "category": category,
                "current": now_count,
                "previous": prev_count,
                "delta": delta,
                "pct": round(delta * 100 / prev_count, 1) if prev_count else (100.0 if now_count else 0.0),
                "avg7": avg7,
                "vs_avg7": round(now_count - avg7, 1),
                "contribution": contribution,
            }
        )
        if now_count >= avg7 + max(5, 2 * std7) and now_count >= 10:
            growth_anomalies.append(
                {
                    "category": category,
                    "current": now_count,
                    "avg7": avg7,
                    "delta_vs_avg": round(now_count - avg7, 1),
                    "zscore": zscore,
                    "reason": "明显高于近7日常态",
                }
            )
    category_change.sort(key=lambda item: item["delta"], reverse=True)
    category_insights = build_category_insights(current, previous, evidence_by_ticket, selected)
    completion = build_completion_stats(completion_current_rows)
    completion_previous = build_completion_stats(completion_previous_rows)
    completion["period_start"] = completion_start.isoformat()
    completion["period_end"] = selected_key
    completion["previous_period_start"] = completion_previous_start.isoformat()
    completion["previous_period_end"] = (completion_start - timedelta(days=1)).isoformat()
    completion["previous"] = completion_previous
    completion["rate_24h_delta"] = round(completion["rate_24h"] - completion_previous["rate_24h"], 1)

    anomaly_records = []
    risk_records = []
    anomaly_counts: Counter[str] = Counter()
    operational_anomaly_counts: Counter[str] = Counter()
    seen_signatures: Counter[tuple[str, str]] = Counter((r["shop_id"], r["description"]) for r in current if r["description"])
    for record in current:
        evidence_text = evidence_by_ticket.get(record["ticket_id"], {})
        risk_hits = merge_risk_hits(
            [
                ("工单描述", record["description"]),
                ("稽查异常类型", evidence_text.get("abnormal_type", "")),
            ]
        )
        reasons = record_anomaly_reasons(record, selected, risk_hits)
        if seen_signatures[(record["shop_id"], record["description"])] > 1:
            reasons.append("同店同描述疑似重复工单")
        if reasons:
            for reason in reasons:
                anomaly_counts[reason] += 1
                if "风险：" not in reason:
                    operational_anomaly_counts[reason] += 1
            anomaly_records.append(
                {
                    "ticket_id": record["ticket_id"],
                    "shop_id": record["shop_id"],
                    "brand": record["brand"] or "非KA/未匹配",
                    "category": record["category"],
                    "description": record["description"],
                    "status": record["status"],
                    "handler": record["handler"],
                    "created_at": record["created_at"],
                    "reasons": reasons,
                }
            )
        if risk_hits:
            risk_records.append(
                {
                    "ticket_id": record["ticket_id"],
                    "shop_id": record["shop_id"],
                    "brand": record["brand"] or "非KA/未匹配",
                    "category": record["category"],
                    "description": record["description"],
                    "status": record["status"],
                    "handler": record["handler"],
                    "created_at": record["created_at"],
                    "risk_level": min(
                        (hit["level"] for hit in risk_hits), key=lambda level: RISK_LEVEL_ORDER.get(level, 99)
                    ),
                    "risk_hits": risk_hits,
                    "source_url": f"{SOURCE_BASE}events/detail?event_id={record['ticket_id']}",
                }
            )
    anomaly_records.sort(key=lambda item: (len(item["reasons"]), item["created_at"]), reverse=True)
    risk_records.sort(key=lambda item: item["created_at"], reverse=True)
    risk_records.sort(key=lambda item: RISK_LEVEL_ORDER.get(item["risk_level"], 99))
    risk_counts = Counter(hit["label"] for record in risk_records for hit in record["risk_hits"])

    topic_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in current:
        topic_groups[classify_topic(record)].append(record)
    topics = []
    for topic, topic_records in sorted(topic_groups.items(), key=lambda item: len(item[1]), reverse=True):
        topics.append(
            {
                "topic": topic,
                "count": len(topic_records),
                "ratio": safe_pct(len(topic_records), len(current)),
                "samples": [
                    {
                        "ticket_id": r["ticket_id"],
                        "brand": r["brand"] or "非KA/未匹配",
                        "description": r["description"],
                    }
                    for r in topic_records[:4]
                ],
            }
        )

    ka_records = [r for r in current if r["is_ka"]]
    previous_ka = [r for r in previous if r["is_ka"]]
    ka_category_current = Counter(r["category"] for r in ka_records)
    ka_category_previous = Counter(r["category"] for r in previous_ka)
    ka_category_breakdown = []
    for category in CORE_CATEGORY_ORDER:
        current_count = ka_category_current.get(category, 0)
        previous_count = ka_category_previous.get(category, 0)
        ka_category_breakdown.append(
            {
                "category": category,
                "current": current_count,
                "previous": previous_count,
                "delta": current_count - previous_count,
                "pct": round((current_count - previous_count) * 100 / previous_count, 1)
                if previous_count
                else (100.0 if current_count else 0.0),
            }
        )

    ka_brands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in ka_records:
        ka_brands[record["brand"] or "未识别品牌"].append(record)

    brand_details = []
    for brand, brand_records in ka_brands.items():
        category_counts = Counter(r["category"] for r in brand_records)
        tickets = [
            {
                "ticket_id": r["ticket_id"],
                "shop_id": r["shop_id"],
                "category": r["category"],
                "description": r["description"],
                "status": r["status"],
                "handler": r["handler"],
                "created_at": r["created_at"],
                "source_url": f"{SOURCE_BASE}events/detail?event_id={r['ticket_id']}",
            }
            for r in sorted(brand_records, key=lambda row: row["created_at"], reverse=True)
        ]
        brand_details.append(
            {
                "brand": brand,
                "count": len(brand_records),
                "categories": {category: category_counts.get(category, 0) for category in CORE_CATEGORY_ORDER},
                "other_count": sum(count for category, count in category_counts.items() if category not in CORE_CATEGORY_ORDER),
                "tickets": tickets,
            }
        )
    brand_details.sort(key=lambda item: (-item["count"], item["brand"]))

    ka = {
        "count": len(ka_records),
        "ratio": safe_pct(len(ka_records), len(current)),
        "previous_count": len(previous_ka),
        "previous_ratio": safe_pct(len(previous_ka), len(previous)),
        "ratio_delta": round(safe_pct(len(ka_records), len(current)) - safe_pct(len(previous_ka), len(previous)), 1),
        "category_breakdown": ka_category_breakdown,
        "brand_details": brand_details,
        "definition": "店铺ID能够在 Forge 的 KA 店铺映射中匹配到品牌，即列表中的品牌字段非空。",
    }

    now = datetime.now(BEIJING_TZ)
    app_rows = [merchant_record_payload(row, now) for row in merchant_current]
    app_pending_rows = [merchant_record_payload(row, now) for row in merchant_pending]
    app_pending_rows.sort(key=lambda row: row["created_at"])
    overdue_rows = [row for row in app_pending_rows if row["overdue_24h"]]
    type_counts = Counter(row["abnormal_type"] for row in app_rows)
    previous_type_counts = Counter(row["abnormal_type"] or "未分类" for row in merchant_previous)
    merchant_app = {
        "count": len(app_rows),
        "previous": len(merchant_previous),
        "delta": len(app_rows) - len(merchant_previous),
        "pct": round((len(app_rows) - len(merchant_previous)) * 100 / len(merchant_previous), 1)
        if merchant_previous else (100.0 if app_rows else 0.0),
        "loaded": "day:" + selected_key in merchant_sync,
        "previous_loaded": "day:" + previous_key in merchant_sync,
        "day_synced_at": merchant_sync.get("day:" + selected_key),
        "open_synced_at": merchant_sync.get("open_synced_at"),
        "as_of": now.isoformat(timespec="seconds"),
        "pending_count": sum(row["is_open"] for row in app_rows),
        "abnormal_count": sum(row["abnormal_state"] == "有异常" for row in app_rows),
        "normal_count": sum(row["abnormal_state"] == "无异常" for row in app_rows),
        "ka_count": sum(row["is_ka"] for row in app_rows),
        "current_pending_count": len(app_pending_rows),
        "overdue_24h_count": len(overdue_rows),
        "overdue_24h_records": overdue_rows,
        "pending_records": app_pending_rows,
        "risk_records": [row for row in app_rows if row["risk_hits"]],
        "type_breakdown": [{"name": name, "count": count, "previous": previous_type_counts.get(name, 0),
                            "delta": count - previous_type_counts.get(name, 0)}
                           for name, count in type_counts.most_common()],
        "records": sorted(app_rows, key=lambda row: (not row["is_open"], row["created_at"])),
        "definition": "APP发起稽查单单独计数；与天润工单相加为入口新增量，同一事件跨入口反馈不去重。当前超时存量不受分析日期限制。",
    }
    current_total = len(current) + len(merchant_current)
    previous_total = len(previous) + len(merchant_previous)
    prior_counts = [len(comparable(grouped.get(key, []))) + len(comparable(merchant_grouped.get(key, []))) for key in prior_7_keys]
    avg7_total = round(sum(prior_counts) / len(prior_counts), 1) if prior_counts else 0
    top_driver = category_change[0] if category_change else None
    return {
        "selected_date": selected_key,
        "cutoff_time": cutoff_time,
        "window": window,
        "source": SOURCE_BASE,
        "last_sync": dict(last_sync) if last_sync else None,
        "summary": {
            "total": current_total,
            "tianrun_count": len(current),
            "merchant_app_count": len(merchant_current),
            "tianrun_previous": len(previous),
            "merchant_app_previous": len(merchant_previous),
            "tianrun_delta": len(current) - len(previous),
            "previous": previous_total,
            "delta": current_total - previous_total,
            "pct": round((current_total - previous_total) * 100 / previous_total, 1) if previous_total else 0,
            "avg7": avg7_total,
            "vs_avg7": round(current_total - avg7_total, 1),
            "top_driver": top_driver,
            "growth_anomaly_count": len(growth_anomalies),
            "record_anomaly_count": len(anomaly_records),
            "risk_ticket_count": len(risk_records),
        },
        "daily_trend": trend[-window:],
        "category_change": category_change,
        "category_insights": category_insights,
        "completion_rate": completion,
        "growth_anomalies": growth_anomalies,
        "anomaly_summary": [{"reason": key, "count": count} for key, count in anomaly_counts.most_common()],
        "operational_anomaly_summary": [
            {"reason": key, "count": count} for key, count in operational_anomaly_counts.most_common()
        ],
        "anomaly_records": anomaly_records[:100],
        "risk_summary": [{"risk": key, "count": count} for key, count in risk_counts.most_common()],
        "risk_records": risk_records,
        "topics": topics,
        "ka": ka,
        "merchant_app": merchant_app,
    }


def load_employee_dashboard_data() -> dict[str, Any]:
    if not EMPLOYEE_DASHBOARD_DATA.exists():
        return {}
    try:
        payload = json.loads(EMPLOYEE_DASHBOARD_DATA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def employee_service_request(method: str, path: str) -> requests.Response:
    session = requests.Session()
    session.trust_env = False
    return session.request(method, EMPLOYEE_DASHBOARD_SERVICE + path, timeout=(2, 8))


def report_run_directory(report_date: str, slot: str) -> Path | None:
    """Resolve only the two formal daily-report slots; verification runs stay private."""
    if slot not in REPORT_SLOTS:
        return None
    try:
        parsed = date.fromisoformat(report_date)
    except ValueError:
        return None
    if parsed.isoformat() != report_date:
        return None
    run_dir = REPORT_ARCHIVE_ROOT / report_date / slot
    try:
        if not run_dir.resolve().is_relative_to(REPORT_ARCHIVE_ROOT.resolve()):
            return None
    except (OSError, RuntimeError):
        return None
    return run_dir


def report_file(run_dir: Path, filename: str) -> Path | None:
    """Only expose regular files immediately inside the selected report directory."""
    target = run_dir / filename
    try:
        resolved = target.resolve()
        if resolved != run_dir.resolve() / filename or not target.is_file():
            return None
    except (OSError, RuntimeError):
        return None
    return target


def read_report_metadata(run_dir: Path) -> dict[str, Any]:
    metadata_path = report_file(run_dir, "run-metadata.json")
    if metadata_path is None:
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    # The generator writes this file last, after the report and source data.
    return payload if isinstance(payload, dict) and payload.get("finished_at") else {}


def report_screenshots(metadata: dict[str, Any]) -> set[str]:
    screenshots = metadata.get("screenshots")
    if not isinstance(screenshots, dict):
        return set()
    return {
        filename for filename in screenshots.values()
        if isinstance(filename, str) and re.fullmatch(r"[a-zA-Z0-9_-]+\.png", filename)
    }


def completed_report(report_date: str, slot: str) -> tuple[Path, dict[str, Any]] | None:
    run_dir = report_run_directory(report_date, slot)
    if run_dir is None:
        return None
    metadata = read_report_metadata(run_dir)
    expected_time = f"{slot[:2]}:{slot[2:]}"
    if not metadata or metadata.get("planned_at") != f"{report_date} {expected_time}":
        return None
    if any(report_file(run_dir, filename) is None for filename in ("analysis.html", "analysis.md", "analysis-data.json")):
        return None
    return run_dir, metadata


def list_daily_reports() -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    try:
        day_directories = list(REPORT_ARCHIVE_ROOT.iterdir())
    except OSError:
        return reports
    for day_dir in day_directories:
        if not day_dir.is_dir():
            continue
        try:
            parsed_day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if parsed_day.isoformat() != day_dir.name:
            continue
        for slot, slot_label in REPORT_SLOTS.items():
            completed = completed_report(day_dir.name, slot)
            if completed is None:
                continue
            run_dir, metadata = completed
            errors = metadata.get("errors") if isinstance(metadata.get("errors"), list) else []
            screenshots = report_screenshots(metadata)
            screenshot_count = sum(
                1 for filename in screenshots if report_file(run_dir, filename) is not None
            )
            refresh_ok = bool(metadata.get("merchant_refresh_ok")) and bool(metadata.get("employee_refresh_ok"))
            status = "success" if refresh_ok and not errors and screenshot_count == len(screenshots) else "partial"
            try:
                revision = "-".join(
                    str((run_dir / filename).stat().st_mtime_ns)
                    for filename in ("run-metadata.json", "analysis.html")
                )
            except OSError:
                continue
            reports.append(
                {
                    "id": f"{day_dir.name}-{slot}",
                    "date": day_dir.name,
                    "slot": slot,
                    "slot_label": slot_label,
                    "planned_at": metadata.get("planned_at") or f"{day_dir.name} {slot_label[:5]}",
                    "started_at": metadata.get("started_at") or "",
                    "finished_at": metadata.get("finished_at") or "",
                    "status": status,
                    "status_label": "生成成功" if status == "success" else "部分完成",
                    "error_count": len(errors),
                    "screenshot_count": screenshot_count,
                    "revision": revision,
                    "report_url": f"/reports/{day_dir.name}/{slot}/",
                    "embed_url": f"/reports/{day_dir.name}/{slot}/?embed=1",
                    "markdown_url": f"/reports/{day_dir.name}/{slot}/analysis.md?download=1",
                    "data_url": f"/reports/{day_dir.name}/{slot}/analysis-data.json?download=1",
                }
            )
    reports.sort(key=lambda item: (item["date"], item["slot"]), reverse=True)
    return reports


@app.get("/")
def workspace():
    return render_template("workspace.html")


@app.get("/daily")
def index():
    return render_template(
        "index.html",
        today=latest_ticket_date(),
        embedded=request.args.get("embed") == "1",
    )


@app.get("/employee-dashboard")
def employee_dashboard_page():
    if not EMPLOYEE_DASHBOARD_HTML.exists():
        return Response("员工工单卡点看板正在首次生成，请稍后刷新。", status=503, content_type="text/plain; charset=utf-8")
    try:
        html = EMPLOYEE_DASHBOARD_HTML.read_text(encoding="utf-8")
    except OSError:
        return Response("员工工单卡点看板暂时无法读取。", status=503, content_type="text/plain; charset=utf-8")

    html = html.replace("fetch('/api/status", "fetch('/api/employee/status")
    html = html.replace("fetch('/api/refresh", "fetch('/api/employee/refresh")
    if request.args.get("embed") == "1":
        embedded_style = """
        <style id="workspace-embedded-style">
          body { background: #f6f7f5 !important; }
          .shell { max-width: none !important; padding: 22px !important; }
          .header { display: none !important; }
          .toolbar { top: 0 !important; border-radius: 10px !important; }
          @media (max-width: 700px) { .shell { padding: 12px !important; } }
        </style>
        """
        html = html.replace("</head>", embedded_style + "</head>", 1)
    return Response(html, content_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/api/reports")
def api_daily_reports():
    reports = list_daily_reports()
    response = jsonify(
        {
            "ok": True,
            "reports": reports,
            "summary": {
                "report_count": len(reports),
                "day_count": len({item["date"] for item in reports}),
                "latest": reports[0] if reports else None,
                "schedule": ["18:00", "23:59"],
            },
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/reports/<report_date>/<slot>/")
def daily_report_document(report_date: str, slot: str):
    completed = completed_report(report_date, slot)
    if completed is None:
        return Response("日报不存在或尚未生成。", status=404, content_type="text/plain; charset=utf-8")
    run_dir, _metadata = completed
    report_path = run_dir / "analysis.html"
    if request.args.get("embed") == "1":
        try:
            report_html = report_path.read_text(encoding="utf-8")
        except OSError:
            return Response("日报暂时无法读取。", status=503, content_type="text/plain; charset=utf-8")
        embedded_style = """
        <style id="workspace-report-embedded-style">
          body { background: #fff !important; }
          main { max-width: 1180px !important; margin: 0 auto !important; padding: 30px 38px 54px !important; box-shadow: none !important; border-radius: 0 !important; }
          @media (max-width: 700px) { main { padding: 20px 16px 40px !important; } }
        </style>
        """
        report_html = report_html.replace("</head>", embedded_style + "</head>", 1)
        response = Response(report_html, content_type="text/html; charset=utf-8")
    else:
        response = send_file(report_path, mimetype="text/html", max_age=0)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
    return response


@app.get("/reports/<report_date>/<slot>/<filename>")
def daily_report_asset(report_date: str, slot: str, filename: str):
    completed = completed_report(report_date, slot)
    if completed is None:
        return Response("文件不存在。", status=404, content_type="text/plain; charset=utf-8")
    run_dir, metadata = completed
    allowed_files = REPORT_DOWNLOAD_FILES | report_screenshots(metadata)
    if filename not in allowed_files:
        return Response("文件不存在。", status=404, content_type="text/plain; charset=utf-8")
    target = report_file(run_dir, filename)
    if target is None:
        return Response("文件不存在。", status=404, content_type="text/plain; charset=utf-8")
    response = send_file(
        target,
        as_attachment=request.args.get("download") == "1",
        download_name=f"{report_date}-{slot}-{filename}",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/api/workspace/overview")
def api_workspace_overview():
    selected_date = latest_ticket_date()
    daily = build_dashboard(date.fromisoformat(selected_date), 14)
    last_sync = daily.get("last_sync")
    if isinstance(last_sync, dict):
        daily_generated_at = last_sync.get("finished_at") or last_sync.get("started_at") or ""
    else:
        daily_generated_at = last_sync or daily.get("cutoff_time") or ""
    employee = load_employee_dashboard_data()
    employee_available = bool(employee)
    totals = employee.get("totals") if isinstance(employee.get("totals"), dict) else {}
    employees = employee.get("employees") if isinstance(employee.get("employees"), list) else []
    busiest = max(
        (item for item in employees if isinstance(item, dict)),
        key=lambda item: int(item.get("total") or 0),
        default={},
    )
    completed_daily = totals.get("completed_daily") if isinstance(totals.get("completed_daily"), dict) else {}
    created_daily = totals.get("created_daily") if isinstance(totals.get("created_daily"), dict) else {}
    flow_days = employee.get("flow_days") if isinstance(employee.get("flow_days"), list) else []
    flow_days = flow_days[-7:]
    summary = daily.get("summary", {})
    return jsonify(
        {
            "ok": True,
            "daily": {
                "selected_date": selected_date,
                "generated_at": daily_generated_at,
                "total": summary.get("total", 0),
                "previous": summary.get("previous", 0),
                "pct": summary.get("pct", 0),
                "risk_ticket_count": summary.get("risk_ticket_count", 0),
                "record_anomaly_count": summary.get("record_anomaly_count", 0),
                "top_driver": summary.get("top_driver") or {},
                "trend": [
                    {
                        "date": item.get("date", ""),
                        "total": item.get("total", 0),
                    }
                    for item in daily.get("daily_trend", [])[-7:]
                ],
            },
            "employee": {
                "available": employee_available,
                "generated_at": employee.get("generated_at", ""),
                "tickets": totals.get("tickets", 0) if employee_available else None,
                "employees": totals.get("employees", 0) if employee_available else None,
                "over48": totals.get("over48", 0) if employee_available else None,
                "over120": totals.get("over120", 0) if employee_available else None,
                "completed_today": completed_daily.get(selected_date, 0) if employee_available else None,
                "created_today": created_daily.get(selected_date, 0) if employee_available else None,
                "busiest": {
                    "name": busiest.get("name", "—") if employee_available else "—",
                    "total": busiest.get("total", 0) if employee_available else None,
                },
                "trend": [
                    {
                        "date": day,
                        "created": created_daily.get(day, 0),
                        "completed": completed_daily.get(day, 0),
                    }
                    for day in flow_days
                ],
            },
        }
    )


@app.get("/api/employee/status")
def api_employee_status():
    try:
        response = employee_service_request("GET", "/api/status")
        return Response(response.content, status=response.status_code, content_type=response.headers.get("Content-Type", "application/json"))
    except requests.RequestException:
        employee = load_employee_dashboard_data()
        return jsonify(
            {
                "generated_at": employee.get("generated_at", ""),
                "updating": False,
                "error": "员工看板刷新服务未连接，当前展示最近一次快照",
                "interval_seconds": 300,
            }
        )


@app.post("/api/employee/refresh")
def api_employee_refresh():
    try:
        response = employee_service_request("POST", "/api/refresh")
        return Response(response.content, status=response.status_code, content_type=response.headers.get("Content-Type", "application/json"))
    except requests.RequestException:
        return jsonify({"ok": False, "error": "员工看板刷新服务未连接"}), 503


@app.get("/dispatch")
def dispatch_page():
    denied = local_admin_only()
    if denied:
        return denied
    return render_template("dispatch.html")


@app.get("/api/dashboard")
def api_dashboard():
    selected_raw = request.args.get("date", latest_ticket_date())
    window = min(30, max(7, int(request.args.get("window", 14))))
    try:
        selected = date.fromisoformat(selected_raw)
    except ValueError:
        return jsonify({"ok": False, "error": "日期格式应为 YYYY-MM-DD"}), 400
    return jsonify(
        {
            "ok": True,
            "data": build_dashboard(selected, window),
            "sync": SYNC_STATE,
            "realtime": {**AUTO_SYNC_STATE, "latest_date": latest_ticket_date()},
        }
    )


@app.get("/api/merchant-ranking")
def api_merchant_complaint_ranking():
    return jsonify({"ok": True, "data": build_merchant_complaint_ranking()})


@app.get("/api/completion-records")
def api_completion_records():
    try:
        selected = date.fromisoformat(request.args.get("date", latest_ticket_date()))
    except ValueError:
        return jsonify({"ok": False, "error": "日期格式应为 YYYY-MM-DD"}), 400
    start_day = selected - timedelta(days=29)
    rows = build_completion_records(start_day, selected)
    return jsonify({"ok": True, "data": rows})


@app.get("/api/completion-day")
def api_completion_day():
    try:
        selected = date.fromisoformat(request.args.get("date", latest_ticket_date()))
    except ValueError:
        return jsonify({"ok": False, "error": "日期格式应为 YYYY-MM-DD"}), 400
    return jsonify({"ok": True, "data": build_completion_day_snapshot(selected)})


@app.get("/api/root-causes")
def api_root_causes():
    selected_raw = request.args.get("date", date.today().isoformat())
    try:
        selected = date.fromisoformat(selected_raw)
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(50, max(10, int(request.args.get("page_size", 25))))
    except ValueError:
        return jsonify({"ok": False, "error": "日期或分页参数格式错误"}), 400

    ledger = build_root_cause_ledger(selected)
    keyword = clean(request.args.get("q", "")).lower()
    root_layer = clean(request.args.get("layer", ""))
    verification = clean(request.args.get("status", ""))
    ka_filter = clean(request.args.get("ka", ""))
    unresolved_only = request.args.get("unresolved", "") in {"1", "true", "yes"}

    def matches(record: dict[str, Any]) -> bool:
        haystack = " ".join(
            str(record.get(key, ""))
            for key in ("ticket_id", "shop_id", "brand", "merchant_claim", "customer_behavior", "root_cause")
        ).lower()
        if keyword and keyword not in haystack:
            return False
        if root_layer and record["root_layer"] != root_layer:
            return False
        if verification and record["verification_status"] != verification:
            return False
        if ka_filter == "ka" and not record["is_ka"]:
            return False
        if ka_filter == "non-ka" and record["is_ka"]:
            return False
        if unresolved_only and record["verification_status"] != "待核实":
            return False
        return True

    filtered = [record for record in ledger.pop("records") if matches(record)]
    pages = max(1, math.ceil(len(filtered) / page_size))
    page = min(page, pages)
    offset = (page - 1) * page_size
    ledger.update(
        {
            "filtered_total": len(filtered),
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "records": filtered[offset : offset + page_size],
        }
    )
    return jsonify({"ok": True, "data": ledger, "evidence_sync": EVIDENCE_STATE})


@app.get("/api/evidence/status")
def api_evidence_status():
    return jsonify({"ok": True, "sync": EVIDENCE_STATE})


@app.post("/api/evidence/sync")
def api_evidence_sync():
    if EVIDENCE_STATE["running"]:
        return jsonify({"ok": False, "error": "已有逐单证据同步任务正在运行"}), 409
    payload = request.get_json(silent=True) or {}
    try:
        selected = date.fromisoformat(payload.get("date", date.today().isoformat()))
    except ValueError:
        return jsonify({"ok": False, "error": "日期格式应为 YYYY-MM-DD"}), 400
    force = bool(payload.get("force", False))

    def runner() -> None:
        try:
            sync_evidence_for_day(selected, force=force)
        except Exception:
            pass

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"ok": True, "message": "逐单证据同步任务已启动"})


@app.get("/api/sync/status")
def api_sync_status():
    return jsonify(
        {
            "ok": True,
            "sync": SYNC_STATE,
            "realtime": {**AUTO_SYNC_STATE, "latest_date": latest_ticket_date()},
        }
    )


@app.post("/api/sync")
def api_sync():
    if SYNC_STATE["running"]:
        return jsonify({"ok": False, "error": "已有同步任务正在运行"}), 409
    payload = request.get_json(silent=True) or {}
    days = min(30, max(2, int(payload.get("days", 14))))

    def runner() -> None:
        try:
            sync_days(days=days)
        except Exception:
            pass

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"ok": True, "message": "同步任务已启动"})


@app.get("/api/tianrun/status")
def api_tianrun_status():
    denied = local_admin_only()
    if denied:
        return denied
    summary = cookie_summary()
    if request.args.get("test") == "1" and summary["configured"]:
        try:
            users = tianrun_users("05435")
            summary.update({"connection": "ok", "message": f"连接正常，查询返回 {len(users)} 名员工"})
        except PermissionError as exc:
            summary.update({"connection": "expired", "message": str(exc)})
        except Exception as exc:
            summary.update({"connection": "error", "message": str(exc)})
    else:
        summary.update(
            {
                "connection": "unknown" if summary["configured"] else "missing",
                "message": "已保存，尚未检测" if summary["configured"] else "尚未配置 Cookie",
            }
        )
    return jsonify({"ok": True, "data": summary})


@app.put("/api/tianrun/cookie")
def api_tianrun_cookie():
    denied = local_admin_only()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    previous = read_tianrun_cookie()
    try:
        save_tianrun_cookie(str(payload.get("cookie") or ""))
        users = tianrun_users("05435")
    except (ValueError, PermissionError, RuntimeError) as exc:
        if previous:
            save_tianrun_cookie(previous)
        elif TIANRUN_COOKIE_PATH.exists():
            TIANRUN_COOKIE_PATH.unlink()
        return jsonify({"ok": False, "error": str(exc)}), 400
    summary = cookie_summary()
    summary.update({"connection": "ok", "message": f"Cookie 已保存并验证，查询返回 {len(users)} 名员工"})
    return jsonify({"ok": True, "data": summary})


@app.get("/api/tianrun/users")
def api_tianrun_users():
    denied = local_admin_only()
    if denied:
        return denied
    query = clean(request.args.get("q", ""))
    if len(query) < 2:
        return jsonify({"ok": False, "error": "请输入至少2个字符的姓名或工号"}), 400
    try:
        rows = tianrun_users(query)
    except (PermissionError, RuntimeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "data": rows})


@app.get("/api/tianrun/team")
def api_tianrun_team():
    denied = local_admin_only()
    if denied:
        return denied
    return jsonify({"ok": True, "data": team_assignment_statistics()})


@app.get("/api/tianrun/pending-tickets")
def api_tianrun_pending_tickets():
    denied = local_admin_only()
    if denied:
        return denied
    refresh = request.args.get("refresh") == "1"
    rows, warning = pending_guard_tickets(refresh=refresh)
    unresolved = unresolved_assignments()
    rows = [enrich_dispatch_ticket(row, unresolved) for row in rows]
    return jsonify(
        {
            "ok": True,
            "data": {
                "queue": GUARD_QUEUE_HANDLER,
                "statuses": sorted(PENDING_ASSIGN_STATUSES),
                "tickets": rows,
                "count": len(rows),
                "warning": warning,
                "refreshed_at": min((row.get("fetched_at", "") for row in rows), default=""),
                "read_at": datetime.now().isoformat(timespec="seconds"),
            },
        }
    )


@app.post("/api/tianrun/assignment/batch-preview")
def api_tianrun_batch_preview():
    denied = local_admin_only()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("ticket_ids"), list) or not isinstance(payload.get("members"), list):
        return jsonify({"ok": False, "error": "工单和人员必须为列表"}), 400
    ticket_ids = [clean(str(value)) for value in payload.get("ticket_ids", [])]
    member_names = [clean(str(value)) for value in payload.get("members", [])]
    reason = clean(str(payload.get("reason") or ""))
    ticket_ids = list(dict.fromkeys(ticket_id for ticket_id in ticket_ids if ticket_id))
    member_names = list(dict.fromkeys(name for name in member_names if name))
    if not ticket_ids or len(ticket_ids) > 100:
        return jsonify({"ok": False, "error": "请选择1至100张待领取工单"}), 400
    if not member_names or len(member_names) > len(TEAM_MEMBERS):
        return jsonify({"ok": False, "error": "请至少选择一名组员"}), 400
    if any(name not in TEAM_MEMBERS for name in member_names):
        return jsonify({"ok": False, "error": "所选人员不在当前组员名单内"}), 400
    if not reason:
        return jsonify({"ok": False, "error": "分派理由不能为空"}), 400
    overrides = payload.get("overrides") or {}
    risk_recipient = clean(str(payload.get("risk_recipient") or ""))
    if not isinstance(overrides, dict) or any(key not in ticket_ids or value not in member_names for key, value in overrides.items()):
        return jsonify({"ok": False, "error": "调整后的接收人须为本次已选员工，工单须在本次选择中"}), 400
    if risk_recipient and risk_recipient not in member_names:
        return jsonify({"ok": False, "error": "风险接收人须为本次已选员工"}), 400
    try:
        live_rows, warning = pending_guard_tickets(refresh=True)
        if warning:
            return jsonify({"ok": False, "error": "无法读取最新队列，暂不能生成分派方案；请刷新后再试"}), 503
        unresolved = unresolved_assignments()
        if any(ticket_id in unresolved for ticket_id in ticket_ids):
            return jsonify({"ok": False, "error": "所选工单存在提交中或待核验记录，请先核验，勿重复分派"}), 409
        live_by_id = {row["ticket_id"]: enrich_dispatch_ticket(row) for row in live_rows}
        missing = [ticket_id for ticket_id in ticket_ids if ticket_id not in live_by_id]
        if missing:
            return jsonify(
                {"ok": False, "error": f"以下工单已不在待领取值守队列，请刷新后重选：{', '.join(missing[:10])}"}
            ), 409
        users = [exact_tianrun_user(name) for name in member_names]
    except (ValueError, PermissionError, RuntimeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    token = secrets.token_urlsafe(24)
    tickets = sorted((live_by_id[ticket_id] for ticket_id in ticket_ids),
                     key=lambda row: (RISK_LEVEL_ORDER.get(row["risk_level"], 3), -(row.get("duration_minutes") or 0)))
    assignments = recommend_assignments(tickets, users, overrides, risk_recipient)
    for previous in list(TIANRUN_PREVIEWS):
        if TIANRUN_PREVIEWS[previous]["expires_at"] < time.time() or previous == payload.get("previous_token"):
            TIANRUN_PREVIEWS.pop(previous, None)
    TIANRUN_PREVIEWS[token] = {
        "mode": "batch",
        "assignments": assignments,
        "reason": reason[:200],
        "expires_at": time.time() + 300,
    }
    distribution = Counter(item["user"]["name"] for item in assignments)
    stats = {row["name"]: row for row in team_assignment_statistics()}
    return jsonify(
        {
            "ok": True,
            "data": {
                "confirm_token": token,
                "expires_in": 300,
                "assign_enabled": TIANRUN_ASSIGN_ENABLED,
                "reason": reason[:200],
                "assignments": assignments,
                "distribution": [{"name": name, "count": distribution[name], "before": stats[name]["assigned_today"],
                                  "after": stats[name]["assigned_today"] + distribution[name]} for name in member_names],
                "members": member_names,
                "warning": warning,
            },
        }
    )


@app.post("/api/tianrun/assignment/batch-execute")
def api_tianrun_batch_execute():
    denied = local_admin_only()
    if denied:
        return denied
    if not TIANRUN_ASSIGN_ENABLED:
        return jsonify({"ok": False, "error": "真实分派当前处于关闭状态"}), 403
    payload = request.get_json(silent=True) or {}
    token = clean(str(payload.get("confirm_token") or ""))
    preview = TIANRUN_PREVIEWS.pop(token, None)
    if not preview or preview.get("mode") != "batch" or preview["expires_at"] < time.time():
        return jsonify({"ok": False, "error": "批量分派预览已过期，请重新预览"}), 400
    if not TIANRUN_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "已有分派正在执行，请查看台账，稍后重新预览"}), 409
    try:
        return execute_dispatch_preview(preview)
    finally:
        TIANRUN_LOCK.release()


def execute_dispatch_preview(preview: dict):
    try:
        live_rows = fetch_open_handler_rows(GUARD_QUEUE_HANDLER)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"执行前无法读取队列，整批未提交：{exc}"}), 502
    live_by_id = {row["ticket_id"]: row for row in live_rows
                  if row["status"] in PENDING_ASSIGN_STATUSES and row.get("handler") == GUARD_QUEUE_HANDLER}
    unresolved = unresolved_assignments()
    unavailable = [item["ticket"]["ticket_id"] for item in preview["assignments"]
                   if item["ticket"]["ticket_id"] not in live_by_id or item["ticket"]["ticket_id"] in unresolved]
    if unavailable:
        return jsonify({"ok": False, "error": f"工单已离开待领取队列或已有待核验提交，整批未提交：{', '.join(unavailable[:10])}"}), 409
    results, stopped = [], False
    for item in preview["assignments"]:
        ticket, user = live_by_id[item["ticket"]["ticket_id"]], item["user"]
        status, audit_id = None, None
        if stopped:
            state, message = "未提交", "前序工单登录失效、权限异常或结果不明，本单未发起请求"
        else:
            try:
                user = exact_tianrun_user(user["cno"] or user["name"])
                if user["name"] != item["user"]["name"]:
                    raise ValueError("员工账号信息已变化，请重新生成方案")
                request_body = {
                    "autoAssign": 0, "handlerId": None, "handlerType": 0, "content": preview["reason"],
                    "handlerIds": [{"name": user["name"], "handlerId": user["handler_id"], "assignLimit": None}],
                    "defaultAssignLimit": 1,
                    "assignTickets": [{"id": ticket["ticket_id"], "taskId": None}], "selectAll": 0,
                }
                # Persist before the network write. Restart/disconnect cannot hide an ambiguous attempt.
                audit_id = save_assignment_audit(ticket, user, preview["reason"], "正在提交", None, "请求提交中；中断后请核验，勿重复分派")
                status, response_data = tianrun_json("PUT", "/api/ticket/batchAssign", request_body)
                message = clean(str(response_data.get("message") or response_data.get("msg") or "请求已提交"))
                state = "幂等完成" if "已是该处理人" in message else "已提交待核验"
            except TianrunUncertainError as exc:
                message, state, stopped = str(exc), "提交结果待确认", True
            except PermissionError as exc:
                message, state, stopped = str(exc), "失败", True
            except (ValueError, RuntimeError) as exc:
                message, state = str(exc), "失败"
            except Exception:
                message, state, stopped = "提交异常，请核验台账，勿重复提交", "提交结果待确认", True
        if audit_id is None:
            audit_id = save_assignment_audit(ticket, user, preview["reason"], state, status, message)
        else:
            conn = get_db()
            conn.execute("UPDATE assignment_audit SET state=?,response_status=?,message=? WHERE id=?", (state, status, message, audit_id))
            conn.commit()
            conn.close()
        results.append({"audit_id": audit_id, "ticket_id": ticket["ticket_id"], "category": ticket.get("category", "其他"),
                        "recipient": user["name"], "state": state, "message": message,
                        "retryable": state in {"失败", "未提交"}})
    counts = Counter(row["state"] for row in results)
    return jsonify({"ok": True, "data": {"total": len(results), "success": counts["幂等完成"] + counts["已提交待核验"],
                    "verified": counts["幂等完成"], "pending": counts["已提交待核验"], "uncertain": counts["提交结果待确认"],
                    "failed": counts["失败"], "unsubmitted": counts["未提交"], "results": results}})


@app.post("/api/tianrun/assignment/verify")
def api_tianrun_assignment_verify():
    denied = local_admin_only()
    if denied:
        return denied
    if not TIANRUN_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "正在提交分派，请稍后核验"}), 409
    try:
        return jsonify({"ok": True, "data": verify_assignment_results()})
    finally:
        TIANRUN_LOCK.release()


@app.post("/api/tianrun/assignment/preview")
def api_tianrun_assignment_preview():
    denied = local_admin_only()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    ticket_id = clean(str(payload.get("ticket_id") or ""))
    recipient = clean(str(payload.get("recipient") or ""))
    reason = clean(str(payload.get("reason") or ""))
    if not ticket_id.isdigit():
        return jsonify({"ok": False, "error": "工单ID必须为数字"}), 400
    if not recipient or not reason:
        return jsonify({"ok": False, "error": "接收人工号/姓名和分派理由不能为空"}), 400
    try:
        ticket = assignment_ticket(ticket_id)
        user = exact_tianrun_user(recipient)
    except (ValueError, PermissionError, RuntimeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    token = secrets.token_urlsafe(24)
    TIANRUN_PREVIEWS[token] = {
        "ticket_id": ticket_id,
        "ticket": ticket,
        "user": user,
        "reason": reason[:200],
        "expires_at": time.time() + 300,
    }
    return jsonify(
        {
            "ok": True,
            "data": {
                "confirm_token": token,
                "expires_in": 300,
                "ticket": ticket,
                "recipient": user,
                "reason": reason[:200],
                "assign_enabled": TIANRUN_ASSIGN_ENABLED,
            },
        }
    )


@app.post("/api/tianrun/assignment/execute")
def api_tianrun_assignment_execute():
    denied = local_admin_only()
    if denied:
        return denied
    if not TIANRUN_ASSIGN_ENABLED:
        return jsonify({"ok": False, "error": "真实分派当前处于关闭状态"}), 403
    payload = request.get_json(silent=True) or {}
    token = clean(str(payload.get("confirm_token") or ""))
    preview = TIANRUN_PREVIEWS.pop(token, None)
    if not preview or preview["expires_at"] < time.time():
        return jsonify({"ok": False, "error": "分派预览已过期，请重新预览"}), 400
    if preview.get("mode") == "batch":
        return jsonify({"ok": False, "error": "请使用批量分派执行入口"}), 400
    if not TIANRUN_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "已有分派正在执行，请稍后重新预览"}), 409
    try:
        # Legacy entry uses the same live-queue, one-use and duplicate-write checks.
        return execute_dispatch_preview({
            "assignments": [{"ticket": preview["ticket"], "user": preview["user"]}],
            "reason": preview["reason"],
        })
    finally:
        TIANRUN_LOCK.release()


@app.get("/api/tianrun/assignment/audit")
def api_tianrun_assignment_audit():
    denied = local_admin_only()
    if denied:
        return denied
    conn = get_db()
    rows = [dict(row) for row in conn.execute("SELECT a.*, COALESCE(t.category, '其他') AS category FROM assignment_audit a "
                                            "LEFT JOIN tickets t ON t.ticket_id=a.ticket_id ORDER BY a.id DESC LIMIT 100")]
    conn.close()
    return jsonify({"ok": True, "data": rows})


def main() -> None:
    parser = argparse.ArgumentParser(description="每日新增工单分析看板")
    parser.add_argument("--sync", action="store_true", help="启动前同步数据")
    parser.add_argument("--days", type=int, default=14, help="同步天数，默认14天")
    parser.add_argument("--host", default=os.environ.get("TICKET_DASHBOARD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-server", action="store_true", help="只同步，不启动服务")
    args = parser.parse_args()
    get_db().close()
    if args.sync:
        result = sync_days(days=args.days)
        print(result)
    if not args.no_server:
        threading.Thread(target=run_auto_sync_loop, daemon=True, name="ticket-auto-sync").start()
        start_complaint_audit_sync()
        app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
