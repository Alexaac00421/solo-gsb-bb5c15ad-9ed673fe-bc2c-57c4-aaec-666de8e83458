"""借展车次（整车多展品）端到端测试（需 PostgreSQL，由 DATABASE_URL 指定）。

覆盖：
- 全数通过：同一事务为各件写既有批次与原交接哈希链，返回车次号与各批次报告；
  整车批次可经原单件接口查询、补正、撤回，三链审计通过
- 任一件失败：整车 REJECTED、保存拒绝报告、各件逐条结果、整车首个断点；
  不写任何批次/记录、不推进任何展品状态
- 展品不存在 -> 整单 404，不写任何数据（幂等键可被新单使用）
- 格式非法：件数不在 2..20、展品编号重复、记录字段非法、缺幂等头 -> 422
- 幂等：同键同载荷（含 REJECTED）重放原报告（200, idempotent_replay=true）；
  同键异载荷 409
- 并发：重叠车次同时提交 / 车次与单件提交并发，绝不出现「部分入链」——
  同一整车单涉及的各件要么全部入链，要么全部不入链
- 撤回后有效状态：整车各件核验以撤回后有效状态为参照

数据库不可达时整个模块自动 skip：

    DATABASE_URL='postgresql://app:app@localhost:5432/seals' \
        python3 -m pytest tests/test_api_vehicle_orders.py
"""
from __future__ import annotations

import os
import threading
import unittest
import uuid

import psycopg

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dev 依赖缺失
    TestClient = None

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://app:app@localhost:5432/seals"
)


def _db_available() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


D1 = "sha256:" + "ab" * 32
D2 = "sha256:" + "cd" * 32
D3 = "sha256:" + "ef" * 32
D4 = "sha256:" + "12" * 32
DBAD = "sha256:" + "34" * 32


def _leg(exhibit_id: str, frm: str, to: str, digest, when, *, home=None,
         seal="SEAL-1", seal_change_reason=None):
    rec = {
        "location": home or "展厅", "from_person": frm, "to_person": to,
        "seal_no": seal, "occurred_at": when, "voucher_digest": digest,
    }
    if seal_change_reason:
        rec["seal_change_reason"] = seal_change_reason
    return rec


def _vehicle(items):
    """items: [(exhibit_id, [record, ...]), ...]"""
    return {"items": [{"exhibit_id": eid, "records": recs} for eid, recs in items]}


