"""M3-1：任务队列、租约、重试、水位、worker 与入队分级。"""

from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from unittest import mock

from app.core.config import get_settings
from app.memory import chat_bridge
from app.memory.worker import JobRetry, MemoryJobWorker
from app.routers import memory as memory_api
from app.routers import sessions as sessions_api
from tests.memory_fixtures import memory_store


def _cfg(**overrides):
    base = replace(
        get_settings(),
        memory_enabled=True,
        memory_context_enabled=True,
        memory_fast_memory=True,
        memory_auto_extract=True,
        memory_worker=True,
        memory_extract_every_turns=6,
        memory_extract_max_messages=20,
        memory_extract_max_input_tokens=4000,
        memory_extract_backend="auto",
        memory_cloud_extract=False,
        memory_extract_debounce_seconds=300,
    )
    return replace(base, **overrides)


def _persistence(store, session_id, question, *, request_id="r1"):
    user = store.add_user_message(session_id, question)
    assistant = store.add_assistant_message(
        session_id, content="好的", status="complete"
    )
    return chat_bridge.ChatPersistence(
        store=store,
        session_id=session_id,
        user_message_id=user["id"],
        assistant_message_id=assistant["id"],
        request_id=request_id,
        question=question,
    )


class JobQueueTests(unittest.TestCase):
    def test_enqueue_claim_complete(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory",
                session_id=session["id"],
                payload={"reason": "test"},
                dedupe_key="k1",
            )
            self.assertEqual(job["status"], "queued")
            claimed = store.jobs.claim(worker_id="w1", lease_seconds=30)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["status"], "running")
            self.assertEqual(claimed["attempts"], 1)
            self.assertEqual(claimed["payload"]["reason"], "test")
            done = store.jobs.complete(
                claimed["id"], result={"ok": True}, worker_id="w1"
            )
            self.assertEqual(done["status"], "done")
            self.assertTrue(done["result"]["ok"])
            self.assertIsNone(store.jobs.claim(worker_id="w2"))

    def test_active_dedupe_updates_instead_of_inserting(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            first = store.jobs.enqueue(
                kind="extract_memory",
                session_id=session["id"],
                dedupe_key="same",
                priority=7,
                delay_seconds=300,
                payload={"a": 1},
            )
            second = store.jobs.enqueue(
                kind="extract_memory",
                session_id=session["id"],
                dedupe_key="same",
                priority=1,
                delay_seconds=0,
                payload={"b": 2},
            )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["priority"], 1)
            self.assertEqual(second["payload"], {"a": 1, "b": 2})
            self.assertEqual(
                len(store.jobs.list_jobs(kind="extract_memory")), 1
            )

    def test_fail_retries_then_fails(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"], max_attempts=2
            )
            claimed = store.jobs.claim(worker_id="w1")
            retried = store.jobs.fail(
                claimed["id"],
                "Timeout",
                retryable=True,
                retry_delay_seconds=0,
                worker_id="w1",
            )
            self.assertEqual(retried["status"], "queued")
            self.assertEqual(retried["attempts"], 1)
            claimed2 = store.jobs.claim(worker_id="w1")
            self.assertEqual(claimed2["attempts"], 2)
            failed = store.jobs.fail(
                claimed2["id"],
                "Timeout",
                retryable=True,
                retry_delay_seconds=0,
                worker_id="w1",
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["last_error"], "Timeout")

    def test_non_retryable_marks_failed(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(kind="extract_memory", session_id=session["id"])
            claimed = store.jobs.claim(worker_id="w1")
            failed = store.jobs.fail(
                claimed["id"], "bad_json", retryable=False, worker_id="w1"
            )
            self.assertEqual(failed["status"], "failed")

    def test_recover_stale_lease_requeues(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(kind="extract_memory", session_id=session["id"])
            store.jobs.claim(worker_id="w1", lease_seconds=1)
            recovered = store.jobs.recover_stale(now="9999-01-01T00:00:00.000Z")
            self.assertEqual(recovered, 1)
            queued = store.jobs.get(job["id"])
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(queued["last_error"], "lease_expired")
            self.assertEqual(store.jobs.claim(worker_id="w2")["attempts"], 2)

    def test_recover_stale_exhausted_marks_failed(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"], max_attempts=1
            )
            store.jobs.claim(worker_id="w1", lease_seconds=1)
            store.jobs.recover_stale(now="9999-01-01T00:00:00.000Z")
            failed = store.jobs.get(job["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["last_error"], "lease_expired")

    def test_watermark_is_monotonic(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            for text in ("第一句", "第二句", "第三句"):
                store.add_user_message(session["id"], text)
            self.assertEqual(
                store.jobs.count_user_messages_since_watermark(session["id"]), 3
            )
            self.assertEqual(store.jobs.set_watermark(session["id"], 2), 2)
            self.assertEqual(
                store.jobs.count_user_messages_since_watermark(session["id"]), 1
            )
            self.assertEqual(store.jobs.set_watermark(session["id"], 1), 2)
            self.assertEqual(store.jobs.get_watermark(session["id"]), 2)

    def test_soft_delete_cancels_queued_jobs(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"]
            )
            store.delete_session(session["id"])
            self.assertEqual(store.jobs.get(job["id"])["status"], "cancelled")


class EnqueueIntegrationTests(unittest.TestCase):
    def test_explicit_marker_gets_priority_one(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            persistence = _persistence(
                store, session["id"], "记住：我的项目代号是 A3"
            )
            with mock.patch(
                "app.memory.chat_bridge.get_settings", return_value=_cfg()
            ):
                self.assertTrue(chat_bridge.enqueue_extraction(persistence))
            job = store.jobs.list_jobs(kind="extract_memory")[0]
            self.assertEqual(job["priority"], 1)
            self.assertEqual(job["payload"]["reason"], "explicit")

    def test_turn_threshold_gets_priority_three(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            for index in range(6):
                store.add_user_message(session["id"], f"普通陈述 {index}")
            persistence = _persistence(store, session["id"], "普通陈述 6")
            with mock.patch(
                "app.memory.chat_bridge.get_settings", return_value=_cfg()
            ):
                self.assertTrue(chat_bridge.enqueue_extraction(persistence))
            job = store.jobs.list_jobs(kind="extract_memory")[0]
            self.assertEqual(job["priority"], 3)
            self.assertEqual(job["payload"]["reason"], "turn_threshold")

    def test_normal_chat_is_debounced(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            persistence = _persistence(store, session["id"], "今天天气怎么样")
            with mock.patch(
                "app.memory.chat_bridge.get_settings", return_value=_cfg()
            ):
                self.assertTrue(chat_bridge.enqueue_extraction(persistence))
            job = store.jobs.list_jobs(kind="extract_memory")[0]
            self.assertEqual(job["priority"], 7)
            self.assertEqual(job["payload"]["reason"], "debounce")

    def test_auto_extract_off_does_not_enqueue(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            persistence = _persistence(store, session["id"], "记住：项目代号 A3")
            with mock.patch(
                "app.memory.chat_bridge.get_settings",
                return_value=_cfg(memory_auto_extract=False),
            ):
                self.assertFalse(chat_bridge.enqueue_extraction(persistence))
            self.assertEqual(store.jobs.list_jobs(kind="extract_memory"), [])

    def test_end_session_queues_when_enabled(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            with mock.patch(
                "app.routers.sessions.get_settings", return_value=_cfg()
            ):
                result = sessions_api.end_session(session["id"])
            self.assertTrue(result["extraction_queued"])
            jobs = store.jobs.list_jobs(kind="extract_memory")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["priority"], 1)
            self.assertEqual(jobs[0]["payload"]["reason"], "session_end")


class WorkerTests(unittest.TestCase):
    def test_effective_lease_covers_llm_timeout(self) -> None:
        worker = MemoryJobWorker(lease_seconds=120)
        self.assertEqual(
            worker.effective_lease_seconds(_cfg(llm_timeout=300)), 360
        )
        self.assertEqual(
            worker.effective_lease_seconds(_cfg(llm_timeout=120)), 180
        )

    def test_start_applies_effective_lease(self) -> None:
        with memory_store():
            worker = MemoryJobWorker(worker_id="w-lease", poll_seconds=0.05)
            with mock.patch(
                "app.memory.worker.get_settings",
                return_value=_cfg(llm_timeout=300),
            ):
                self.assertTrue(worker.start())
                try:
                    self.assertGreaterEqual(worker.lease_seconds, 360)
                finally:
                    worker.stop()

    def test_worker_runs_registered_handler(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"]
            )
            worker = MemoryJobWorker(worker_id="w-test", poll_seconds=0.05)
            seen = []
            worker.register(
                "extract_memory",
                lambda item: seen.append(item["id"]) or {"ok": True},
            )
            with mock.patch(
                "app.memory.worker.get_settings", return_value=_cfg()
            ):
                self.assertTrue(worker.run_once())
            self.assertEqual(seen, [job["id"]])
            done = store.jobs.get(job["id"])
            self.assertEqual(done["status"], "done")
            self.assertTrue(done["result"]["ok"])

    def test_worker_without_handler_is_idle(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"]
            )
            worker = MemoryJobWorker(worker_id="w-idle")
            with mock.patch(
                "app.memory.worker.get_settings", return_value=_cfg()
            ):
                self.assertFalse(worker.run_once())
            self.assertEqual(store.jobs.get(job["id"])["status"], "queued")

    def test_handler_error_is_retried(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"]
            )
            worker = MemoryJobWorker(worker_id="w-err")

            def boom(_item):
                raise RuntimeError("boom")

            worker.register("extract_memory", boom)
            with mock.patch(
                "app.memory.worker.get_settings", return_value=_cfg()
            ):
                self.assertTrue(worker.run_once())
            failed = store.jobs.get(job["id"])
            self.assertEqual(failed["status"], "queued")
            self.assertEqual(failed["last_error"], "RuntimeError")

    def test_job_retry_can_disable_retry(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"]
            )
            worker = MemoryJobWorker(worker_id="w-retry")

            def stop(_item):
                raise JobRetry("bad", retryable=False)

            worker.register("extract_memory", stop)
            with mock.patch(
                "app.memory.worker.get_settings", return_value=_cfg()
            ):
                self.assertTrue(worker.run_once())
            self.assertEqual(store.jobs.get(job["id"])["status"], "failed")

    def test_worker_start_requires_auto_extract(self) -> None:
        worker = MemoryJobWorker(worker_id="w-off")
        with mock.patch(
            "app.memory.worker.get_settings",
            return_value=_cfg(memory_auto_extract=False),
        ):
            self.assertFalse(worker.start())

    def test_running_job_gets_follow_up_when_new_enqueue_arrives(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            original = store.jobs.enqueue(
                kind="extract_memory",
                session_id=session["id"],
                dedupe_key="follow",
                payload={"round": 1},
            )
            worker = MemoryJobWorker(worker_id="w-follow")

            def handler(_item):
                # 模拟任务运行期间又有新消息入队。
                store.jobs.enqueue(
                    kind="extract_memory",
                    session_id=session["id"],
                    dedupe_key="follow",
                    payload={"round": 2},
                    priority=1,
                )
                return {"ok": True}

            worker.register("extract_memory", handler)
            with mock.patch(
                "app.memory.worker.get_settings", return_value=_cfg()
            ):
                self.assertTrue(worker.run_once())
            self.assertEqual(store.jobs.get(original["id"])["status"], "done")
            queued = store.jobs.list_jobs(kind="extract_memory", status="queued")
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]["payload"]["round"], 2)
            self.assertNotIn("_follow_up_requested", queued[0]["payload"])
            self.assertEqual(queued[0]["priority"], 1)

    def test_worker_thread_processes_job(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.jobs.enqueue(kind="extract_memory", session_id=session["id"])
            worker = MemoryJobWorker(
                worker_id="w-thread", poll_seconds=0.05, lease_seconds=5
            )
            done = threading.Event()

            def handler(_item):
                done.set()
                return {"ok": True}

            worker.register("extract_memory", handler)
            with mock.patch(
                "app.memory.worker.get_settings", return_value=_cfg()
            ):
                self.assertTrue(worker.start())
                self.assertTrue(done.wait(2.0))
                worker.stop()
            self.assertFalse(worker.running())
            jobs = store.jobs.list_jobs(kind="extract_memory")
            self.assertEqual(jobs[0]["status"], "done")


class JobsApiTests(unittest.TestCase):
    def test_jobs_api_lists_and_retries(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"]
            )
            listed = memory_api.list_jobs(
                status=None, kind=None, session_id=None, limit=50, offset=0
            )
            self.assertTrue(listed["enabled"])
            self.assertEqual([item["id"] for item in listed["items"]], [job["id"]])
            self.assertEqual(listed["counts"]["queued"], 1)

            store.jobs.claim(worker_id="w1")
            store.jobs.fail(job["id"], "boom", retryable=False, worker_id="w1")
            retried = memory_api.retry_job(job["id"])
            self.assertEqual(retried["job"]["status"], "queued")

    def test_status_contains_job_counts(self) -> None:
        with memory_store() as (store, _):
            store.create_session()
            status = store.status()
            self.assertIn("jobs", status)
            self.assertEqual(status["jobs"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
