"""人工核实记完成的链接校验与字段核对。

纯函数、不触碰浏览器与数据库，便于离线单元测试：
- parse_record_url：粘贴的链接必须指向"该行所属门户"的记录详情页；
- detail_mismatches：逐字核对门户详情页内容与 CSV 行
  （记录 ID、设备编号、校准日期、示值误差）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from . import config
from .automation import normalize_base_url
from .csvutil import normalize_date, same_error

# 门户记录详情页路径：/records/123（允许尾斜杠与 ?saved=1 等查询串）
_DETAIL_PATH_RE = re.compile(r"^/records/(\d+)/?$")


@dataclass
class ParsedRecordUrl:
    ok: bool
    record_id: str = ""
    error: str = ""


def portal_aliases(portal_url: str) -> set[str]:
    """该行所属门户的等价地址集合。

    用户上传时填写的地址、容器内地址（PORTAL_INTERNAL）与外部地址
    （PORTAL_EXTERNAL）指向同一台门户，互相等价；其余地址一律视为外站。
    """
    aliases = {normalize_base_url(portal_url)}
    internal = normalize_base_url(config.PORTAL_INTERNAL)
    external = normalize_base_url(config.PORTAL_EXTERNAL)
    if aliases & {internal, external}:
        aliases.update({internal, external})
    return aliases


def parse_record_url(portal_url: str, url: str) -> ParsedRecordUrl:
    """校验粘贴的链接是否为该行所属门户的记录详情页，并取出记录 ID。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return ParsedRecordUrl(False, error="链接需以 http:// 或 https:// 开头")
    base = normalize_base_url(url)
    if base not in portal_aliases(portal_url):
        return ParsedRecordUrl(
            False,
            error=(
                f"链接不属于本行所属门户（{portal_url}），"
                "请从该门户的记录详情页复制链接"
            ),
        )
    m = _DETAIL_PATH_RE.match(urlparse(url).path)
    if not m:
        return ParsedRecordUrl(
            False,
            error="链接不是门户记录详情页（路径应为 /records/记录ID）",
        )
    return ParsedRecordUrl(True, record_id=m.group(1))


def detail_mismatches(row, detail, expect_record_id: str) -> list[str]:
    """核对门户详情页字段与该行 CSV 值。

    返回不符项说明列表；空列表表示记录 ID、设备编号、校准日期、
    示值误差全部一致，允许记完成。
    """
    problems: list[str] = []
    if detail.record_id.strip() != expect_record_id.strip():
        problems.append(
            f"页面记录ID {detail.record_id} 与链接中的 ID {expect_record_id} 不一致"
        )
    if detail.device_no.strip() != row["device_no"].strip():
        problems.append(
            f"设备编号不符：门户 {detail.device_no} ≠ 本行 {row['device_no']}"
        )
    try:
        portal_date = normalize_date(detail.cal_date)
    except ValueError:
        portal_date = detail.cal_date.strip()
    if portal_date != row["cal_date"]:
        problems.append(
            f"校准日期不符：门户 {detail.cal_date} ≠ 本行 {row['cal_date']}"
        )
    if not same_error(detail.indication_error, row["indication_error"]):
        problems.append(
            f"示值误差不符：门户 {detail.indication_error} "
            f"≠ 本行 {row['indication_error']}"
        )
    return problems
