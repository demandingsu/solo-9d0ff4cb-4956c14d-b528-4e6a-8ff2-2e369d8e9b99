"""应用配置。"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "app.db"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
UPLOAD_DIR = DATA_DIR / "uploads"

# 浏览器里"门户记录链接"使用的地址。容器内访问用 PORTAL_INTERNAL，
# 给用户浏览器点击的外部地址用 PORTAL_EXTERNAL（可不同）。
PORTAL_INTERNAL = os.environ.get("PORTAL_INTERNAL", "http://portal:8080")
PORTAL_EXTERNAL = os.environ.get("PORTAL_EXTERNAL", "http://localhost:8080")

HEADLESS = os.environ.get("HEADLESS", "1") not in ("0", "false", "False")
PLAYWRIGHT_BROWSERS_PATH = os.environ.get(
    "PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright"
)
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "20000"))

# 行状态
PENDING = "pending"            # 尚未处理
QUERYING = "querying"          # 正在查询
DONE_EXISTING = "done_existing"  # 门户已有唯一同值记录，跳过
DONE_SUBMITTED = "done_submitted"  # 本系统提交成功并复核到
PAUSED_NO_RECORD = "paused_no_record"  # 提交中断线后恢复：查无记录，待人工核实
PAUSED_MULTI = "paused_multi"        # 多条记录
PAUSED_DIFF = "paused_diff"          # 唯一但异值
PAUSED_QUERY_FAIL = "paused_query_fail"  # 查询失败
PAUSED_SUBMIT_FAIL = "paused_submit_fail"  # 提交异常且复核无结果
IGNORED = "ignored"            # 人工忽略（如 CSV 内重复行）

PAUSED_STATES = {
    PAUSED_NO_RECORD,
    PAUSED_MULTI,
    PAUSED_DIFF,
    PAUSED_QUERY_FAIL,
    PAUSED_SUBMIT_FAIL,
}
DONE_STATES = {DONE_EXISTING, DONE_SUBMITTED}
TERMINAL_STATES = DONE_STATES | {IGNORED}

# 批次状态
B_RUNNING = "running"
B_PAUSED = "paused"
B_DONE = "done"

STATUS_LABELS = {
    PENDING: "待处理",
    QUERYING: "查询中",
    DONE_EXISTING: "已完成·门户已有同值（跳过）",
    DONE_SUBMITTED: "已完成·本次提交",
    PAUSED_NO_RECORD: "已暂停·断线后查无记录待核",
    PAUSED_MULTI: "已暂停·门户存在多条记录",
    PAUSED_DIFF: "已暂停·门户记录异值（不覆盖）",
    PAUSED_QUERY_FAIL: "已暂停·查询失败",
    PAUSED_SUBMIT_FAIL: "已暂停·提交异常待核",
    IGNORED: "已忽略",
}
