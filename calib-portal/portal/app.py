"""本地模拟"旧门户"：无账号、服务端渲染表单 + 查询页。

仅用于演示，真实场景中它是计量室的旧门户站点。
支持通过 /admin/fault 注入故障：
  none         正常
  drop_submit  保存成功后直接断开 HTTP 响应（模拟"保存成功却断线"）
  query_500    查询接口返回 500（模拟查询失败）
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.status import HTTP_303_SEE_OTHER, HTTP_500_INTERNAL_SERVER_ERROR

DB_PATH = os.environ.get("PORTAL_DB", "/data/portal.db")

FAULT_MODES = {"none", "drop_submit", "query_500"}

app = FastAPI(title="模拟旧门户（校准记录）")


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_no TEXT NOT NULL,
                cal_date TEXT NOT NULL,
                indication_error TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        cur = conn.execute("SELECT COUNT(*) AS n FROM records")
        if cur.fetchone()["n"] == 0:
            seed = [
                # 与示例 CSV 同值 -> 自动化应跳过
                ("EQ-1002", "2026-11-01", "+0.02"),
                # 与示例 CSV 异值 -> 暂停、不覆盖
                ("EQ-1003", "2026-11-02", "+0.05"),
                # 同设备同日期两条 -> 多条暂停
                ("EQ-1004", "2026-11-03", "+0.01"),
                ("EQ-1004", "2026-11-03", "+0.04"),
            ]
            now = datetime.utcnow().isoformat(timespec="seconds")
            conn.executemany(
                "INSERT INTO records(device_no, cal_date, indication_error, created_at)"
                " VALUES (?, ?, ?, ?)",
                [(*r, now) for r in seed],
            )
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('fault_mode', 'none')"
        )


def get_fault_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key='fault_mode'"
    ).fetchone()
    return row["value"] if row else "none"


# --------------------------------------------------------------------------
# 页面模板（内联，保持门户零额外依赖风格）
# --------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: sans-serif; margin: 2em; color:#222 }}
table {{ border-collapse: collapse; margin: 1em 0 }}
th, td {{ border:1px solid #999; padding:4px 10px }}
input {{ padding:4px }} .nav a {{ margin-right:1em }}
.err {{ background:#fdd; padding:1em; border:1px solid #c33 }}
.ok {{ background:#dfd; padding:1em; border:1px solid #393 }}
</style></head><body>
<div class="nav"><a href="/">门户首页</a>
<a href="/records">校准记录查询</a>
<a href="/records/new">新增校准记录</a>
<a href="/admin/records">管理后台</a></div><hr>
{body}
</body></html>"""


def render(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=title, body=body))


# --------------------------------------------------------------------------
# 特殊响应：只发 response.start 就结束，模拟服务端保存后断线
# --------------------------------------------------------------------------
class DroppedResponse:
    """ASGI 响应：发出响应头后直接终止，不发送 body，连接被对端关闭。"""

    async def __call__(self, scope, receive, send):  # noqa: D102
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html; charset=utf-8")],
            }
        )
        # 故意不发送 http.response.body，uvicorn 将中断连接


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return render(
        "旧门户首页",
        "<h1>校准记录旧门户（演示用，免登录）</h1>"
        "<ul><li>查询已有记录：<a href='/records'>查询页</a></li>"
        "<li>手工录入：<a href='/records/new'>新增表单</a></li>"
        "<li>故障注入 / 数据重置：<a href='/admin/records'>管理后台</a></li></ul>",
    )


@app.get("/records", response_class=HTMLResponse)
def records_page(
    request: Request, device_no: str = "", cal_date: str = ""
) -> Response:
    with db() as conn:
        fault = get_fault_mode(conn)
        if fault == "query_500" and (device_no or cal_date):
            return HTMLResponse(
                PAGE.format(
                    title="查询失败",
                    body='<h1>查询失败</h1><div id="error-page" class="err">'
                    "门户查询服务暂时不可用（HTTP 500），请稍后重试。</div>",
                ),
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            )
        rows: list[sqlite3.Row] = []
        if device_no or cal_date:
            sql = "SELECT * FROM records WHERE 1=1"
            params: list[Any] = []
            if device_no:
                sql += " AND device_no = ?"
                params.append(device_no.strip())
            if cal_date:
                sql += " AND cal_date = ?"
                params.append(cal_date.strip())
            sql += " ORDER BY id"
            rows = conn.execute(sql, params).fetchall()

    form = f"""
    <h1>校准记录查询</h1>
    <form id="query-form" method="get" action="/records">
      设备编号 <input id="device_no" name="device_no" value="{device_no}">
      校准日期 <input id="cal_date" name="cal_date" placeholder="YYYY-MM-DD"
                     value="{cal_date}">
      <button id="query-btn" type="submit">查询</button>
    </form>"""
    if not (device_no or cal_date):
        return render("校准记录查询", form)

    if not rows:
        return render(
            "查询结果",
            form + '<p id="no-result">查无记录</p>',
        )

    trs = "".join(
        '<tr class="result-row">'
        f'<td class="col-id">{r["id"]}</td>'
        f'<td class="col-device">{r["device_no"]}</td>'
        f'<td class="col-date">{r["cal_date"]}</td>'
        f'<td class="col-error">{r["indication_error"]}</td>'
        f'<td><a class="record-link" href="/records/{r["id"]}">查看门户记录</a></td>'
        "</tr>"
        for r in rows
    )
    table = (
        '<table id="result-table"><thead><tr>'
        "<th>记录ID</th><th>设备编号</th><th>校准日期</th><th>示值误差</th><th>链接</th>"
        f"</tr></thead><tbody>{trs}</tbody></table>"
        f'<p id="result-count">共 {len(rows)} 条</p>'
    )
    return render("查询结果", form + table)


