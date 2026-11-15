"""批次处理工作线程与安全状态机。

铁律：
1. 每行先查门户，确认没有唯一同值记录后才允许提交；
2. 提交后必须再次查询复核；
3. 服务重启 / 提交中断线后的恢复行"只查询、不重提"：
   唯一同值 -> 记完成；查无 -> 暂停待人工核实；多条/异值 -> 暂停；
4. 查询失败、多条、异值一律暂停该批，不覆盖任何门户原记录。
"""
from __future__ import annotations

import logging
import threading
import traceback

from playwright.sync_api import BrowserContext

from . import config, database as db
from .automation import AutomationRunner, QueryResult, SubmitResult
from .csvutil import same_error

log = logging.getLogger("worker")


class Worker:
    def __init__(self, automation: AutomationRunner) -> None:
        self.auto = automation
        self._lock = threading.Lock()  # 同一时刻只跑一个批次
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="batch-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.recover_after_restart()
                batch = db.running_batches()[0:1]
                if batch:
                    with self._lock:
                        self._process_batch(batch[0]["id"])
                else:
                    self._wake.wait(timeout=5)
                    self._wake.clear()
            except Exception:  # 工作线程不能死
                log.exception("worker loop error")
                try:
                    self.auto.restart()  # 浏览器可能已崩溃，重启后下轮重试
                except Exception:
                    log.exception("browser restart failed")
                self._wake.wait(timeout=5)

    # ------------------------------------------------------------------
    # 启动恢复：停留在 querying 的在途行，只查询不提交。
    # 暂停态是人工待办，系统不会自动反复重查。
    # ------------------------------------------------------------------
    def recover_after_restart(self) -> None:
        rows = db.in_flight_rows()
        if not rows:
            return
        log.warning("服务重启恢复：%d 个在途行需要只查不提复核", len(rows))
        # 按门户分组复用浏览器 context
        ctx_cache: dict[str, BrowserContext] = {}
        try:
            for row in rows:
                batch = db.get_batch(row["batch_id"])
                if batch is None:
                    continue
                portal_url = row["portal_url"]
                ctx = ctx_cache.get(portal_url)
                if ctx is None:
                    ctx = self.auto.new_context(portal_url)
                    ctx_cache[portal_url] = ctx
                self._recover_one(ctx, row)
        finally:
            for ctx in ctx_cache.values():
                self.auto.close_context(ctx)
        # 恢复后统一重算各批次状态
        batch_ids = {r["batch_id"] for r in rows}
        for bid in batch_ids:
            self._refresh_batch_status(bid)

    def _recover_one(self, ctx: BrowserContext, row) -> None:
        tag = f"recover_r{row['id']}"
        db.update_row(row["id"], status=config.QUERYING,
                      detail="服务重启/中断恢复：仅查询复核，不重新提交")
        db.increment_attempt(row["id"])
        q: QueryResult = self.auto.query_records(
            ctx, row["device_no"], row["cal_date"], tag
        )
        if not q.ok:
            db.update_row(
                row["id"], status=config.PAUSED_QUERY_FAIL,
                detail=f"恢复时查询失败：{q.error}",
                screenshot=q.screenshot or row["screenshot"],
            )
            return
        decision = self._classify(q, row)
        if decision == "same":
            rec = q.records[0]
            # 进入恢复流程的行原本都是 querying（在途），复核到同值即记完成
            db.update_row(
                row["id"], status=config.DONE_SUBMITTED,
                detail=f"恢复复核：门户存在唯一同值记录（ID {rec.record_id}），记完成",
                portal_record_id=rec.record_id,
                portal_record_url=self._ext(rec.url, row["portal_url"]),
                screenshot=q.screenshot or row["screenshot"],
            )
        elif decision == "none":
            db.update_row(
                row["id"], status=config.PAUSED_NO_RECORD,
                detail="恢复复核：门户查无此记录，暂停待人工核实（不自动重提）",
                screenshot=q.screenshot or row["screenshot"],
            )
        elif decision == "multi":
            db.update_row(
                row["id"], status=config.PAUSED_MULTI,
                detail=f"恢复复核：门户存在 {len(q.records)} 条记录，暂停待核",
                portal_record_url="; ".join(
                    self._ext(r.url, row["portal_url"]) for r in q.records
                ),
                screenshot=q.screenshot or row["screenshot"],
            )
        else:  # diff
            rec = q.records[0]
            db.update_row(
                row["id"], status=config.PAUSED_DIFF,
                detail=(
                    f"恢复复核：门户记录（ID {rec.record_id}）示值误差 "
                    f"{rec.indication_error} 与填报值 {row['indication_error']} 不一致"
                ),
                portal_record_id=rec.record_id,
                portal_record_url=self._ext(rec.url, row["portal_url"]),
                screenshot=q.screenshot or row["screenshot"],
            )

    # ------------------------------------------------------------------
    # 正常批次处理
    # ------------------------------------------------------------------
    def _process_batch(self, batch_id: int) -> None:
        batch = db.get_batch(batch_id)
        if batch is None:
            return
        portal_url = batch["portal_url"]
        log.info("开始处理批次 #%s 门户=%s", batch_id, portal_url)
        ctx = self.auto.new_context(portal_url)
        try:
            pending = db.rows_by_status(batch_id, {config.PENDING})
            for row in pending:
                if db.get_batch(batch_id)["status"] != config.B_RUNNING:
                    log.info("批次 #%s 已非运行态，停止取新行", batch_id)
                    break
                self._process_one(ctx, row)
                if db.get_row(row["id"])["status"] in config.PAUSED_STATES:
                    db.set_batch_status(
                        batch_id, config.B_PAUSED,
                        note=f"第 {row['line_no']} 行触发暂停，后续行未处理"
                    )
                    break
        finally:
            self.auto.close_context(ctx)
        self._refresh_batch_status(batch_id)

    def _process_one(self, ctx: BrowserContext, row) -> None:
        tag = f"r{row['id']}"
        log.info("行 %s：%s %s %s", row["id"], row["device_no"],
                 row["cal_date"], row["indication_error"])

        # 第一步：永远先查门户
        db.update_row(row["id"], status=config.QUERYING,
                      detail="查询门户是否已有记录")
        db.increment_attempt(row["id"])
        q: QueryResult = self.auto.query_records(
            ctx, row["device_no"], row["cal_date"], tag
        )
        if not q.ok:
            db.update_row(
                row["id"], status=config.PAUSED_QUERY_FAIL,
                detail=f"查询失败，暂停该批：{q.error}",
                screenshot=q.screenshot,
            )
            return

        decision = self._classify(q, row)
        if decision == "same":
            rec = q.records[0]
            db.update_row(
                row["id"], status=config.DONE_EXISTING,
                detail=f"门户已有唯一同值记录（ID {rec.record_id}），跳过提交",
                portal_record_id=rec.record_id,
                portal_record_url=self._ext(rec.url, row["portal_url"]),
                screenshot=q.screenshot,
            )
            return
        if decision == "multi":
            db.update_row(
                row["id"], status=config.PAUSED_MULTI,
                detail=f"门户存在 {len(q.records)} 条记录，无法判定，暂停不提交",
                portal_record_url="; ".join(
                    self._ext(r.url, row["portal_url"]) for r in q.records
                ),
                screenshot=q.screenshot,
            )
            return
        if decision == "diff":
            rec = q.records[0]
            db.update_row(
                row["id"], status=config.PAUSED_DIFF,
                detail=(
                    f"门户已有异值记录（ID {rec.record_id}，"
                    f"门户值 {rec.indication_error} ≠ 填报值 "
                    f"{row['indication_error']}），暂停且不覆盖"
                ),
                portal_record_id=rec.record_id,
                portal_record_url=self._ext(rec.url, row["portal_url"]),
                screenshot=q.screenshot,
            )
            return

        # decision == "none"：确认门户没有，才提交
        s: SubmitResult = self.auto.submit_record(
            ctx, row["device_no"], row["cal_date"],
            row["indication_error"], tag,
        )

        # 无论提交返回成功还是异常，都以"再次查询"为唯一确认手段
        q2: QueryResult = self.auto.query_records(
            ctx, row["device_no"], row["cal_date"], f"{tag}_verify"
        )
        if not q2.ok:
            db.update_row(
                row["id"], status=config.PAUSED_QUERY_FAIL,
                detail=(
                    f"提交后复核查询失败（提交结果：{s.error or '正常响应'}），"
                    "暂停待人工核实，不重提"
                ),
                screenshot=s.screenshot,
            )
            return

        decision2 = self._classify(q2, row)
        if decision2 == "same":
            rec = q2.records[0]
            note = "提交后复核到唯一同值记录"
            if not s.ok:
                note = f"门户保存成功但响应中断（{s.error}）；{note}，确认完成"
            db.update_row(
                row["id"], status=config.DONE_SUBMITTED,
                detail=note,
                portal_record_id=rec.record_id,
                portal_record_url=self._ext(rec.url, row["portal_url"]),
                screenshot=s.screenshot or q2.screenshot,
            )
            return
        if decision2 == "none":
            db.update_row(
                row["id"], status=config.PAUSED_SUBMIT_FAIL,
                detail=(
                    "提交未获正常响应且复核查无记录，暂停待人工核实，"
                    "禁止自动重提以防重复填报"
                    + ("" if s.ok else f"（浏览器异常：{s.error}）")
                ),
                screenshot=s.screenshot,
            )
            return
        if decision2 == "multi":
            db.update_row(
                row["id"], status=config.PAUSED_MULTI,
                detail=(
                    f"提交后复核发现 {len(q2.records)} 条记录，暂停待核，"
                    "不做任何覆盖"
                ),
                portal_record_url="; ".join(
                    self._ext(r.url, row["portal_url"]) for r in q2.records
                ),
                screenshot=s.screenshot,
            )
            return
        # diff
        rec = q2.records[0]
        db.update_row(
            row["id"], status=config.PAUSED_DIFF,
            detail=(
                f"提交后复核发现门户异值记录（ID {rec.record_id}，"
                f"门户值 {rec.indication_error}），暂停不覆盖"
            ),
            portal_record_id=rec.record_id,
            portal_record_url=self._ext(rec.url, row["portal_url"]),
            screenshot=s.screenshot,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _classify(q: QueryResult, row) -> str:
        """same / none / multi / diff。"""
        if not q.records:
            return "none"
        if len(q.records) > 1:
            return "multi"
        rec = q.records[0]
        return "same" if same_error(
            rec.indication_error, row["indication_error"]
        ) else "diff"

    @staticmethod
    def _ext(url: str, portal_url: str) -> str:
        from .automation import externalize
        return externalize(url, portal_url)

    @staticmethod
    def _refresh_batch_status(batch_id: int) -> None:
        counts = db.batch_counts(batch_id)
        unfinished = counts["total"] - sum(
            counts.get(s, 0) for s in config.TERMINAL_STATES
        )
        paused = sum(counts.get(s, 0) for s in config.PAUSED_STATES)
        if paused:
            db.set_batch_status(batch_id, config.B_PAUSED,
                                note="存在暂停行，待人工处理后续办")
        elif unfinished == 0:
            db.set_batch_status(batch_id, config.B_DONE, note="全部行已完结")
        else:
            db.set_batch_status(batch_id, config.B_RUNNING, note="")