@unittest.skipUnless(TestClient, "fastapi/httpx 未安装")
@unittest.skipUnless(_db_available(), "PostgreSQL 不可达，跳过端到端测试")
class TestVehicleOrdersApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app.db as db
        db.DATABASE_URL = DATABASE_URL
        import app.main as main
        main.DATABASE_URL = DATABASE_URL
        cls.main = main
        cls.client_cm = TestClient(main.app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def setUp(self):
        self.ids = [f"EX-VH-{uuid.uuid4().hex[:10]}" for _ in range(4)]
        for ex_id in self.ids:
            r = self.client.post("/api/v1/exhibits", json={
                "exhibit_id": ex_id, "name": "车次测试展品",
                "home_location": "库房A", "custodian": "张伟",
                "initial_seal": "SEAL-1",
            })
            self.assertEqual(r.status_code, 201, r.text)
        self.a, self.b, self.c, self.d = self.ids

    # -- 辅助 ----------------------------------------------------------------

    def _state(self, ex_id):
        r = self.client.get(f"/api/v1/exhibits/{ex_id}")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _batches(self, ex_id):
        return self.client.get(f"/api/v1/exhibits/{ex_id}/batches").json()

    def _vehicle(self, items, key=None):
        return self.client.post(
            "/api/v1/vehicle-handover-orders",
            json=_vehicle(items),
            headers={"Idempotency-Key": key or f"v-{uuid.uuid4()}"},
        )

    def _single(self, ex_id, rec, key=None):
        return self.client.post(
            "/api/v1/handover-batches",
            json={"exhibit_id": ex_id, "records": [rec]},
            headers={"Idempotency-Key": key or f"k-{uuid.uuid4()}"},
        )

    def _first_leg(self, ex_id, digest=D1, when="2026-09-01T09:00:00+08:00"):
        return _leg(ex_id, "张伟", "李静", digest, when, home="库房A")

    def _second_leg(self, ex_id, digest=D2, when="2026-09-02T09:00:00+08:00"):
        return _leg(ex_id, "李静", "王强", digest, when)

    # -- 全数通过 -------------------------------------------------------------

    def test_vehicle_accepts_all_and_writes_batches_in_one_transaction(self):
        r = self._vehicle([
            (self.a, [self._first_leg(self.a, D1)]),
            (self.b, [self._first_leg(self.b, D2)]),
        ])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["item_count"], 2)
        self.assertIsNone(body["first_break"])
        self.assertIsNone(body["vehicle_first_break"])
        order_id = body["vehicle_order_id"]
        batch_ids = []
        for item, ex_id in zip(body["items"], [self.a, self.b]):
            self.assertEqual(item["exhibit_id"], ex_id)
            self.assertEqual(item["status"], "ACCEPTED")
            self.assertTrue(all(it["ok"] for it in item["items"]))
            self.assertIsNotNone(item["batch_id"])
            batch_ids.append(item["batch_id"])

        # 各件链均推进一环
        self.assertEqual(self._state(self.a)["chain_length"], 1)
        self.assertEqual(self._state(self.b)["chain_length"], 1)

        # 车次号可查询，内容与首次一致
        g = self.client.get(f"/api/v1/vehicle-handover-orders/{order_id}")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()["vehicle_order_id"], order_id)

        # 各件批次可经原单件接口查询，批次历史回带车次号
        for ex_id, bid in zip([self.a, self.b], batch_ids):
            br = self.client.get(f"/api/v1/handover-batches/{bid}")
            self.assertEqual(br.status_code, 200)
            self.assertEqual(br.json()["status"], "ACCEPTED")
            summaries = self._batches(ex_id)
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["vehicle_order_id"], order_id)

        # 哈希链：prev_hash 接 GENESIS，且审计通过
        for ex_id in [self.a, self.b]:
            chain = self.client.get(f"/api/v1/exhibits/{ex_id}/handovers").json()
            self.assertEqual(chain["chain_length"], 1)
            self.assertEqual(chain["records"][0]["prev_hash"], "GENESIS")
            audit = self.client.get(f"/api/v1/exhibits/{ex_id}/audit/verify").json()
            self.assertTrue(audit["valid"], audit["detail"])

    def test_vehicle_multi_record_per_exhibit_chain_hashes_continue(self):
        r = self._vehicle([
            (self.a, [self._first_leg(self.a, D1), self._second_leg(self.a, D2)]),
            (self.b, [self._first_leg(self.b, D3)]),
        ])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "ACCEPTED")
        self.assertEqual(self._state(self.a)["chain_length"], 2)
        self.assertEqual(self._state(self.b)["chain_length"], 1)
        chain = self.client.get(f"/api/v1/exhibits/{self.a}/handovers").json()
        self.assertEqual(chain["records"][1]["prev_hash"],
                         chain["records"][0]["record_hash"])

    # -- 任一件失败：整车拒绝、零写入 -----------------------------------------

    def test_one_item_fails_whole_order_rejected_without_any_write(self):
        bad = _leg(self.b, "陌生人", "李静", "not-a-digest",
                   "2026-09-01T09:00:00+08:00", home="别处")
        key = f"v-{uuid.uuid4()}"
        r = self._vehicle([
            (self.a, [self._first_leg(self.a, D1)]),
            (self.b, [bad]),
        ], key=key)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "REJECTED")
        self.assertEqual(body["vehicle_first_break"]["exhibit_id"], self.b)
        self.assertEqual(body["vehicle_first_break"]["seq"], 1)
        item_a, item_b = body["items"]
        self.assertEqual(item_a["status"], "ACCEPTED")
        self.assertIsNone(item_a["batch_id"])          # 即使该件通过也不产生批次
        self.assertEqual(item_b["status"], "REJECTED")
        self.assertFalse(item_b["items"][0]["ok"])
        self.assertTrue(len(item_b["items"][0]["reasons"]) >= 1)

        # 不写批次/记录、不推进任何展品状态
        self.assertEqual(self._state(self.a)["chain_length"], 0)
        self.assertEqual(self._state(self.b)["chain_length"], 0)
        self.assertEqual(self._batches(self.a), [])
        self.assertEqual(self._batches(self.b), [])

        # 拒绝报告持久化：GET 车次号可取回
        g = self.client.get(
            f"/api/v1/vehicle-handover-orders/{body['vehicle_order_id']}")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()["status"], "REJECTED")

    def test_rejected_order_same_key_same_payload_replays(self):
        bad = _leg(self.b, "陌生人", "李静", D1,
                   "2026-09-01T09:00:00+08:00", home="别处")
        key = f"v-{uuid.uuid4()}"
        first = self._vehicle([
            (self.a, [self._first_leg(self.a, D1)]),
            (self.b, [bad]),
        ], key=key)
        self.assertEqual(first.status_code, 200)
        oid = first.json()["vehicle_order_id"]

        replay = self._vehicle([
            (self.a, [self._first_leg(self.a, D1)]),
            (self.b, [bad]),
        ], key=key)
        self.assertEqual(replay.status_code, 200)
        rb = replay.json()
        self.assertTrue(rb["idempotent_replay"])
        self.assertEqual(rb["vehicle_order_id"], oid)
        self.assertEqual(rb["status"], "REJECTED")
        self.assertEqual(self._state(self.a)["chain_length"], 0)
        self.assertEqual(self._state(self.b)["chain_length"], 0)

    # -- 展品不存在 / 格式 ---------------------------------------------------

    def test_unknown_exhibit_whole_order_404_and_no_write(self):
        missing = f"EX-NOPE-{uuid.uuid4().hex[:10]}"
        key = f"v-{uuid.uuid4()}"
        r = self._vehicle([
            (self.a, [self._first_leg(self.a, D1)]),
            (missing, [self._first_leg(missing, D2)]),
        ], key=key)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self._state(self.a)["chain_length"], 0)

        # 404 不落整车单：同一幂等键随后用于合法整车单，按首次请求处理（非重放）
        ok = self._vehicle([
            (self.a, [self._first_leg(self.a, D1)]),
            (self.b, [self._first_leg(self.b, D2)]),
        ], key=key)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertFalse(ok.json()["idempotent_replay"])
        self.assertEqual(ok.json()["status"], "ACCEPTED")

    def test_payload_validation(self):
        good = lambda eid: self._first_leg(eid, D1)  # noqa: E731

        # 仅 1 件
        r1 = self._vehicle([(self.a, [good(self.a)])])
        self.assertEqual(r1.status_code, 422)
        # 21 件（展品无需存在：件数 422 先于展品存在性 404 判定）
        many = [
            (f"EX-VX-{i:02d}", [{
                "location": "库房A", "from_person": "张伟", "to_person": "李静",
                "seal_no": "SEAL-1", "occurred_at": "2026-09-01T09:00:00+08:00",
                "voucher_digest": D1,
            }])
            for i in range(21)
        ]
        r2 = self.client.post("/api/v1/vehicle-handover-orders", json=_vehicle(many))
        self.assertEqual(r2.status_code, 422)
        # 展品编号重复
        r3 = self._vehicle([
            (self.a, [good(self.a)]),
            (self.a, [good(self.a)]),
        ])
        self.assertEqual(r3.status_code, 422)
        # 记录字段非法（时间不带时区）
        bad_rec = dict(good(self.a))
        bad_rec["occurred_at"] = "2026-09-01T09:00:00"
        r4 = self._vehicle([
            (self.a, [bad_rec]),
            (self.b, [good(self.b)]),
        ])
        self.assertEqual(r4.status_code, 422)
        # 缺幂等头
        r5 = self.client.post("/api/v1/vehicle-handover-orders",
                              json=_vehicle([(self.a, [good(self.a)]),
                                            (self.b, [good(self.b)])]))
        self.assertEqual(r5.status_code, 422)
        # 全部 422 均零写入
        for ex_id in [self.a, self.b]:
            self.assertEqual(self._state(ex_id)["chain_length"], 0)

    # -- 幂等冲突 -------------------------------------------------------------

    def test_same_key_different_payload_is_409(self):
        key = f"v-{uuid.uuid4()}"
        first = self._vehicle([
            (self.a, [self._first_leg(self.a, D1)]),
            (self.b, [self._first_leg(self.b, D2)]),
        ], key=key)
        self.assertEqual(first.status_code, 200)
        # 同键，第二件换一条记录 -> 409
        conflict = self._vehicle([
            (self.a, [self._first_leg(self.a, D1)]),
            (self.b, [self._first_leg(self.b, D3)]),
        ], key=key)
        self.assertEqual(conflict.status_code, 409)
        # GET 未知车次 -> 404
        r = self.client.get(f"/api/v1/vehicle-handover-orders/{uuid.uuid4()}")
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/v1/vehicle-handover-orders/not-a-uuid")
        self.assertEqual(r.status_code, 422)

    # -- 并发：不得部分入链 ---------------------------------------------------

    def test_concurrent_overlapping_vehicle_orders_never_partial(self):
        """两件重叠展品的两个首程车次并发：只可能一单一车整体受理，
        另一整车整体拒绝；任何一件都不得单独入链。"""
        barrier = threading.Barrier(2)
        outcomes = {}

        def fire(tag, digest):
            barrier.wait()
            outcomes[tag] = self._vehicle([
                (self.a, [self._first_leg(self.a, digest,
                                          when="2026-09-01T09:00:00+08:00")]),
                (self.b, [self._first_leg(self.b, D3,
                                          when="2026-09-01T09:00:00+08:00")]),
            ])

        t1 = threading.Thread(target=fire, args=("o1", D1))
        t2 = threading.Thread(target=fire, args=("o2", D2))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = {tag: resp.status_code for tag, resp in outcomes.items()}
        self.assertEqual(statuses, {"o1": 200, "o2": 200}, statuses)
        bodies = {tag: resp.json() for tag, resp in outcomes.items()}
        verdicts = sorted(b["status"] for b in bodies.values())
        self.assertEqual(verdicts, ["ACCEPTED", "REJECTED"], bodies)
        accepted = next(b for b in bodies.values() if b["status"] == "ACCEPTED")
        rejected = next(b for b in bodies.values() if b["status"] == "REJECTED")
        # 受理单两件都有批次；拒绝单两件都无批次
        self.assertTrue(all(it["batch_id"] for it in accepted["items"]))
        self.assertTrue(all(it["batch_id"] is None for it in rejected["items"]))

        # 关键不变量：两件展品链长始终相等（整车同时推进），记录总数为 2
        n_a = self._state(self.a)["chain_length"]
        n_b = self._state(self.b)["chain_length"]
        self.assertEqual(n_a, n_b)
        self.assertEqual(n_a, 1)
        # 各只有一个批次
        self.assertEqual(len(self._batches(self.a)), 1)
        self.assertEqual(len(self._batches(self.b)), 1)

        # 下一段整车（两件同时李静->王强）可整体受理，链长同步到 2
        nxt = self._vehicle([
            (self.a, [self._second_leg(self.a, D1,
                                       when="2026-09-02T09:00:00+08:00")]),
            (self.b, [self._second_leg(self.b, D2,
                                       when="2026-09-02T09:00:00+08:00")]),
        ])
        self.assertEqual(nxt.status_code, 200, nxt.text)
        self.assertEqual(nxt.json()["status"], "ACCEPTED")
        self.assertEqual(self._state(self.a)["chain_length"], 2)
        self.assertEqual(self._state(self.b)["chain_length"], 2)
        for ex_id in [self.a, self.b]:
            audit = self.client.get(f"/api/v1/exhibits/{ex_id}/audit/verify").json()
            self.assertTrue(audit["valid"], audit["detail"])

    def test_concurrent_vehicle_vs_single_submission_never_partial(self):
        """整车（C+D 首程）与单件（C 首程）并发：

        整车胜出 -> C、D 链长都为 1，单件被拒；
        单件胜出 -> C 链长 1、D 链长 0，整车整体被拒；
        严禁出现 C 未入链而 D 入链（整车部分入链）。
        """
        barrier = threading.Barrier(2)
        results = {}

        def fire_vehicle():
            barrier.wait()
            results["vehicle"] = self._vehicle([
                (self.c, [self._first_leg(self.c, D1,
                                          when="2026-09-01T09:00:00+08:00")]),
                (self.d, [self._first_leg(self.d, D2,
                                          when="2026-09-01T09:00:00+08:00")]),
            ])

        def fire_single():
            barrier.wait()
            results["single"] = self._single(
                self.c, self._first_leg(self.c, D3,
                                        when="2026-09-01T09:00:00+08:00"))

        t1 = threading.Thread(target=fire_vehicle)
        t2 = threading.Thread(target=fire_single)
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(results["vehicle"].status_code, 200, results["vehicle"].text)
        self.assertEqual(results["single"].status_code, 200, results["single"].text)
        n_c = self._state(self.c)["chain_length"]
        n_d = self._state(self.d)["chain_length"]
        # 合法终态集合：整车赢 (1,1) 或单件赢 (1,0)；绝不允许 (0,1)
        self.assertIn((n_c, n_d), {(1, 1), (1, 0)}, (n_c, n_d))
        if (n_c, n_d) == (1, 1):
            self.assertEqual(results["vehicle"].json()["status"], "ACCEPTED")
            self.assertEqual(results["single"].json()["status"], "REJECTED")
        else:
            self.assertEqual(results["vehicle"].json()["status"], "REJECTED")
            self.assertEqual(results["single"].json()["status"], "ACCEPTED")
            self.assertEqual(len(self._batches(self.d)), 0)

    # -- 与既有补正/撤回/审计兼容 ---------------------------------------------

    def test_vehicle_batches_support_correction_withdrawal_and_audit(self):
        r = self._vehicle([
            (self.a, [self._first_leg(self.a, D1), self._second_leg(self.a, D2)]),
            (self.b, [self._first_leg(self.b, D3)]),
        ])
        body = r.json()
        self.assertEqual(body["status"], "ACCEPTED")
        batch_a = next(it for it in body["items"] if it["exhibit_id"] == self.a)
        batch_b = next(it for it in body["items"] if it["exhibit_id"] == self.b)
        self.assertEqual(batch_a["batch_id"], self._batches(self.a)[0]["batch_id"])

        # 撤回 a 的整车批次（最新已受理、未被补正引用），不影响 b
        wd = self.client.post(
            f"/api/v1/exhibits/{self.a}/batch-withdrawals",
            json={"batch_id": batch_a["batch_id"], "reason": "整车批次误登记，撤回"},
            headers={"Idempotency-Key": f"w-{uuid.uuid4()}"},
        )
        self.assertEqual(wd.status_code, 201, wd.text)
        self.assertEqual(wd.json()["effective_chain_length"], 0)
        self.assertEqual(self._state(self.a)["chain_length"], 2)      # 原链历史不变
        self.assertEqual(self._state(self.a)["effective_chain_length"], 0)
        self.assertEqual(self._state(self.b)["chain_length"], 1)      # b 不受影响

        # b 的整车批次记录可补正（补正不推进交接链；该批次此后不可再撤回——既有互斥规则）
        corr = self.client.post(
            f"/api/v1/exhibits/{self.b}/handovers/1/digest-corrections",
            json={"expected_digest": D3, "new_digest": D4,
                  "reason": "整车批次凭证摘要笔误，补正"},
            headers={"Idempotency-Key": f"c-{uuid.uuid4()}"},
        )
        self.assertEqual(corr.status_code, 201, corr.text)

        # 撤回后再以有效状态（登记保管人张伟）整车提交：a 重走首程、b 续接第二程
        again = self._vehicle([
            (self.a, [self._first_leg(self.a, DBAD,
                                      when="2026-09-10T09:00:00+08:00")]),
            (self.b, [self._second_leg(self.b, D1,
                                       when="2026-09-10T10:00:00+08:00")]),
        ])
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["status"], "ACCEPTED")
        self.assertEqual(self._state(self.a)["effective_chain_length"], 1)
        self.assertEqual(self._state(self.b)["effective_chain_length"], 2)

        # 三链审计：a（撤回 + 原链物理追加）与 b（补正）全部通过
        for ex_id in [self.a, self.b]:
            audit = self.client.get(f"/api/v1/exhibits/{ex_id}/audit/verify").json()
            self.assertTrue(audit["valid"], audit["detail"])


if __name__ == "__main__":
    unittest.main()
