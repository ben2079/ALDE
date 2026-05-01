from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PKG_ROOT = Path(__file__).resolve().parents[2]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from ALDE_Projekt.ALDE.alde.agents_desktop_runtime import (
    DesktopAgentRun,
    DesktopAgentRunFacadeService,
    DesktopAgentRunMonitorService,
    DesktopAgentRunPersistenceService,
    DesktopAgentRunQueueService,
    DesktopAgentRunStoreService,
    DesktopAgentRuntimeExecutionService,
)
from ALDE_Projekt.ALDE.alde.agents_runtime_core import AgentRuntimeCoreService, InMemoryMessageRunnerService


class TestDesktopRuntime(unittest.TestCase):
    def test_persistence_service_recovers_runs_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            persistence_service = DesktopAgentRunPersistenceService(
                storage_path=Path(temp_dir) / "desktop_runs.json",
            )
            store_service = DesktopAgentRunStoreService(persistence_service=persistence_service)

            queued_run = store_service.create_object_run(
                request_kind="chat",
                target_agent="_xplaner_xrouter",
                prompt="persist me",
                status="queued",
            )
            store_service.update_object_run(queued_run.run_id, status="completed", output="ok")

            reloaded_store = DesktopAgentRunStoreService(persistence_service=persistence_service)
            reloaded_run = reloaded_store.load_object_run(queued_run.run_id)

            self.assertIsNotNone(reloaded_run)
            assert reloaded_run is not None
            self.assertEqual(reloaded_run.status, "completed")
            self.assertEqual(reloaded_run.output, "ok")

    def test_persistence_service_marks_unfinished_runs_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            persistence_service = DesktopAgentRunPersistenceService(
                storage_path=Path(temp_dir) / "desktop_runs.json",
            )
            store_service = DesktopAgentRunStoreService(persistence_service=persistence_service)

            queued_run = store_service.create_object_run(
                request_kind="chat",
                target_agent="_xworker",
                prompt="resume me",
                status="queued",
            )

            reloaded_store = DesktopAgentRunStoreService(persistence_service=persistence_service)
            reloaded_run = reloaded_store.load_object_run(queued_run.run_id)

            self.assertIsNotNone(reloaded_run)
            assert reloaded_run is not None
            self.assertEqual(reloaded_run.status, "interrupted")
            self.assertIn("Recovered unfinished local desktop run", reloaded_run.error or "")

    def test_shared_runtime_core_falls_back_on_runtime_error(self) -> None:
        service = AgentRuntimeCoreService()

        with patch.object(service, "execute_chat_object", side_effect=RuntimeError("boom")):
            result = service.run_chat_object(target_agent="_xworker", prompt="hello")

        self.assertIn("Agent runtime fallback path activated.", result)
        self.assertIn("RuntimeError: boom", result)

    def test_execute_chat_object_uses_chatcom_for_xplaner(self) -> None:
        service = DesktopAgentRuntimeExecutionService()
        captured: dict[str, object] = {}

        class _FakeChatCom:
            def __init__(self, _model: str, _url: list[str], _input_text: str) -> None:
                captured["model"] = _model
                captured["url"] = list(_url)
                captured["input_text"] = _input_text

            def get_response(self) -> str:
                return "planner ok"

        run = DesktopAgentRun(
            run_id="run-1",
            request_kind="chat",
            target_agent="_xplaner_xrouter",
            prompt="hello",
            attachments=["/tmp/input.txt"],
            model_name="",
        )

        with patch.object(service, "load_chat_components", return_value=(_FakeChatCom, object(), object())), patch.object(
            service,
            "load_runtime_components",
            return_value=(lambda _label: {"model": "gpt-test"}, lambda label: label, object()),
        ):
            result = service.execute_chat_object(run)

        self.assertEqual(result, "planner ok")
        self.assertEqual(captured["model"], "gpt-test")
        self.assertEqual(captured["url"], ["/tmp/input.txt"])
        self.assertEqual(captured["input_text"], "hello")

    def test_execute_chat_object_forces_route_for_xworker_targets(self) -> None:
        service = DesktopAgentRuntimeExecutionService()
        captured: dict[str, object] = {}

        def _fake_execute_forced_route(args: dict, *, ChatCom=None, origin_agent_label: str = "") -> str:
            captured["args"] = dict(args)
            captured["origin_agent_label"] = origin_agent_label
            return "worker ok"

        run = DesktopAgentRun(
            run_id="run-2",
            request_kind="chat",
            target_agent="_xworker",
            prompt="build result",
        )

        with patch.object(service, "load_chat_components", return_value=(object(), object(), object())), patch.object(
            service,
            "load_runtime_components",
            return_value=(lambda _label: {"model": "gpt-test"}, lambda label: label, _fake_execute_forced_route),
        ):
            result = service.execute_chat_object(run)

        self.assertEqual(result, "worker ok")
        self.assertEqual(captured["args"], {"target_agent": "_xworker", "job_name": "generic_execution", "user_question": "build result"})
        self.assertEqual(captured["origin_agent_label"], "_xplaner_xrouter")

    def test_inmemory_message_runner_processes_messages(self) -> None:
        processed: list[str] = []
        runner = InMemoryMessageRunnerService[str](
            worker_name="alde-test-runner",
            process_object_message=processed.append,
            poll_interval_seconds=0.05,
        )

        try:
            runner.submit_object_message("hello")
            deadline = time.time() + 1.0
            while time.time() < deadline and not processed:
                time.sleep(0.02)
            self.assertEqual(processed, ["hello"])
            self.assertEqual(runner.load_object_health()["backend"], "inmemory")
        finally:
            runner.stop_object_runner()

    def test_inmemory_message_runner_starts_configured_worker_pool(self) -> None:
        processed: list[str] = []
        runner = InMemoryMessageRunnerService[str](
            worker_name="alde-test-runner-pool",
            process_object_message=processed.append,
            poll_interval_seconds=0.05,
            worker_count=3,
        )

        try:
            runner.start_object_runner()
            deadline = time.time() + 1.0
            health = runner.load_object_health()
            while time.time() < deadline and int(health.get("runner_count") or 0) < 3:
                time.sleep(0.02)
                health = runner.load_object_health()

            self.assertEqual(health["configured_runner_count"], 3)
            self.assertEqual(health["runner_count"], 3)
        finally:
            runner.stop_object_runner()

    def test_queue_service_reads_parallel_runner_configuration_from_env(self) -> None:
        class _IdleExecutionService:
            def execute_object_run(self, run: DesktopAgentRun) -> str:
                return "ok"

        with tempfile.TemporaryDirectory() as temp_dir:
            store_service = DesktopAgentRunStoreService(
                persistence_service=DesktopAgentRunPersistenceService(
                    storage_path=Path(temp_dir) / "desktop_runs.json",
                )
            )
            with patch.dict(
                os.environ,
                {
                    "ALDE_DESKTOP_PARALLEL_ENABLED": "1",
                    "ALDE_DESKTOP_PARALLEL_WORKERS": "3",
                    "ALDE_DESKTOP_RUNNER_POLL_SECONDS": "0.03",
                },
                clear=False,
            ):
                queue_service = DesktopAgentRunQueueService(
                    store_service=store_service,
                    execution_service=_IdleExecutionService(),
                )

                try:
                    health = queue_service.load_object_health()
                    self.assertTrue(health["parallel_enabled"])
                    self.assertEqual(health["max_parallel_workers"], 3)
                    self.assertEqual(health["configured_worker_count"], 3)
                    self.assertEqual(health["configured_runner_count"], 3)
                finally:
                    queue_service.stop_object_runner()

    def test_queue_object_run_completes_in_background(self) -> None:
        class _FakeExecutionService:
            def execute_object_run(self, run: DesktopAgentRun) -> str:
                return f"done:{run.prompt}"

        with tempfile.TemporaryDirectory() as temp_dir:
            store_service = DesktopAgentRunStoreService(
                persistence_service=DesktopAgentRunPersistenceService(
                    storage_path=Path(temp_dir) / "desktop_runs.json",
                )
            )
            execution_service = _FakeExecutionService()
            queue_service = DesktopAgentRunQueueService(
                store_service=store_service,
                execution_service=execution_service,
            )
            facade = DesktopAgentRunFacadeService(
                store_service=store_service,
                queue_service=queue_service,
                execution_service=execution_service,
                monitor_service=DesktopAgentRunMonitorService(
                    store_service=store_service,
                    queue_service=queue_service,
                ),
            )

            try:
                queued_run = facade.queue_object_run(
                    request_kind="chat",
                    target_agent="_xplaner_xrouter",
                    prompt="async hello",
                    attachments=[],
                    metadata={"source": "test"},
                )

                deadline = time.time() + 2.0
                completed_run = facade.load_object_run(queued_run.run_id)
                while time.time() < deadline:
                    completed_run = facade.load_object_run(queued_run.run_id)
                    if completed_run is not None and completed_run.status == "completed":
                        break
                    time.sleep(0.05)

                self.assertIsNotNone(completed_run)
                assert completed_run is not None
                self.assertEqual(completed_run.status, "completed")
                self.assertEqual(completed_run.output, "done:async hello")
            finally:
                queue_service.stop_object_runner()

    def test_queue_object_run_records_failures(self) -> None:
        class _FailingExecutionService:
            def execute_object_run(self, run: DesktopAgentRun) -> str:
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as temp_dir:
            store_service = DesktopAgentRunStoreService(
                persistence_service=DesktopAgentRunPersistenceService(
                    storage_path=Path(temp_dir) / "desktop_runs.json",
                )
            )
            execution_service = _FailingExecutionService()
            queue_service = DesktopAgentRunQueueService(
                store_service=store_service,
                execution_service=execution_service,
            )
            facade = DesktopAgentRunFacadeService(
                store_service=store_service,
                queue_service=queue_service,
                execution_service=execution_service,
                monitor_service=DesktopAgentRunMonitorService(
                    store_service=store_service,
                    queue_service=queue_service,
                ),
            )

            try:
                queued_run = facade.queue_object_run(
                    request_kind="chat",
                    target_agent="_xplaner_xrouter",
                    prompt="async fail",
                )

                deadline = time.time() + 2.0
                failed_run = facade.load_object_run(queued_run.run_id)
                while time.time() < deadline:
                    failed_run = facade.load_object_run(queued_run.run_id)
                    if failed_run is not None and failed_run.status == "failed":
                        break
                    time.sleep(0.05)

                self.assertIsNotNone(failed_run)
                assert failed_run is not None
                self.assertEqual(failed_run.status, "failed")
                self.assertIn("RuntimeError: boom", failed_run.error or "")
            finally:
                queue_service.stop_object_runner()

    def test_monitor_service_summarizes_local_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_service = DesktopAgentRunStoreService(
                persistence_service=DesktopAgentRunPersistenceService(
                    storage_path=Path(temp_dir) / "desktop_runs.json",
                )
            )

            class _IdleExecutionService:
                def execute_object_run(self, run: DesktopAgentRun) -> str:
                    return "ok"

            queue_service = DesktopAgentRunQueueService(
                store_service=store_service,
                execution_service=_IdleExecutionService(),
            )
            monitor_service = DesktopAgentRunMonitorService(
                store_service=store_service,
                queue_service=queue_service,
            )

            store_service.create_object_run(
                request_kind="chat",
                target_agent="_xplaner_xrouter",
                prompt="queued",
                status="queued",
            )
            failed_run = store_service.create_object_run(
                request_kind="chat",
                target_agent="_xworker",
                prompt="failed",
                status="failed",
            )
            store_service.update_object_run(failed_run.run_id, error="RuntimeError: boom")

            snapshot = monitor_service.load_object_snapshot(limit=5)
            activity_view = monitor_service.load_object_activity_view(limit=5)

            self.assertEqual(snapshot["run_count"], 2)
            self.assertEqual(snapshot["active_count"], 1)
            self.assertEqual(snapshot["failure_count"], 1)
            self.assertEqual(len(snapshot["recent_runs"]), 2)
            self.assertEqual(len(activity_view), 2)

    def test_monitor_service_includes_runtime_observability_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_service = DesktopAgentRunStoreService(
                persistence_service=DesktopAgentRunPersistenceService(
                    storage_path=Path(temp_dir) / "desktop_runs.json",
                )
            )

            class _IdleExecutionService:
                def execute_object_run(self, run: DesktopAgentRun) -> str:
                    return "ok"

            queue_service = DesktopAgentRunQueueService(
                store_service=store_service,
                execution_service=_IdleExecutionService(),
            )
            monitor_service = DesktopAgentRunMonitorService(
                store_service=store_service,
                queue_service=queue_service,
            )

            with patch.object(
                monitor_service,
                "load_runtime_observability_snapshot",
                return_value={
                    "healthy": True,
                    "queue_backend": "inmemory",
                    "queue_healthy": True,
                    "session_count": 2,
                    "active_session_count": 1,
                    "validation": {"valid": True, "errors": []},
                },
            ):
                snapshot = monitor_service.load_object_snapshot(limit=5)

            self.assertTrue(snapshot["runtime_observability"]["healthy"])
            self.assertEqual(snapshot["runtime_observability"]["session_count"], 2)
            self.assertEqual(snapshot["runtime_observability"]["queue_backend"], "inmemory")


if __name__ == "__main__":
    unittest.main()