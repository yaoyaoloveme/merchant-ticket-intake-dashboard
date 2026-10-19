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

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "tickets.db"
DEFAULT_SOURCE = "http://172.25.11.31:8080/ticket-dashboard/"
SOURCE_BASE = os.environ.get("TICKET_DASHBOARD_URL", DEFAULT_SOURCE).rstrip("/") + "/"

app = Flask(__name__)
SYNC_LOCK = threading.Lock()
SYNC_STATE: dict[str, Any] = {"running": False, "message": "尚未同步", "finished_at": None}
EVIDENCE_LOCK = threading.Lock()
EVIDENCE_STATE: dict[str, Any] = {"running": False, "message": "尚未补齐稽查证据", "finished_at": None}
AUTO_SYNC_INTERVAL_SECONDS = 5 * 60
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
        CREATE TABLE IF NOT EXISTS sync_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          date_from TEXT,
          date_to TEXT,
          ticket_count INTEGER DEFAULT 0,
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
        """
    )
    return conn


def latest_ticket_date() -> str:
    """首页默认展示本地已有数据的最新日期，避免跨日后尚未同步时打开即空白。"""
    conn = get_db()
    row = conn.execute("SELECT MAX(created_date) AS latest_date FROM tickets").fetchone()
    conn.close()
    return row["latest_date"] if row and row["latest_date"] else date.today().isoformat()


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


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
    return session


def fetch_ticket_day(day: date) -> list[dict[str, Any]]:
    session = source_session()
    url = urljoin(SOURCE_BASE, "list")
    first = session.get(url, params={"date": day.isoformat(), "page": 1}, timeout=30)
    first.raise_for_status()
    soup = BeautifulSoup(first.text, "html.parser")
    pager = clean(soup.select_one(".pager").get_text(" ", strip=True) if soup.select_one(".pager") else "")
    match = re.search(r"第\s*\d+\s*/\s*(\d+)\s*页", pager)
    pages = int(match.group(1)) if match else 1
    rows = parse_ticket_rows(soup)
    for page in range(2, pages + 1):
        response = session.get(url, params={"date": day.isoformat(), "page": page}, timeout=30)
        response.raise_for_status()
        rows.extend(parse_ticket_rows(BeautifulSoup(response.text, "html.parser")))
    return rows


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
        response = session.get(url, params={"event_id": ticket_id}, timeout=25)
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
        try:
            current = start_day
            while current <= end_day:
                SYNC_STATE["message"] = f"正在读取 {current.isoformat()}"
                records = fetch_ticket_day(current)
                save_day(conn, current, records)
                total += len(records)
                current += timedelta(days=1)
            finished = datetime.now().isoformat(timespec="seconds")
            message = f"同步完成，共读取 {total} 条工单"
            conn.execute(
                "UPDATE sync_runs SET finished_at=?,ticket_count=?,status='ok',message=? WHERE id=?",
                (finished, total, message, run_id),
            )
            conn.commit()
            SYNC_STATE.update({"running": False, "message": message, "finished_at": finished})
            return {"ok": True, "count": total, "date_from": start_day.isoformat(), "date_to": end_day.isoformat()}
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

    # 当天尚未结束时，环比和近7日均值统一截取到当前时刻，避免用“今日半天”
    # 对比“昨日全天”造成虚假下降。历史日期则按完整自然日分析。
    cutoff_time = datetime.now().strftime("%H:%M") if selected == date.today() else None

    def comparable(day_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not cutoff_time:
            return day_records
        return [r for r in day_records if len(r["created_at"]) >= 16 and r["created_at"][11:16] <= cutoff_time]

    selected_key = selected.isoformat()
    previous_key = (selected - timedelta(days=1)).isoformat()
    current = comparable(grouped.get(selected_key, []))
    previous = comparable(grouped.get(previous_key, []))

    trend = []
    cursor = start_day
    while cursor <= selected:
        day_records = comparable(grouped.get(cursor.isoformat(), []))
        counts = Counter(r["category"] for r in day_records)
        trend.append(
            {
                "date": cursor.isoformat(),
                "label": cursor.strftime("%m-%d"),
                "total": len(day_records),
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

    current_total = len(current)
    previous_total = len(previous)
    prior_counts = [len(comparable(grouped.get(key, []))) for key in prior_7_keys]
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
    }


@app.get("/")
def index():
    return render_template("index.html", today=latest_ticket_date())


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


def main() -> None:
    parser = argparse.ArgumentParser(description="每日新增工单分析看板")
    parser.add_argument("--sync", action="store_true", help="启动前同步数据")
    parser.add_argument("--days", type=int, default=14, help="同步天数，默认14天")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-server", action="store_true", help="只同步，不启动服务")
    args = parser.parse_args()
    get_db().close()
    if args.sync:
        result = sync_days(days=args.days)
        print(result)
    if not args.no_server:
        threading.Thread(target=run_auto_sync_loop, daemon=True, name="ticket-auto-sync").start()
        app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
