"""Offline unit tests; do not access Shopee or Bark."""
import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch, Mock
import monitor

from monitor import extract_payload, inspect_payload, parse_product_ids, model_state


def payload(models, status=1, item_status="normal"):
    return {"item": {"title": '商品 "A"', "item_status": item_status, "status": status, "models": models}}


class MonitorTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(parse_product_ids("https://shopee.tw/abc-i.123.456?ref=foo"), ("123", "456"))
        self.assertEqual(parse_product_ids("https://shopee.tw/product/123/456"), ("123", "456"))
        self.assertEqual(parse_product_ids("https://shopee.tw/opaanlp/1510747083/53668488583"), ("1510747083", "53668488583"))
        with self.assertRaises(ValueError):
            parse_product_ids("https://evil.com/a-i.1.2")

    def test_payload_extraction(self):
        value = payload([{"name": "黑色,M", "is_grayout": True, "is_clickable": False}])
        html = "<html><script>const x={\"123/456\":" + json.dumps(value, ensure_ascii=False) + "};</script></html>"
        self.assertEqual(extract_payload(html, "123", "456"), value)
        self.assertIsNone(extract_payload("<html>none</html>", "123", "456"))

    def test_available_and_soldout(self):
        data = payload([
            {"name": "黑色,M", "stock": 10, "is_grayout": True, "is_clickable": False},
            {"name": "白色,L", "stock": 10, "is_grayout": False, "is_clickable": True},
        ])
        self.assertEqual(inspect_payload(data, []).state, "in_stock")
        self.assertEqual(inspect_payload(data, ["黑色", "M"]).state, "out_of_stock")
        self.assertEqual(inspect_payload(data, ["白色", "L"]).state, "in_stock")
        self.assertEqual(inspect_payload(data, ["綠色"]).state, "unknown")

    def test_dont_trust_stock_or_delisted(self):
        sold = payload([{"name": "only", "stock": 10, "is_grayout": True}])
        self.assertEqual(inspect_payload(sold, []).state, "out_of_stock")
        self.assertEqual(inspect_payload(payload([{"name": "only", "stock": 100}]), []).state, "unknown")
        self.assertEqual(inspect_payload(payload([{"name": "only", "is_grayout": False}], status=0), []).state, "unknown")
        self.assertEqual(inspect_payload(payload([{"name": "only", "is_grayout": False}], item_status="deleted"), []).state, "unknown")
        self.assertIsNone(model_state({"is_grayout": True, "is_clickable": True}))

    def test_notify_only_on_restocks(self):
        cfg = {"items": [{"url": "https://shopee.tw/product/123/456", "name": "測試", "variant_keywords": []}]}
        with TemporaryDirectory() as td:
            with patch.object(monitor, "STATE_FILE", Path(td) / "state.json"):
                with patch.object(monitor, "send_bark") as push:
                    with patch.object(monitor, "fetch_stock", side_effect=[
                        monitor.Result("out_of_stock", "缺貨"),
                        monitor.Result("unknown", "驗證頁"),
                        monitor.Result("in_stock", "可買", "商品"),
                        monitor.Result("in_stock", "可買", "商品"),
                        monitor.Result("out_of_stock", "缺貨"),
                        monitor.Result("in_stock", "可買", "商品"),
                    ]):
                        for _ in range(6):
                            monitor.check_all(cfg, Mock(), use_browser=False)
                    self.assertEqual(push.call_count, 2)

    def test_failed_push_keeps_out_of_stock_state(self):
        cfg = {"items": [{"url": "https://shopee.tw/product/123/456", "name": "測試"}]}
        with TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            with patch.object(monitor, "STATE_FILE", path):
                with patch.object(monitor, "send_bark", side_effect=RuntimeError("failed")):
                    with patch.object(monitor, "fetch_stock", side_effect=[
                        monitor.Result("out_of_stock", "缺貨"),
                        monitor.Result("in_stock", "可買", "商品")
                    ]):
                        monitor.check_all(cfg, Mock(), use_browser=False)
                        monitor.check_all(cfg, Mock(), use_browser=False)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(next(iter(saved.values())), "out_of_stock")


if __name__ == "__main__":
    unittest.main()
