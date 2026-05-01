from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any, Callable, Generic, TypeVar


MessageObject = TypeVar("MessageObject")


class AgentRuntimeCoreService:
    def load_chat_components(self) -> tuple[Any, Any, Any]:
        from alde.agents_ccomp import ChatCom, ImageCreate, ImageDescription  # type: ignore

        return ChatCom, ImageDescription, ImageCreate

    def load_runtime_components(self) -> tuple[Any, Any, Any]:
        from alde.agents_config import get_agent_config, normalize_agent_label  # type: ignore
        from alde.agents_factory import execute_forced_route  # type: ignore

        return get_agent_config, normalize_agent_label, execute_forced_route

    def build_runtime_fallback(self, *, target_agent: str, exc: Exception) -> str:
        return (
            "Agent runtime fallback path activated. "
            f"target={target_agent}; reason={type(exc).__name__}: {exc}"
        )

    def load_object_job_name(self, normalized_target: str) -> str:
        try:
            from alde.agents_config import get_default_job_name  # type: ignore
        except ImportError as e:
            msg = str(e)
            if "attempted relative import" in msg or "no known parent package" in msg:
                from alde.agents_config import get_default_job_name  # type: ignore
            else:
                raise
        return str(get_default_job_name(normalized_target) or "").strip()

    def build_object_route_request(self, *, normalized_target: str, prompt: str) -> dict[str, Any]:
        route_request: dict[str, Any] = {
            "target_agent": normalized_target,
            "user_question": prompt,
        }
        job_name = self.load_object_job_name(normalized_target)
        if job_name:
            route_request["job_name"] = job_name
        return route_request

    def execute_chat_object(
        self,
        *,
        target_agent: str,
        prompt: str,
        attachments: list[str] | None = None,
        model_name: str = "",
    ) -> str:
        ChatCom, _, _ = self.load_chat_components()
        get_agent_config, normalize_agent_label, execute_forced_route = self.load_runtime_components()

        normalized_target = normalize_agent_label(target_agent)
        if normalized_target == "_xplaner_xrouter":
            model = str(model_name or (get_agent_config(normalized_target) or {}).get("model") or "gpt-4o")
            chat_kwargs: dict[str, Any] = {
                "_model": model,
                "_input_text": prompt,
            }
            if attachments:
                chat_kwargs["_url"] = list(attachments)
            return str(
                ChatCom(**chat_kwargs).get_response()
                or ""
            )

        return str(
            execute_forced_route(
                self.build_object_route_request(normalized_target=normalized_target, prompt=prompt),
                ChatCom=ChatCom,
                origin_agent_label="_xplaner_xrouter",
            )
            or ""
        )

    def run_chat_object(
        self,
        *,
        target_agent: str,
        prompt: str,
        attachments: list[str] | None = None,
        model_name: str = "",
    ) -> str:
        try:
            return self.execute_chat_object(
                target_agent=target_agent,
                prompt=prompt,
                attachments=attachments,
                model_name=model_name,
            )
        except Exception as exc:
            return self.build_runtime_fallback(target_agent=target_agent, exc=exc)


class InMemoryMessageRunnerService(Generic[MessageObject]):
    def __init__(
        self,
        *,
        worker_name: str,
        process_object_message: Callable[[MessageObject], None],
        poll_interval_seconds: float = 0.5,
        worker_count: int = 1,
    ) -> None:
        self.worker_name = worker_name
        self.process_object_message = process_object_message
        self.poll_interval_seconds = max(float(poll_interval_seconds), 0.05)
        self.worker_count = max(int(worker_count or 1), 1)
        self._queue: Queue[MessageObject] = Queue()
        self._stop = Event()
        self._thread_lock = Lock()
        self._threads: list[Thread] = []

    def _build_object_thread(self, thread_index: int) -> Thread:
        normalized_worker_name = str(self.worker_name or "").strip() or "inmemory-runner"
        thread_name = (
            normalized_worker_name
            if self.worker_count <= 1
            else f"{normalized_worker_name}-{thread_index + 1}"
        )
        return Thread(target=self._work_loop, daemon=True, name=thread_name)

    def _load_alive_object_threads(self) -> list[Thread]:
        return [thread for thread in self._threads if thread.is_alive()]

    def start_object_runner(self) -> None:
        with self._thread_lock:
            alive_threads = self._load_alive_object_threads()
            if len(alive_threads) >= self.worker_count:
                self._threads = alive_threads
                return

            self._stop.clear()
            while len(alive_threads) < self.worker_count:
                thread = self._build_object_thread(len(alive_threads))
                alive_threads.append(thread)
                thread.start()

            self._threads = alive_threads

    def stop_object_runner(self) -> None:
        self._stop.set()
        with self._thread_lock:
            threads = list(self._threads)

        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=5)

        with self._thread_lock:
            self._threads = self._load_alive_object_threads()

    def submit_object_message(self, message: MessageObject) -> None:
        self.start_object_runner()
        self._queue.put(message)

    def load_object_health(self) -> dict[str, Any]:
        with self._thread_lock:
            alive_threads = self._load_alive_object_threads()
            self._threads = alive_threads

        return {
            "backend": "inmemory",
            "healthy": True,
            "runner_alive": bool(alive_threads),
            "runner_count": len(alive_threads),
            "configured_runner_count": self.worker_count,
            "pending_count": self._queue.qsize(),
        }

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._queue.get(timeout=self.poll_interval_seconds)
            except Empty:
                continue

            try:
                self.process_object_message(message)
            finally:
                self._queue.task_done()