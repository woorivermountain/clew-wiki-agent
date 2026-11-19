import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from clew_okf.runtime.jobs import JobStore, enqueue_in_transaction


class Clock:
    def __init__(self):
        self.value = 1_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state/jobs.sqlite"
        self.clock = Clock()
        self.store = JobStore(self.path, now=self.clock)

    def enqueue(self, key="job", kind="build", **payload):
        return self.store.enqueue(kind, payload, dedupe_key=key)

    def claim(self, owner="worker"):
        claimed = self.store.claim(owner)
        self.assertIsNotNone(claimed)
        return claimed

    def finish(self, job, status="succeeded"):
        self.assertTrue(self.store.finish(job["id"], job["lease_token"], status=status, result={"done": True}))

    def test_durable_idempotence_and_private_lease_visibility(self):
        first = self.store.enqueue("report", {"report_id": "R1", "options": {"b": 2, "a": 1}}, dedupe_key="report:R1")
        restarted = JobStore(self.path, now=self.clock)
        again = restarted.enqueue("report", {"options": {"a": 1, "b": 2}, "report_id": "R1"}, dedupe_key="report:R1")
        self.assertEqual(again, first)
        with self.assertRaises(ValueError):
            restarted.enqueue("report", {"report_id": "R2"}, dedupe_key="report:R1")
        self.assertEqual(len(restarted.list_jobs()), 1)
        running = restarted.claim("restarted")
        self.assertIn("lease_token", running)
        self.assertNotIn("lease_token", restarted.get(running["id"]))
        self.assertNotIn("lease_token", restarted.list_jobs()[0])
        with restarted.transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_racing_workers_claim_only_one_job_for_the_whole_store(self):
        for index in range(8):
            self.enqueue(str(index), index=index)
        stores = [JobStore(self.path, now=self.clock) for _ in range(8)]
        barrier = Barrier(len(stores))

        def claim(pair):
            index, store = pair
            barrier.wait(timeout=10)
            return store.claim(f"worker-{index}")

        with ThreadPoolExecutor(max_workers=len(stores)) as workers:
            claims = list(workers.map(claim, enumerate(stores)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertEqual(sum(job["status"] == "running" for job in self.store.list_jobs()), 1)

    def test_expired_lease_cannot_renew_mutate_finish_or_defer_after_reclaim(self):
        queued = self.enqueue()
        old = self.claim()
        self.clock.advance(60)
        self.assertFalse(self.store.heartbeat(old["id"], old["lease_token"]))
        new = JobStore(self.path, now=self.clock).claim("recovery")
        self.assertEqual(new["id"], queued["id"])
        self.assertEqual(new["attempts"], 2)
        self.assertNotEqual(new["lease_token"], old["lease_token"])
        self.assertFalse(self.store.progress(old["id"], old["lease_token"], "publish", "stale"))
        self.assertFalse(self.store.finish(old["id"], old["lease_token"], status="succeeded"))
        self.assertFalse(self.store.defer(old["id"], old["lease_token"]))
        self.assertFalse(self.store.defer(old["id"], old["lease_token"], consume_attempt=False))
        self.assertEqual(self.store.get(new["id"])["attempts"], 2)
        self.assertEqual(self.store.get(new["id"])["status"], "running")
        self.finish(new)

    def test_crashed_job_has_bounded_attempts_and_does_not_block_next_job(self):
        first = self.enqueue("first")
        second = self.enqueue("second")
        for attempt in (1, 2, 3):
            claimed = self.claim(f"worker-{attempt}")
            self.assertEqual((claimed["id"], claimed["attempts"]), (first["id"], attempt))
            self.clock.advance(61)
        recovered = self.claim("next-job")
        self.assertEqual(recovered["id"], second["id"])
        failed = self.store.get(first["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "attempts_exhausted")
        self.assertIsNotNone(failed["finished_at"])

    def test_defer_respects_delay_and_eventually_finishes_at_retry_limit(self):
        queued = self.enqueue()
        for attempt in (1, 2, 3):
            running = self.claim()
            self.assertEqual(running["attempts"], attempt)
            self.assertTrue(self.store.defer(running["id"], running["lease_token"], delay_seconds=2))
            if attempt < 3:
                self.assertIsNone(self.store.claim("early"))
                self.clock.advance(2)
        self.assertEqual(self.store.get(queued["id"])["status"], "failed")
        self.assertIsNone(self.store.claim("after-limit"))

    def test_long_external_lock_waits_preserve_retry_quota(self):
        queued = self.enqueue()
        for _ in range(12):
            running = self.claim()
            self.assertEqual(running["attempts"], 1)
            self.assertTrue(self.store.defer(
                running["id"], running["lease_token"], delay_seconds=5, consume_attempt=False,
            ))
            waiting = self.store.get(queued["id"])
            self.assertEqual((waiting["status"], waiting["attempts"]), ("queued", 0))
            self.assertFalse(self.store.defer(
                running["id"], running["lease_token"], consume_attempt=False,
            ))
            self.assertEqual(self.store.get(queued["id"])["attempts"], 0)
            self.assertIsNone(self.store.claim("early"))
            self.clock.advance(5)

        for attempt in (1, 2):
            running = self.claim()
            self.assertEqual(running["attempts"], attempt)
            self.assertTrue(self.store.defer(running["id"], running["lease_token"], delay_seconds=0))
        at_cap = self.claim()
        self.assertEqual(at_cap["attempts"], 3)
        self.assertTrue(self.store.defer(
            at_cap["id"], at_cap["lease_token"], delay_seconds=5, consume_attempt=False,
        ))
        self.assertEqual(self.store.get(queued["id"])["attempts"], 2)
        self.clock.advance(5)
        final = self.claim()
        self.assertEqual(final["attempts"], 3)
        self.assertTrue(self.store.defer(final["id"], final["lease_token"]))
        self.assertEqual(self.store.get(queued["id"])["status"], "failed")

    def test_defer_consume_attempt_requires_exact_boolean(self):
        queued = self.enqueue()
        running = self.claim()
        before = self.store.get(queued["id"])
        for invalid in (None, "false", 0, 1, [], {}):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(ValueError, "consume_attempt must be a boolean"):
                    self.store.defer(running["id"], running["lease_token"], consume_attempt=invalid)
                self.assertEqual(self.store.get(queued["id"]), before)

    def test_heartbeat_progress_and_incremental_events(self):
        queued = self.enqueue()
        running = self.claim()
        self.clock.advance(30)
        self.assertTrue(self.store.heartbeat(running["id"], running["lease_token"]))
        self.clock.advance(40)
        self.assertIsNone(self.store.claim("other-worker"))
        self.assertTrue(self.store.progress(
            running["id"], running["lease_token"], "parsing", "Reading originals", {"scanned": 2, "processed": 1},
        ))
        self.assertEqual(self.store.get(queued["id"])["progress"], {"scanned": 2, "processed": 1})
        events = self.store.get_events(queued["id"])
        self.assertEqual([event["stage"] for event in events], ["queued", "running", "parsing"])
        self.assertEqual(self.store.get_events(queued["id"], after=events[-2]["id"]), events[-1:])
        self.finish(running)
        self.assertEqual(self.store.get_events(queued["id"])[-1]["status"], "succeeded")

    def test_waiting_parent_counts_children_and_failure_takes_precedence(self):
        parent = self.enqueue("bootstrap", kind="bootstrap")
        lease = self.claim()
        children = self.store.set_waiting(parent["id"], lease["lease_token"], result={"source_count": 2}, children=[
            {"kind": "report", "payload": {"report_id": "R1"}, "dedupe_key": "parent:R1"},
            {"kind": "report", "payload": {"report_id": "R2"}, "dedupe_key": "parent:R2"},
        ])
        waiting = self.store.get(parent["id"])
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["progress"], {"scanned": 2, "processed": 0, "total": 2})
        self.assertTrue(all(child["parent_id"] == parent["id"] for child in children))
        self.finish(self.claim(), "held")
        self.assertEqual(self.store.get(parent["id"])["progress"]["processed"], 1)
        self.assertEqual(self.store.get(parent["id"])["status"], "waiting")
        self.finish(self.claim(), "failed")
        complete = self.store.get(parent["id"])
        self.assertEqual(complete["status"], "failed")
        self.assertEqual(complete["result"]["source_count"], 2)
        self.assertEqual(complete["result"]["children"]["held"], 1)
        self.assertEqual(complete["result"]["children"]["failed"], 1)
        self.assertEqual(complete["progress"]["processed"], 2)

    def test_nested_waiting_parents_and_empty_child_batches_complete(self):
        parent = self.enqueue("root", kind="bootstrap")
        lease = self.claim()
        child = self.store.set_waiting(parent["id"], lease["lease_token"], result={"root": True}, children=[
            {"kind": "bootstrap", "payload": {}, "dedupe_key": "nested"},
        ])[0]
        nested = self.claim()
        self.assertEqual(nested["id"], child["id"])
        self.assertEqual(self.store.set_waiting(nested["id"], nested["lease_token"], result={}, children=[]), [])
        self.store.reconcile_parents()
        self.assertEqual(self.store.get(child["id"])["status"], "succeeded")
        self.assertEqual(self.store.get(parent["id"])["status"], "succeeded")

    def test_child_enqueue_conflict_rolls_back_entire_batch_and_parent_transition(self):
        parent = self.enqueue("parent", kind="bootstrap")
        lease = self.claim()
        with self.assertRaises(ValueError):
            self.store.set_waiting(parent["id"], lease["lease_token"], result={}, children=[
                {"kind": "report", "payload": {"id": "R1"}, "dedupe_key": "child"},
                {"kind": "report", "payload": {"id": "R2"}, "dedupe_key": "child"},
            ])
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertEqual(self.store.get(parent["id"])["status"], "running")
        self.assertTrue(self.store.heartbeat(parent["id"], lease["lease_token"]))

    def test_intake_state_and_enqueue_share_one_atomic_transaction(self):
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE example_intake(id TEXT PRIMARY KEY)")
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO example_intake VALUES ('R1')")
                enqueue_in_transaction(conn, "report", {"id": "R1"}, "atomic", now=self.clock())
                raise RuntimeError("rollback")
        self.assertEqual(self.store.list_jobs(), [])
        with self.store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM example_intake").fetchone()[0], 0)
            job = enqueue_in_transaction(conn, "report", {"id": "R1"}, "atomic", now=self.clock())
        self.assertEqual(self.store.get(job["id"])["status"], "queued")

    def test_invalid_data_cannot_create_jobs_or_release_live_lease(self):
        for payload in ({"not_json": float("nan")}, {1: "non-string key"}, {"nested": object()}):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(ValueError):
                    self.store.enqueue("build", payload, dedupe_key="invalid")
        self.assertEqual(self.store.list_jobs(), [])
        self.enqueue()
        running = self.claim()
        for status in ("queued", "waiting", "unknown"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    self.store.finish(running["id"], running["lease_token"], status=status)
        self.assertEqual(self.store.get(running["id"])["status"], "running")
        self.assertNotIn("lease_owner", json.dumps(self.store.get(running["id"])))


if __name__ == "__main__":
    unittest.main()