@app.get("/records/new", response_class=HTMLResponse)
def new_record_page() -> HTMLResponse:
    return render(
        "新增校准记录",
        """<h1>新增校准记录</h1>
        <form id="entry-form" method="post" action="/records">
          <p>设备编号 <input id="device_no" name="device_no" required></p>
          <p>校准日期 <input id="cal_date" name="cal_date"
             placeholder="YYYY-MM-DD" required></p>
          <p>示值误差 <input id="indication_error" name="indication_error"
             placeholder="如 +0.03" required></p>
          <p><button id="submit-btn" type="submit">提交保存</button></p>
        </form>""",
    )


@app.post("/records")
async def create_record(
    device_no: str = Form(...),
    cal_date: str = Form(...),
    indication_error: str = Form(...),
) -> Response:
    device_no = device_no.strip()
    cal_date = cal_date.strip()
    indication_error = indication_error.strip()
    if not (device_no and cal_date and indication_error):
        return render(
            "提交被拒绝",
            '<div class="err" id="error-page">所有字段均为必填。</div>',
        )

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO records(device_no, cal_date, indication_error, created_at)"
            " VALUES (?, ?, ?, ?)",
            (device_no, cal_date, indication_error,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        new_id = cur.lastrowid
        fault = get_fault_mode(conn)

    # 故障在提交之后生效：记录已经落库，但客户端拿不到正常响应
    if fault == "drop_submit":
        time.sleep(0.2)
        return DroppedResponse()  # type: ignore[return-value]

    return RedirectResponse(
        url=f"/records/{new_id}?saved=1",
        status_code=HTTP_303_SEE_OTHER,
    )


@app.get("/records/{record_id}", response_class=HTMLResponse)
def record_detail(record_id: int, saved: int = 0) -> HTMLResponse:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM records WHERE id = ?", (record_id,)
        ).fetchone()
    if row is None:
        return render("记录不存在", '<div class="err">未找到该记录</div>')
    banner = (
        '<div class="ok" id="save-ok">门户保存成功</div>' if saved else ""
    )
    body = f"""
    {banner}
    <h1>校准记录详情</h1>
    <table>
      <tr><th>门户记录ID</th><td id="record-id">{row['id']}</td></tr>
      <tr><th>设备编号</th><td id="record-device">{row['device_no']}</td></tr>
      <tr><th>校准日期</th><td id="record-date">{row['cal_date']}</td></tr>
      <tr><th>示值误差</th><td id="record-error">{row['indication_error']}</td></tr>
      <tr><th>录入时间</th><td>{row['created_at']}</td></tr>
    </table>
    <p><a href="/records">返回查询</a></p>"""
    return render("校准记录详情", body)


@app.get("/admin/records", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    with db() as conn:
        rows = conn.execute("SELECT * FROM records ORDER BY id").fetchall()
        fault = get_fault_mode(conn)
    trs = "".join(
        f"<tr><td>{r['id']}</td><td>{r['device_no']}</td>"
        f"<td>{r['cal_date']}</td><td>{r['indication_error']}</td>"
        f"<td>{r['created_at']}</td></tr>"
        for r in rows
    )
    opts = "".join(
        f'<option value="{m}"{" selected" if m == fault else ""}>'
        f"{m}</option>"
        for m in sorted(FAULT_MODES)
    )
    body = f"""
    <h1>管理后台</h1>
    <h2>故障注入</h2>
    <form method="post" action="/admin/fault">
      <select name="mode">{opts}</select>
      <button type="submit">应用故障模式</button>
    </form>
    <p>none=正常；drop_submit=保存后断线；query_500=查询返回500。</p>
    <h2>当前门户记录（{len(rows)} 条）</h2>
    <table><thead><tr><th>ID</th><th>设备编号</th><th>校准日期</th>
    <th>示值误差</th><th>录入时间</th></tr></thead><tbody>{trs}</tbody></table>
    <form method="post" action="/admin/reset"
          onsubmit="return confirm('确认清空并恢复演示种子数据？')">
      <button type="submit">重置门户数据与故障模式</button>
    </form>"""
    return render("管理后台", body)


@app.post("/admin/fault")
def set_fault(mode: str = Form(...)) -> RedirectResponse:
    if mode not in FAULT_MODES:
        mode = "none"
    with db() as conn:
        conn.execute(
            "UPDATE settings SET value=? WHERE key='fault_mode'", (mode,)
        )
    return RedirectResponse(url="/admin/records",
                            status_code=HTTP_303_SEE_OTHER)


@app.post("/admin/reset")
def reset_portal() -> RedirectResponse:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    return RedirectResponse(url="/admin/records",
                            status_code=HTTP_303_SEE_OTHER)
