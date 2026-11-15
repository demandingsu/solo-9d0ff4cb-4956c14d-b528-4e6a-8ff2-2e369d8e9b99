"""FastAPI Web 应用：上传 CSV、查看逐行状态、人工续办。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, database as db
from .automation import AutomationRunner, externalize, normalize_base_url
from .csvutil import CsvError, parse_csv
from .verify import detail_mismatches, parse_record_url
from .worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("web")

app = FastAPI(title="校准读数补录助手")
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)
config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
app.mount(
    "/screenshots",
    StaticFiles(directory=str(config.SCREENSHOT_DIR)),
    name="screenshots",
)

automation = AutomationRunner()
worker = Worker(automation)


@app.on_event("startup")
def _startup() -> None:
    db.init()
    automation.start()
    worker.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    worker.stop()
    automation.stop()


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    batches = db.list_batches()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "batches": batches,
            "labels": config.STATUS_LABELS,
            "portal_external": config.PORTAL_EXTERNAL,
        },
    )


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int):
    batch = db.get_batch(batch_id)
    if batch is None:
        return RedirectResponse("/", status_code=303)
    rows = db.list_rows(batch_id)
    counts = db.batch_counts(batch_id)
    return templates.TemplateResponse(
        request,
        "batch.html",
        {
            "batch": batch,
            "rows": rows,
            "counts": counts,
            "labels": config.STATUS_LABELS,
            "paused_states": sorted(config.PAUSED_STATES),
            "done_states": sorted(config.DONE_STATES),
        },
    )


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@app.post("/api/batches")
async def create_batch(
    portal_url: str = Form(...), file: UploadFile = File(...)
):
    portal_url = normalize_base_url(portal_url)
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return JSONResponse(
            {"ok": False, "error": "请上传 .csv 文件"}, status_code=400
        )
    content = await file.read()
    try:
        parsed = parse_csv(content)
    except CsvError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except UnicodeDecodeError:
        return JSONResponse(
            {"ok": False, "error": "CSV 需为 UTF-8 编码"}, status_code=400
        )

    # 先通过浏览器确认门户可打开（不调用其数据接口）
    try:
        ok, err = automation.health_check(portal_url)
    except Exception as exc:  # 浏览器尚未就绪等
        ok, err = False, str(exc)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": f"门户连通性检查失败：{err}"},
            status_code=502,
        )

    saved = config.UPLOAD_DIR / file.filename
    saved.write_bytes(content)

    batch_id = db.create_batch(portal_url, file.filename)
    dup_notes: list[str] = []
    for r in parsed:
        row_id, action = db.upsert_row(
            batch_id, portal_url, r.device_no, r.cal_date,
            r.indication_error, r.line_no,
        )
        if action == "dup_in_csv":
            db.update_row(
                row_id, status=config.IGNORED,
                detail=f"CSV 第 {r.line_no} 行与本文件内更早行同键，忽略",
            )
            dup_notes.append(f"第 {r.line_no} 行重复已忽略")
        elif action == "reused":
            dup_notes.append(
                f"第 {r.line_no} 行 {r.device_no}/{r.cal_date} "
                "已有持久化进度，续办"
            )

    worker.kick()
    return JSONResponse(
        {
            "ok": True,
            "batch_id": batch_id,
            "rows": len(parsed),
            "notes": dup_notes,
            "redirect": f"/batches/{batch_id}",
        }
    )


@app.get("/api/batches/{batch_id}/rows")
def api_rows(batch_id: int):
    if db.get_batch(batch_id) is None:
        return JSONResponse({"ok": False}, status_code=404)
    rows = [dict(r) for r in db.list_rows(batch_id)]
    counts = db.batch_counts(batch_id)
    batch = dict(db.get_batch(batch_id))
    for r in rows:
        r["status_label"] = config.STATUS_LABELS.get(r["status"], r["status"])
    return JSONResponse(
        {"ok": True, "batch": batch, "rows": rows, "counts": counts,
         "labels": config.STATUS_LABELS}
    )


@app.post("/api/batches/{batch_id}/resume")
def resume_batch(batch_id: int):
    """续办：批次重新进入运行态，工作线程只处理 pending 行；
    暂停行保持暂停，需逐行人工处理。"""
    batch = db.get_batch(batch_id)
    if batch is None:
        return JSONResponse({"ok": False, "error": "批次不存在"}, 404)
    db.set_batch_status(batch_id, config.B_RUNNING, note="人工续办")
    worker.kick()
    return JSONResponse({"ok": True})


# ---- 逐行人工操作 -------------------------------------------------------
@app.post("/api/rows/{row_id}/retry-query")
def row_retry_query(row_id: int):
    """暂停行重新查询（只查不提）。系统会对该行走恢复逻辑。"""
    row = db.get_row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "行不存在"}, 404)
    if row["status"] not in config.PAUSED_STATES:
        return JSONResponse(
            {"ok": False, "error": "仅暂停状态的行可重新查询"}, 409
        )
    # 置为 querying 后由下一轮恢复循环"只查不提"处理
    db.update_row(row_id, status=config.QUERYING,
                  detail="人工触发重新查询（只查不提）")
    worker.kick()
    return JSONResponse({"ok": True})


@app.post("/api/rows/{row_id}/mark-done")
def row_mark_done(row_id: int, portal_record_url: str = Form("")):
    """人工核实后记完成（先核实，再记完成）。

    安全流程：
    1. 链接必须指向"该行所属门户"的记录详情页，外站/非详情页一律拒绝；
    2. 用 Playwright 实际打开该页，核对记录 ID、设备编号、校准日期、
       示值误差与本行一致；页面不可达或字段不符时保持暂停与原证据；
    3. 读取门户期间若该行被后台重查或人工处置（revision 变化），
       即使最终仍是暂停态，也放弃本次核验结果，要求重新核实；
    4. 全部通过才原子写入 记录 ID + 链接 + 完成状态，持久保存。
    """
    row = db.get_row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "行不存在"}, 404)
    if row["status"] not in config.PAUSED_STATES:
        return JSONResponse({"ok": False, "error": "仅暂停行可标记"}, 409)

    parsed = parse_record_url(row["portal_url"], portal_record_url)
    if not parsed.ok:
        return JSONResponse({"ok": False, "error": parsed.error}, 400)

    # 核实前快照：此后任何对该行的改动（含后台重查、人工处置）
    # 都会令 revision 递增，第 4 步的原子写入将因此失败。
    snapshot_rev = row["revision"]

    # 用浏览器打开粘贴的详情页读取实际内容（不调用门户数据接口）。
    # 本路由运行在线程池中，浏览器调用走专属 runner 线程，
    # 期间其他行的处理与状态查询不受影响。
    try:
        detail = automation.read_record_detail(
            row["portal_url"], portal_record_url.strip(), f"markdone_r{row_id}"
        )
    except Exception as exc:  # 浏览器尚未就绪等
        return JSONResponse(
            {"ok": False, "error": f"浏览器核实失败，保持暂停：{exc}"},
            status_code=502,
        )
    if not detail.ok:
        return JSONResponse(
            {"ok": False,
             "error": f"门户记录页核实失败，保持暂停：{detail.error}"},
            status_code=502,
        )

    problems = detail_mismatches(row, detail, parsed.record_id)
    if problems:
        return JSONResponse(
            {
                "ok": False,
                "error": "门户记录与本行不符，保持暂停："
                + "；".join(problems),
            },
            status_code=409,
        )

    # 全部核对通过：仅当核实期间该行未被改动才落库
    url_saved = externalize(portal_record_url.strip(), row["portal_url"])
    saved = db.update_row_if_revision(
        row_id,
        snapshot_rev,
        status=config.DONE_EXISTING,
        portal_record_id=detail.record_id,
        portal_record_url=url_saved,
        detail=(
            f"人工核实门户记录（ID {detail.record_id}）与本行"
            f"设备/日期/示值误差一致，记完成：{url_saved}"
        ),
    )
    if not saved:
        return JSONResponse(
            {
                "ok": False,
                "error": "核实期间该行已被后台重查或人工处置，"
                "本次核验结果已作废，请重新核实",
            },
            status_code=409,
        )

    # 若批次内行已全部完结，批次状态收口为 done（其余情况保持原态，
    # 剩余待处理行仍需人工显式续办）
    counts = db.batch_counts(row["batch_id"])
    unfinished = counts["total"] - sum(
        counts.get(s, 0) for s in config.TERMINAL_STATES
    )
    if unfinished == 0:
        db.set_batch_status(row["batch_id"], config.B_DONE,
                            note="全部行已完结")
    worker.kick()
    return JSONResponse({"ok": True, "record_id": detail.record_id})


@app.post("/api/rows/{row_id}/ignore")
def row_ignore(row_id: int, reason: str = Form("")):
    row = db.get_row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "行不存在"}, 404)
    if row["status"] in config.TERMINAL_STATES:
        return JSONResponse({"ok": False, "error": "该行已完结"}, 409)
    db.update_row(
        row_id, status=config.IGNORED,
        detail=f"人工忽略：{reason or '未填写原因'}",
    )
    worker.kick()
    return JSONResponse({"ok": True})


@app.post("/api/rows/{row_id}/resubmit")
def row_resubmit(row_id: int):
    """显式人工授权：对"查无记录"暂停行重新走完整 查→提→复核 流程。

    系统绝不自动重提；只有人工点击才会回到 pending。
    """
    row = db.get_row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "行不存在"}, 404)
    allowed = {config.PAUSED_NO_RECORD, config.PAUSED_SUBMIT_FAIL,
               config.PAUSED_QUERY_FAIL}
    if row["status"] not in allowed:
        return JSONResponse(
            {"ok": False, "error": "该状态不允许重新提交"}, 409
        )
    db.update_row(
        row_id, status=config.PENDING,
        detail="人工授权重新提交（将先查询、确认无同值后再提交）",
    )
    db.set_batch_status(row["batch_id"], config.B_RUNNING,
                        note="人工授权重提后续办")
    worker.kick()
    return JSONResponse({"ok": True})
