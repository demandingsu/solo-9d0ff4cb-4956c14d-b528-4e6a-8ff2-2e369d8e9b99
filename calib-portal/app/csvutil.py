"""CSV 解析与数值归一化比较。"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime

REQUIRED_HEADERS = ("设备编号", "校准日期", "示值误差")
HEADER_ALIASES = {
    "设备编号": "设备编号",
    "设备号": "设备编号",
    "设备编码": "设备编号",
    "device_no": "设备编号",
    "校准日期": "校准日期",
    "日期": "校准日期",
    "cal_date": "校准日期",
    "示值误差": "示值误差",
    "误差": "示值误差",
    "indication_error": "示值误差",
}


@dataclass
class CsvRow:
    line_no: int          # CSV 内物理行号（含表头，从 2 开始）
    device_no: str
    cal_date: str
    indication_error: str


class CsvError(ValueError):
    pass


def parse_csv(content: bytes) -> list[CsvRow]:
    # Excel 导出常带 BOM
    text = content.decode("utf-8-sig", errors="strict")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise CsvError("CSV 为空")

    header = [h.strip() for h in rows[0]]
    mapped = [HEADER_ALIASES.get(h, h) for h in header]
    idx = {}
    for name in REQUIRED_HEADERS:
        if name not in mapped:
            raise CsvError(
                f"缺少必需列「{name}」，实际表头：{', '.join(header)}"
            )
        idx[name] = mapped.index(name)

    out: list[CsvRow] = []
    for physical, raw in enumerate(rows[1:], start=2):
        if not raw or all(not cell.strip() for cell in raw):
            continue  # 跳过空行
        if len(raw) < len(header):
            raw = raw + [""] * (len(header) - len(raw))
        device_no = raw[idx["设备编号"]].strip()
        cal_date = raw[idx["校准日期"]].strip()
        indication_error = raw[idx["示值误差"]].strip()
        missing = [
            n for n, v in (
                ("设备编号", device_no),
                ("校准日期", cal_date),
                ("示值误差", indication_error),
            ) if not v
        ]
        if missing:
            raise CsvError(f"第 {physical} 行缺少字段：{', '.join(missing)}")
        try:
            cal_date = normalize_date(cal_date)
        except ValueError as exc:
            raise CsvError(f"第 {physical} 行日期无法识别：{exc}") from None
        try:
            normalize_error(indication_error)
        except ValueError as exc:
            raise CsvError(
                f"第 {physical} 行示值误差无法识别：{exc}"
            ) from None
        out.append(CsvRow(physical, device_no, cal_date, indication_error))
    if not out:
        raise CsvError("CSV 没有有效数据行")
    return out


def normalize_date(value: str) -> str:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(value)


_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)$")


def normalize_error(value: str) -> float:
    """把 '+0.03'、'0.030'、'-.5' 等归一为 float 以便严格同值比较。"""
    v = value.strip().replace(" ", "")
    if not _NUM_RE.match(v):
        raise ValueError(value)
    return float(v)


def same_error(a: str, b: str) -> bool:
    """门户值与 CSV 值是否为同一示值误差（按数值比较，忽略书写格式差异）。"""
    try:
        return normalize_error(a) == normalize_error(b)
    except ValueError:
        return a.strip() == b.strip()
