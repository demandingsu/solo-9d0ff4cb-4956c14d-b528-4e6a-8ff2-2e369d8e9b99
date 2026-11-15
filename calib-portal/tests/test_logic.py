"""离线单元测试：CSV 解析、数值同值比较、状态分类、核实记完成校验。

运行：python -m pytest tests/ -q   （或直接 python tests/test_logic.py）
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, database as db
from app.automation import DetailResult
from app.csvutil import CsvError, normalize_date, parse_csv, same_error
from app.verify import detail_mismatches, parse_record_url, portal_aliases
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

    def test_revision_guard(self):
        """任何写入都令 revision 递增；条件更新只在快照未变时生效。"""
        bid = db.create_batch("http://p", "a.csv")
        rid, _ = db.upsert_row(bid, "http://p", "D1", "2026-11-01", "+1", 2)
        rev0 = db.get_row(rid)["revision"]

        # 快照后行被改动（模拟核实期间后台重查/人工处置）
        db.update_row(rid, detail="被改动")
        rev1 = db.get_row(rid)["revision"]
        self.assertGreater(rev1, rev0)
        ok = db.update_row_if_revision(
            rid, rev0, status=config.DONE_EXISTING, detail="过时核验"
        )
        self.assertFalse(ok)
        self.assertNotEqual(db.get_row(rid)["status"], config.DONE_EXISTING)

        # 快照未变则可写入；即使行最终仍是暂停态，revision 不同也拒绝
        db.update_row(rid, status=config.PAUSED_QUERY_FAIL)
        rev2 = db.get_row(rid)["revision"]
        db.increment_attempt(rid)  # 后台重查先加尝试次数
        db.update_row(rid, status=config.PAUSED_QUERY_FAIL)  # 又回到暂停态
        self.assertEqual(
            db.get_row(rid)["status"], config.PAUSED_QUERY_FAIL
        )
        ok = db.update_row_if_revision(
            rid, rev2, status=config.DONE_EXISTING, detail="过时核验"
        )
        self.assertFalse(ok)
        self.assertEqual(
            db.get_row(rid)["status"], config.PAUSED_QUERY_FAIL
        )
        ok = db.update_row_if_revision(
            rid, db.get_row(rid)["revision"],
            status=config.DONE_EXISTING, portal_record_id="7",
            portal_record_url="http://p/records/7",
        )
        self.assertTrue(ok)
        row = db.get_row(rid)
        self.assertEqual(row["status"], config.DONE_EXISTING)
        self.assertEqual(row["portal_record_id"], "7")


class MarkDoneVerifyTests(unittest.TestCase):
    """核实记完成的链接校验与字段核对（不触碰浏览器）。"""

    PORTAL = "http://localhost:8080"

    def setUp(self):
        self._saved = (config.PORTAL_INTERNAL, config.PORTAL_EXTERNAL)
        config.PORTAL_INTERNAL = "http://portal:8080"
        config.PORTAL_EXTERNAL = "http://localhost:8080"

    def tearDown(self):
        config.PORTAL_INTERNAL, config.PORTAL_EXTERNAL = self._saved

    def _row(self):
        return {
            "device_no": "EQ-9002",
            "cal_date": "2026-11-21",
            "indication_error": "-0.13",
        }

    def _detail(self, **kw):
        base = dict(
            ok=True, record_id="9", device_no="EQ-9002",
            cal_date="2026-11-21", indication_error="-0.130",
        )
        base.update(kw)
        return DetailResult(**base)

    def test_parse_record_url_ok(self):
        for url in (
            "http://localhost:8080/records/9",
            "http://localhost:8080/records/9/",
            "http://localhost:8080/records/9?saved=1",
            "http://portal:8080/records/9",   # 容器内地址等价
        ):
            p = parse_record_url(self.PORTAL, url)
            self.assertTrue(p.ok, f"{url} 应被接受: {p.error}")
            self.assertEqual(p.record_id, "9")

    def test_parse_record_url_reject_foreign(self):
        for url in (
            "http://example.com/records/9",      # 外站
            "http://localhost:9999/records/9",   # 端口不符
            "https://localhost:8080/records/9",  # 协议不符
            "http://localhost:8080/records",     # 非详情页
            "http://localhost:8080/admin/records",
            "http://localhost:8080/records/abc",
            "javascript:alert(1)",
            "",
        ):
            p = parse_record_url(self.PORTAL, url)
            self.assertFalse(p.ok, f"{url} 应被拒绝")

    def test_portal_aliases(self):
        aliases = portal_aliases(self.PORTAL)
        self.assertIn("http://localhost:8080", aliases)
        self.assertIn("http://portal:8080", aliases)
        # 与配置无关的自定义门户只接受自身
        self.assertEqual(
            portal_aliases("http://my-portal:9000"),
            {"http://my-portal:9000"},
        )

    def test_detail_match(self):
        self.assertEqual(
            detail_mismatches(self._row(), self._detail(), "9"), []
        )

    def test_detail_mismatch_each_field(self):
        cases = [
            ("record_id", "10"),          # 页面 ID 与链接 ID 不一致
            ("device_no", "EQ-1002"),     # 粘贴了别的设备记录
            ("cal_date", "2026-11-20"),
            ("indication_error", "+0.05"),
        ]
        for field, bad in cases:
            problems = detail_mismatches(
                self._row(), self._detail(**{field: bad}), "9"
            )
            self.assertTrue(problems, f"{field} 不符应被检出")

    def test_detail_date_format_tolerance(self):
        # 门户页面日期写法不同但为同一天，视为相符
        d = self._detail(cal_date="2026/11/21")
        self.assertEqual(detail_mismatches(self._row(), d, "9"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
