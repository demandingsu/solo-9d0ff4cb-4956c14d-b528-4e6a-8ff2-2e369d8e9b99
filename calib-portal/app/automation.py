"""Playwright 门户自动化。

严格模拟人工：只通过浏览器访问查询页 / 表单页，填写、点击、读 DOM，
不直接调用门户的任何数据接口。
"""
from __future__ import annotations

import concurrent.futures
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from . import config


@dataclass
class PortalRecord:
    record_id: str
    device_no: str
    cal_date: str
    indication_error: str
    url: str


@dataclass
class QueryResult:
    ok: bool
    records: list[PortalRecord] = field(default_factory=list)
    error: str = ""
    screenshot: str = ""


@dataclass
class SubmitResult:
    ok: bool
    error: str = ""
    detail: str = ""
    screenshot: str = ""
    record_id: str = ""
    record_url: str = ""


@dataclass
class DetailResult:
    """门户记录详情页读取结果（人工核实记完成用）。"""

    ok: bool
    record_id: str = ""
    device_no: str = ""
    cal_date: str = ""
    indication_error: str = ""
    error: str = ""
    screenshot: str = ""


def normalize_base_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        parsed = urlparse("http://" + url.strip())
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def internalize(url: str) -> str:
    """把用户侧外部门户地址转换成容器内浏览器可访问的地址。

    与 externalize 互为反向：用户在网页上填写/粘贴的是外部地址
    （如 http://localhost:8080），容器内 Playwright 需走服务名
    （如 http://portal:8080）。两者配置相同时为恒等映射。
    """
    internal_base = normalize_base_url(config.PORTAL_INTERNAL)
    external_base = normalize_base_url(config.PORTAL_EXTERNAL)
    if internal_base != external_base and url.startswith(external_base):
        return internal_base + url[len(external_base):]
    return url


def externalize(internal_url: str, portal_url: str) -> str:
    """把容器内门户链接转换成用户浏览器可打开的外部地址。"""
    internal_base = normalize_base_url(config.PORTAL_INTERNAL)
    external_base = normalize_base_url(config.PORTAL_EXTERNAL)
    target_base = normalize_base_url(portal_url)
    if internal_url.startswith(internal_base):
        return external_base + internal_url[len(internal_base):]
    if internal_url.startswith(target_base):
        return external_base + internal_url[len(target_base):]
    return internal_url


