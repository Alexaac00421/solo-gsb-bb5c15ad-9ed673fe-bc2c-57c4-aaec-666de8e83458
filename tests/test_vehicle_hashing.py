"""借展车次（整车多展品）请求指纹单测（纯标准库，无数据库依赖）。"""
from __future__ import annotations

import unittest

from app.hashing import vehicle_order_request_hash


def _item(exhibit_id: str, digest_suffix: str = "ab") -> dict:
    return {
        "exhibit_id": exhibit_id,
        "records": [
            {
                "location": "库房A",
                "from_person": "张伟",
                "to_person": "李静",
                "seal_no": "SEAL-1",
                "occurred_at": "2026-09-01T09:00:00+08:00",
                "voucher_digest": "sha256:" + digest_suffix * 32,
                "seal_change_reason": None,
            }
        ],
    }


class TestVehicleOrderRequestHash(unittest.TestCase):
    def test_stable_for_same_payload(self):
        payload = [_item("EX-0001"), _item("EX-0002")]
        self.assertEqual(
            vehicle_order_request_hash(payload),
            vehicle_order_request_hash([dict(_item("EX-0001")), dict(_item("EX-0002"))]),
        )

    def test_64_hex_digest(self):
        h = vehicle_order_request_hash([_item("EX-0001"), _item("EX-0002")])
        self.assertRegex(h, r"^[0-9a-f]{64}$")

    def test_sensitive_to_exhibit_set(self):
        base = vehicle_order_request_hash([_item("EX-0001"), _item("EX-0002")])
        changed = vehicle_order_request_hash([_item("EX-0001"), _item("EX-0003")])
        self.assertNotEqual(base, changed)

    def test_sensitive_to_item_order(self):
        base = vehicle_order_request_hash([_item("EX-0001"), _item("EX-0002")])
        reordered = vehicle_order_request_hash([_item("EX-0002"), _item("EX-0001")])
        self.assertNotEqual(base, reordered)

    def test_sensitive_to_record_field(self):
        base = vehicle_order_request_hash([_item("EX-0001", "ab"), _item("EX-0002", "cd")])
        changed = vehicle_order_request_hash([_item("EX-0001", "ab"), _item("EX-0002", "ef")])
        self.assertNotEqual(base, changed)


if __name__ == "__main__":
    unittest.main()
