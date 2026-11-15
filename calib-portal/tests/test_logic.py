"""离线单元测试：CSV 解析、数值同值比较、状态分类。

运行：python -m pytest tests/ -q   （或直接 python tests/test_logic.py）
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database as db
from app.csvutil import CsvError, normalize_date, parse_csv, same_error
from app.worker import Worker


class CsvUtilTests(unittest.TestCase):
    def test_parse_basic(self):
        content = "设备编号,校准日期,示值误差\nA1,2026/11/01,+0.03\n".encode()
        rows = parse_csv(content)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cal_date, "2026-11-01")
        self.assertEqual(rows[0].device_no, "A1")
        self.assertEqual(rows[0].line_no, 2)

    def test_header_alias_and_bom(self):
        content = "﻿device_no,cal_date,indication_error\nA2,20261102,.5\n".encode()
        rows = parse_csv(content)
        self.assertEqual(rows[0].device_no, "A2")
        self.assertEqual(rows[0].cal_date, "2026-11-02")

    def test_missing_column(self):
        with self.assertRaises(CsvError):
            parse_csv("设备编号,校准日期\nA,2026-11-01\n".encode())

    def test_bad_date(self):
        with self.assertRaises(CsvError):
            parse_csv("设备编号,校准日期,示值误差\nA,11/01,+1\n".encode())

    def test_same_error_numeric(self):
        self.assertTrue(same_error("+0.03", "0.030"))
        self.assertTrue(same_error("-.5", "-0.50"))
        self.assertFalse(same_error("0.1", "0.100001"))
        self.assertFalse(same_error("+0.05", "+0.10"))

    def test_normalize_date_variants(self):
        self.assertEqual(normalize_date("2026.11.03"), "2026-11-03")
        self.assertEqual(normalize_date("2026-11-03"), "2026-11-03")


class FakeQuery:
    def __init__(self, records):
        self.ok = True
        self.records = records
        self.screenshot = ""


class FakeRow:
    def __init__(self, indication_error):
        self.indication_error = indication_error

    def __getitem__(self, key):
        return getattr(self, key)


class ClassifyTests(unittest.TestCase):
    def _rec(self, value):
        from app.automation import PortalRecord
        return PortalRecord("1", "D", "2026-11-01", value, "http://x/1")

    def test_classify(self):
        row = FakeRow("+0.10")
        self.assertEqual(Worker._classify(FakeQuery([]), row), "none")
        self.assertEqual(
            Worker._classify(FakeQuery([self._rec("0.100")]), row), "same"
        )
        self.assertEqual(
            Worker._classify(FakeQuery([self._rec("0.05")]), row), "diff"
        )
        self.assertEqual(
            Worker._classify(
                FakeQuery([self._rec("0.10"), self._rec("0.10")]), row
            ),
            "multi",
        )


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self.tmp.name
        # 重新加载模块级路径
        import importlib
        importlib.reload(config)
        importlib.reload(db)
        db.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_unique_key_reuse(self):
        bid = db.create_batch("http://p", "a.csv")
        rid1, act1 = db.upsert_row(bid, "http://p", "D1", "2026-11-01", "+1", 2)
        self.assertEqual(act1, "created")
        # 同门户同设备同日期：新批次也复用同一行
        bid2 = db.create_batch("http://p", "b.csv")
        rid2, act2 = db.upsert_row(bid2, "http://p", "D1", "2026-11-01", "+1", 2)
        self.assertEqual((rid2, act2), (rid1, "reused"))
        # 同批次内重复键
        _, act3 = db.upsert_row(bid, "http://p", "D1", "2026-11-01", "+1", 9)
        self.assertEqual(act3, "dup_in_csv")
        # 不同门户互不干扰
        _, act4 = db.upsert_row(bid, "http://q", "D1", "2026-11-01", "+1", 2)
        self.assertEqual(act4, "created")

    def test_status_flow(self):
        bid = db.create_batch("http://p", "a.csv")
        rid, _ = db.upsert_row(bid, "http://p", "D1", "2026-11-01", "+1", 2)
        db.update_row(rid, status=config.PAUSED_DIFF, detail="x")
        self.assertEqual(db.get_row(rid)["status"], config.PAUSED_DIFF)
        counts = db.batch_counts(bid)
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts[config.PAUSED_DIFF], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