class PortalAutomation:
    """整个应用共享一个浏览器实例；每个批次一个 context。"""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    # ---- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        if self._browser is not None:
            return
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=config.HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

    def stop(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            self._browser = None
            if self._pw:
                self._pw.stop()
                self._pw = None

    def health_check(self, portal_url: str) -> tuple[bool, str]:
        """在创建批次前确认门户可打开。"""
        target = internalize(portal_url)
        ctx = self.new_context(portal_url)
        page = ctx.new_page()
        try:
            page.goto(target, wait_until="domcontentloaded",
                      timeout=config.PAGE_TIMEOUT_MS)
            page.wait_for_selector("text=旧门户", timeout=5000)
            return True, ""
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return False, f"无法打开门户 {portal_url}：{exc}"
        finally:
            ctx.close()

    def new_context(self, portal_url: str) -> BrowserContext:
        assert self._browser is not None
        return self._browser.new_context(
            base_url=normalize_base_url(internalize(portal_url)),
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )

    # ---- 截图 ------------------------------------------------------------
    @staticmethod
    def _shoot(page: Page, tag: str) -> str:
        name = f"{tag}_{int(time.time() * 1000)}.png"
        path = config.SCREENSHOT_DIR / name
        try:
            page.screenshot(path=str(path), full_page=False)
            return f"/screenshots/{name}"
        except PlaywrightError:
            return ""

    # ---- 查询页 ----------------------------------------------------------
    def query_records(
        self, ctx: BrowserContext, device_no: str, cal_date: str, tag: str
    ) -> QueryResult:
        """模拟人工：打开查询页 → 填条件 → 点查询 → 读结果表格。"""
        page = ctx.new_page()
        try:
            page.goto("/records", wait_until="domcontentloaded",
                      timeout=config.PAGE_TIMEOUT_MS)
            page.fill("#device_no", device_no)
            page.fill("#cal_date", cal_date)
            with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=config.PAGE_TIMEOUT_MS,
            ):
                page.click("#query-btn")

            # HTTP 500 / 门户错误页
            if page.locator("#error-page").count() > 0:
                shot = self._shoot(page, f"{tag}_queryfail")
                return QueryResult(
                    ok=False,
                    error="门户查询页返回错误页（HTTP 500/服务异常）",
                    screenshot=shot,
                )

            # 查无记录
            if page.locator("#no-result").count() > 0:
                return QueryResult(ok=True, records=[])

            rows_el = page.locator("#result-table .result-row")
            n = rows_el.count()
            if n == 0:
                # 结果表格存在但没行，视为无记录
                if page.locator("#result-table").count() == 0:
                    shot = self._shoot(page, f"{tag}_queryweird")
                    return QueryResult(
                        ok=False,
                        error="查询结果页结构无法识别",
                        screenshot=shot,
                    )
                return QueryResult(ok=True, records=[])

            records: list[PortalRecord] = []
            for i in range(n):
                row = rows_el.nth(i)
                link = row.locator(".record-link")
                href = link.get_attribute("href") or ""
                records.append(
                    PortalRecord(
                        record_id=row.locator(".col-id").inner_text().strip(),
                        device_no=row.locator(".col-device")
                        .inner_text().strip(),
                        cal_date=row.locator(".col-date")
                        .inner_text().strip(),
                        indication_error=row.locator(".col-error")
                        .inner_text().strip(),
                        url=urljoin(page.url, href),
                    )
                )
            return QueryResult(ok=True, records=records)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            shot = self._shoot(page, f"{tag}_queryexc")
            return QueryResult(
                ok=False,
                error=f"查询过程浏览器异常：{exc.__class__.__name__}: {exc}",
                screenshot=shot,
            )
        finally:
            page.close()

    # ---- 表单提交 --------------------------------------------------------
    def submit_record(
        self,
        ctx: BrowserContext,
        device_no: str,
        cal_date: str,
        indication_error: str,
        tag: str,
    ) -> SubmitResult:
        """模拟人工：打开新增表单 → 填写 → 点提交。

        门户可能在保存成功后断线：浏览器会抛连接类异常，
        此时无法确认结果，调用方必须"只查不提"复核。
        """
        page = ctx.new_page()
        try:
            page.goto("/records/new", wait_until="domcontentloaded",
                      timeout=config.PAGE_TIMEOUT_MS)
            page.fill("#device_no", device_no)
            page.fill("#cal_date", cal_date)
            page.fill("#indication_error", indication_error)

            try:
                with page.expect_navigation(
                    wait_until="domcontentloaded",
                    timeout=config.PAGE_TIMEOUT_MS,
                ):
                    page.click("#submit-btn")
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                shot = self._shoot(page, f"{tag}_submitdrop")
                return SubmitResult(
                    ok=False,
                    error="提交后未收到门户正常响应（可能已保存但断线）",
                    detail=f"{exc.__class__.__name__}: {exc}",
                    screenshot=shot,
                )

            # 正常路径：303 跳到详情页
            if re.search(r"/records/\d+", page.url):
                m = re.search(r"/records/(\d+)", page.url)
                rid = m.group(1) if m else ""
                return SubmitResult(
                    ok=True,
                    record_id=rid,
                    record_url=page.url,
                    detail="门户返回详情页，保存成功",
                )

            # 留在表单/错误页：门户拒绝或展示校验错误
            shot = self._shoot(page, f"{tag}_submiterr")
            body_txt = page.locator("body").inner_text()[:300]
            return SubmitResult(
                ok=False,
                error="门户未跳转到记录详情页，保存未确认",
                detail=body_txt,
                screenshot=shot,
            )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            shot = self._shoot(page, f"{tag}_submitexc")
            return SubmitResult(
                ok=False,
                error="提交过程浏览器异常（可能已保存但断线）",
                detail=f"{exc.__class__.__name__}: {exc}",
                screenshot=shot,
            )
        finally:
            page.close()

    # ---- 记录详情页（人工核实记完成用） -----------------------------------
    def read_record_detail(
        self, ctx: BrowserContext, url: str, tag: str
    ) -> DetailResult:
        """模拟人工：打开粘贴的门户记录详情页，读取页面上的实际字段。

        页面不可达、不是有效详情页时返回 ok=False，调用方必须保持
        行的暂停状态与原证据不变。
        """
        page = ctx.new_page()
        try:
            page.goto(
                internalize(url),
                wait_until="domcontentloaded",
                timeout=config.PAGE_TIMEOUT_MS,
            )
            try:
                page.wait_for_selector("#record-id", timeout=8000)
            except PlaywrightTimeoutError:
                shot = self._shoot(page, f"{tag}_nodetail")
                return DetailResult(
                    ok=False,
                    error="打开的页面不是有效的门户记录详情页"
                    "（记录不存在或已被删除）",
                    screenshot=shot,
                )
            return DetailResult(
                ok=True,
                record_id=page.locator("#record-id").inner_text().strip(),
                device_no=page.locator("#record-device").inner_text().strip(),
                cal_date=page.locator("#record-date").inner_text().strip(),
                indication_error=page.locator("#record-error")
                .inner_text()
                .strip(),
            )
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            shot = self._shoot(page, f"{tag}_unreach")
            return DetailResult(
                ok=False,
                error=f"门户记录页不可达：{exc.__class__.__name__}: {exc}",
                screenshot=shot,
            )
        finally:
            page.close()

    # ---- 便捷封装 --------------------------------------------------------
    def open_existing_page(self, ctx: BrowserContext, url: str) -> bool:
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=config.PAGE_TIMEOUT_MS)
            return page.locator("#record-id").count() > 0
        except PlaywrightError:
            return False
        finally:
            page.close()


class AutomationRunner:
    """所有 Playwright 同步 API 都在这个专属线程执行。

    FastAPI 运行在 asyncio 事件循环线程，Playwright sync API 禁止在
    事件循环线程使用，因此通过队列把调用 marshal 到本线程。
    """

    _STOP = object()

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.auto: PortalAutomation | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="playwright-runner", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._q.put((self._STOP, None, None))
            self._thread.join(timeout=15)

    def restart(self) -> None:
        """关闭并重新启动浏览器（崩溃恢复用），仍在 Playwright 线程执行。"""
        self.call(_restart_browser, timeout=60)

    def _run(self) -> None:
        self.auto = PortalAutomation()
        self.auto.start()
        while True:
            fn, args, fut = self._q.get()
            if fn is self._STOP:
                self.auto.stop()
                return
            try:
                fut.set_result(fn(self.auto, *args))
            except BaseException as exc:  # 回传给调用方
                fut.set_exception(exc)

    def call(self, fn: Callable, *args: Any, timeout: float | None = None):
        """同步等待一个在 Playwright 线程执行的调用。"""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((fn, args, fut))
        return fut.result(timeout=timeout)

    # ---- 透传方法（Web/Worker 侧的统一入口） -----------------------------
    def health_check(self, portal_url: str) -> tuple[bool, str]:
        return self.call(PortalAutomation.health_check, portal_url,
                         timeout=config.PAGE_TIMEOUT_MS / 1000 + 30)

    def new_context(self, portal_url: str) -> "ContextHandle":
        return self.call(PortalAutomation.new_context, portal_url)

    def close_context(self, ctx: BrowserContext) -> None:
        self.call(_close_context, ctx)

    def query_records(self, ctx: BrowserContext, device_no: str,
                      cal_date: str, tag: str) -> QueryResult:
        return self.call(PortalAutomation.query_records,
                         ctx, device_no, cal_date, tag,
                         timeout=config.PAGE_TIMEOUT_MS / 1000 * 2 + 30)

    def submit_record(self, ctx: BrowserContext, device_no: str,
                      cal_date: str, indication_error: str,
                      tag: str) -> SubmitResult:
        return self.call(PortalAutomation.submit_record,
                         ctx, device_no, cal_date, indication_error, tag,
                         timeout=config.PAGE_TIMEOUT_MS / 1000 * 2 + 30)

    def read_record_detail(self, portal_url: str, url: str,
                           tag: str) -> DetailResult:
        """打开门户记录详情页并读取字段；自建临时 context 并负责关闭。"""
        return self.call(_read_record_detail, portal_url, url, tag,
                         timeout=config.PAGE_TIMEOUT_MS / 1000 + 30)


def _read_record_detail(auto: PortalAutomation, portal_url: str,
                        url: str, tag: str) -> DetailResult:
    ctx = auto.new_context(portal_url)
    try:
        return auto.read_record_detail(ctx, url, tag)
    finally:
        ctx.close()


def _close_context(_auto: PortalAutomation, ctx: BrowserContext) -> None:
    ctx.close()


def _restart_browser(auto: PortalAutomation) -> None:
    auto.stop()
    auto.start()


# 让 Worker 中类型标注保持简单
ContextHandle = BrowserContext
