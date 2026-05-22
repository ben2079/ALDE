from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PKG_ROOT = Path(__file__).resolve().parents[2]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

import alde.agents_factory as agents_factory
import alde.agents_tools as tools_mod
import alde.chat_completion as chat_mod
import alde.agents_config as agents_configurator
from alde.agents_config import get_agent_workflow_config


def _tool_call(name: str, arguments: str, call_id: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _FakeChatComE:
    last_messages = None
    last_tools = None
    last_model = None

    def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
        type(self).last_model = _model
        type(self).last_messages = list(_messages)
        type(self).last_tools = list(tools)

    def _response(self):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="xworker ok", tool_calls=None))])


class _FakeClient:
    class _Chat:
        class _Completions:
            @staticmethod
            def create(model=None, messages=None, tools=None, tool_choice=None):
                msg = SimpleNamespace(
                    content="",
                    tool_calls=[
                        _tool_call(
                            "route_to_agent",
                            '{"target_agent":"_xworker","job_name":"document_dispatch","user_question":"please dispatch this request"}',
                        )
                    ],
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)], id="resp_1")

        completions = _Completions()

    chat = _Chat()


class TestAgentRouting(unittest.TestCase):
    def setUp(self) -> None:
        chat_mod.ChatHistory._history_ = []
        agents_factory._WORKFLOW_SESSION_CACHE.clear()
        _FakeChatComE.last_messages = None
        _FakeChatComE.last_tools = None
        _FakeChatComE.last_model = None

    def test_routed_followup_uses_clean_handoff_messages(self) -> None:
        history = agents_factory.get_history()
        history._history_ = [
            {"role": "user", "content": "scan /tmp/jobs", "thread-id": history._thread_iD},
            {
                "role": "assistant",
                "content": "",
                "thread-id": history._thread_iD,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "route_to_agent", "arguments": '{"target_agent":"_xworker"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "content": "Routing to _xworker",
                "thread-id": history._thread_iD,
                "tool_call_id": "call_1",
                "name": "route_to_agent",
            },
        ]

        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[_tool_call("route_to_agent", '{"target_agent":"_xworker","user_question":"scan /tmp/jobs","job_name":"document_dispatch"}')],
        )

        with patch("alde.chat_completion.ChatComE", _FakeChatComE):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xplaner_xrouter")

        self.assertEqual(result, "xworker ok")
        self.assertIsNotNone(_FakeChatComE.last_messages)
        self.assertEqual(_FakeChatComE.last_messages[0]["role"], "system")
        self.assertEqual(_FakeChatComE.last_messages[-1]["role"], "user")
        self.assertIn('"handoff_to": "_xworker"', _FakeChatComE.last_messages[-1]["content"])

    def test_primary_chatcom_routes_with_xplaner_label(self) -> None:
        captured: dict[str, object] = {}

        def _fake_handle_tool_calls(agent_msg, depth=0, ChatCom=None, agent_label=""):
            captured["agent_label"] = agent_label
            return "ok"

        with patch.object(chat_mod.ChatCompletion, "_get_client", return_value=_FakeClient()), patch(
            "alde.agents_factory._handle_tool_calls", side_effect=_fake_handle_tool_calls
        ):
            chat = chat_mod.ChatCom(_model="gpt-4o-mini", _input_text="please dispatch this request")
            result = chat.get_response()

        self.assertEqual(result, "ok")
        self.assertEqual(captured.get("agent_label"), "_xrouter_xplanner")
        self.assertEqual(chat._instance_policy, "session_scoped")
        self.assertEqual(chat._agent_runtime.get("role"), "xrouter_xplanner")

    def test_latest_user_message_returns_last_non_empty_user_entry(self) -> None:
        history = agents_factory.get_history()
        history._history_ = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "   "},
            {"role": "user", "content": "final question"},
        ]

        result = agents_factory._latest_user_message("fallback")

        self.assertEqual(result, "final question")

    def test_runtime_learning_sync_for_assistant_text_response(self) -> None:
        history = agents_factory.get_history()
        history._history_ = [
            {"role": "user", "content": "please summarize"},
        ]

        with patch("alde.agents_factory._sync_runtime_learning_signal") as sync_mock:
            result = agents_factory.ASSISTANT_RESPONSE_SERVICE._handle_text_response(
                text="summary complete",
                routing_request=None,
                history=history,
                response_agent_label="_xworker",
                workflow_session=None,
                tool_results=["tool output"],
            )

        self.assertEqual(result, "summary complete")
        self.assertTrue(sync_mock.called)
        sync_kwargs = dict(sync_mock.call_args.kwargs)
        self.assertEqual(sync_kwargs.get("tool_name"), "runtime_assistant_response")
        self.assertEqual((sync_kwargs.get("model_result") or {}).get("content"), "summary complete")

    def test_runtime_learning_sync_for_terminal_tool_result(self) -> None:
        history = agents_factory.get_history()
        history._history_ = [
            {"role": "user", "content": "write this file"},
        ]
        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[_tool_call("write_document", '{"content":"hello"}', call_id="call_sync")],
        )

        with patch.object(
            agents_factory.TOOL_CALL_EXECUTION_SERVICE,
            "process_object_calls",
            return_value={
                "workflow_session": None,
                "routing_request": None,
                "tool_results": ["stored"],
                "terminal_tool_result": "stored",
                "terminal_tool_name": "write_document",
            },
        ), patch("alde.agents_factory._sync_runtime_learning_signal") as sync_mock:
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xworker")

        self.assertEqual(result, "stored")
        self.assertTrue(sync_mock.called)
        sync_kwargs = dict(sync_mock.call_args.kwargs)
        self.assertEqual(sync_kwargs.get("tool_name"), "runtime_tool_terminal")
        self.assertTrue(bool((sync_kwargs.get("model_result") or {}).get("direct_tool_result")))

    def test_routed_agent_keeps_xworker_label_for_nested_tool_followup(self) -> None:
        captured_calls: list[dict[str, object]] = []

        class _SequencedChatComE:
            responses = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                tool_calls=[
                                    _tool_call(
                                        "vdb_worker",
                                        '{"operation":"create","store":{"name":"VSM_5_Data"}}',
                                        call_id="call_2",
                                    )
                                ],
                            )
                        )
                    ]
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="vector db built",
                                tool_calls=None,
                            )
                        )
                    ]
                ),
            ]

            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                captured_calls.append(
                    {
                        "model": _model,
                        "messages": list(_messages),
                        "tools": list(tools),
                        "tool_choice": tool_choice,
                    }
                )

            def _response(self):
                return type(self).responses.pop(0)

        history = agents_factory.get_history()
        history._history_ = [{"role": "user", "content": "build vectordb", "thread-id": history._thread_iD}]
        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "route_to_agent",
                    '{"target_agent":"_xworker","job_name":"document_dispatch","user_question":"build vectordb"}',
                    call_id="call_1",
                )
            ],
        )

        expected_worker_tools = agents_factory.get_agent_runtime_tools("_xworker")

        with patch("alde.chat_completion.ChatComE", _SequencedChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=[
                (
                    "Routing to _xworker",
                    {
                        "messages": [
                            {"role": "system", "content": "xworker system"},
                            {"role": "user", "content": "build vectordb"},
                        ],
                        "agent_label": "_xworker",
                        "tools": expected_worker_tools,
                        "model": agents_factory.AGENTS_REGISTRY["_xworker"]["model"],
                        "include_history": False,
                    },
                ),
                (
                    '{"operation":"create","store":{"name":"VSM_5_Data"}}',
                    None,
                ),
            ],
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xplaner_xrouter")

        self.assertEqual(result, '{"operation":"create","store":{"name":"VSM_5_Data"}}')
        self.assertEqual(len(captured_calls), 1)
        self.assertEqual(captured_calls[0]["tools"], expected_worker_tools)
        self.assertEqual(captured_calls[0]["model"], agents_factory.AGENTS_REGISTRY["_xworker"]["model"])
        self.assertEqual(chat_mod.ChatHistory._history_[-1]["assistant-name"], "_xworker")

    def test_followup_request_uses_routing_request_configuration(self) -> None:
        history = SimpleNamespace(
            _insert=lambda tool, f_depth: [{"role": "assistant", "content": f"history:{f_depth}"}],
        )

        request = agents_factory.TOOL_CALL_FOLLOWUP_SERVICE.build_object_request(
            history=history,
            routing_request={
                "messages": {"role": "user", "content": "dispatch this"},
                "include_history": True,
                "history_depth": "7",
                "tools": [{"function": {"name": "vdb_worker"}}],
                "model": "gpt-test",
                "agent_label": "_xworker",
            },
            agent_label="_xplaner_xrouter",
        )

        self.assertEqual(
            request["messages"],
            [
                {"role": "user", "content": "dispatch this"},
                {"role": "assistant", "content": "history:7"},
            ],
        )
        self.assertEqual(request["model"], "gpt-test")
        self.assertEqual(request["tools"], [{"function": {"name": "vdb_worker"}}])

    def test_non_routed_followup_logs_assistant_response_phase(self) -> None:
        class _TextOnlyChatComE:
            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self._model = _model

            def _response(self):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="final tool summary", tool_calls=None))]
                )

        history = agents_factory.get_history()
        history._history_ = [{"role": "user", "content": "run write_document", "thread-id": history._thread_iD}]
        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[_tool_call("write_document", '{"content":"hello","path":"/tmp","titel":"x"}')],
        )

        with patch("alde.chat_completion.ChatComE", _TextOnlyChatComE), patch(
            "alde.agents_factory.execute_tool",
            return_value=("saved document", None),
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xworker")

        self.assertEqual(result, "saved document")
        workflow_entries = [
            entry for entry in chat_mod.ChatHistory._history_
            if isinstance(entry, dict)
            and isinstance(entry.get("data"), dict)
            and isinstance((entry.get("data") or {}).get("workflow"), dict)
            and entry.get("role") == "assistant"
        ]
        self.assertTrue(workflow_entries)
        self.assertEqual(workflow_entries[-1]["content"], "saved document")
        self.assertEqual(workflow_entries[-1]["data"]["workflow"].get("phase"), "assistant_response")

    def test_read_document_error_runs_followup_instead_of_terminal_result(self) -> None:
        class _RetryPromptChatComE:
            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self._model = _model

            def _response(self):
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="Bitte gib einen gueltigen Dateipfad an.",
                                tool_calls=None,
                            )
                        )
                    ]
                )

        history = agents_factory.get_history()
        history._history_ = [{"role": "user", "content": "parse this file", "thread-id": history._thread_iD}]
        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[_tool_call("read_document", '{"file_path":"job_posting_parser"}')],
        )

        with patch("alde.chat_completion.ChatComE", _RetryPromptChatComE), patch(
            "alde.agents_factory.execute_tool",
            return_value=(
                "Error: Datei '/home/ben/Vs_Code_Projects/Projects/ALDE_Projekt/job_posting_parser' nicht gefunden.",
                None,
            ),
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xworker")

        self.assertEqual(result, "Bitte gib einen gueltigen Dateipfad an.")

    def test_tool_failure_replaces_hallucinated_success_followup_text(self) -> None:
        class _SequencedChatComE:
            responses = [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content="",
                                tool_calls=[
                                    _tool_call(
                                        "read_document",
                                        '{"file_path":"/path/to/job_posting_data.json"}',
                                        call_id="call_read",
                                    )
                                ],
                            )
                        )
                    ]
                ),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content=(
                                    "The tasks have been routed to the xworker agent for processing. "
                                    "The applicant profile and job postings will be parsed."
                                ),
                                tool_calls=None,
                            )
                        )
                    ]
                ),
            ]

            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self._model = _model

            def _response(self):
                return type(self).responses.pop(0)

        history = agents_factory.get_history()
        history._history_ = [
            {"role": "user", "content": "parse applicant and job posting files", "thread-id": history._thread_iD}
        ]
        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "route_to_agent",
                    '{"target_agent":"_xworker","job_name":"job_posting_parser","user_question":"parse files"}',
                    call_id="call_route",
                )
            ],
        )

        route_request = {
            "messages": [
                {"role": "system", "content": "xworker system"},
                {"role": "user", "content": "parse files"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_document",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"file_path": {"type": "string"}},
                            "required": ["file_path"],
                        },
                    },
                }
            ],
            "model": "gpt-test",
            "agent_label": "_xworker",
            "include_history": False,
        }

        with patch("alde.chat_completion.ChatComE", _SequencedChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=[
                ("Routing to _xworker", route_request),
                ("Error: Datei '/path/to/job_posting_data.json' nicht gefunden.", None),
            ],
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xplaner_xrouter")

        self.assertEqual(result, "Error: Datei '/path/to/job_posting_data.json' nicht gefunden.")

    def test_only_xplaner_retains_router_workflow(self) -> None:
        legacy_workflow = get_agent_workflow_config("_xplaner_xrouter")
        canonical_workflow = get_agent_workflow_config("_xrouter_xplanner")

        self.assertEqual(legacy_workflow.get("name"), "xplaner_xrouter_router")
        self.assertEqual(canonical_workflow.get("name"), "xplaner_xrouter_router")
        self.assertEqual(legacy_workflow, canonical_workflow)
        self.assertEqual(get_agent_workflow_config("_xworker").get("name"), "xworker_leaf")

    def test_xplaner_workflow_supports_branch_merge_conditions(self) -> None:
        legacy_session = agents_factory._create_workflow_session("_xplaner_xrouter")
        canonical_session = agents_factory._create_workflow_session("_xrouter_xplanner")

        self.assertIsNotNone(legacy_session)
        self.assertIsNotNone(canonical_session)
        self.assertEqual(legacy_session.get("workflow_name"), "xplaner_xrouter_router")
        self.assertEqual(canonical_session.get("workflow_name"), "xplaner_xrouter_router")
        self.assertEqual(legacy_session.get("agent_label"), "_xrouter_xplanner")
        self.assertEqual(canonical_session.get("agent_label"), "_xrouter_xplanner")
        self.assertEqual(legacy_session.get("current_state"), "xplaner_ready")
        self.assertEqual(canonical_session.get("current_state"), "xplaner_ready")

    def test_create_workflow_session_prefers_job_workflow_from_routing_request(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "job_name": "job_posting_parser",
                "user_question": "Parse this job posting file",
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)

        session = agents_factory._create_workflow_session("_xworker", routing_request=route)

        self.assertIsNotNone(session)
        self.assertEqual(session.get("workflow_name"), "xworker_job_posting_parser_leaf")
        self.assertEqual(session.get("current_state"), "job_posting_parser_active")

    def test_job_posting_parser_workflow_transitions_inactive_to_active(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "job_name": "job_posting_parser",
                "user_question": "Parse this job posting file",
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)

        session = agents_factory._create_workflow_session("_xworker", routing_request=route)
        self.assertIsNotNone(session)

        session = agents_factory._advance_workflow_session(
            session,
            event_kind="tool",
            event_name="read_document",
            payload={"file_path": "/tmp/job_posting.pdf"},
        )

        self.assertIsNotNone(session)
        self.assertEqual(session.get("current_state"), "job_posting_parser_active")
        self.assertFalse(bool(session.get("terminal")))

    def test_worker_runtime_tools_include_route_to_agent(self) -> None:
        worker_tools = agents_factory.get_agent_runtime_tools("_xworker")
        tool_names = {tool["function"]["name"] for tool in worker_tools}

        self.assertIn("route_to_agent", tool_names)
        self.assertTrue(agents_factory._agent_can_route("_xworker"))

    def test_planner_runtime_tools_include_route_to_agent(self) -> None:
        planner_tools = agents_factory.get_agent_runtime_tools("_xplaner_xrouter")
        tool_names = {tool["function"]["name"] for tool in planner_tools}

        self.assertIn("route_to_agent", tool_names)
        self.assertIn("load_repo_context_for_ide_agent", tool_names)
        self.assertTrue(agents_factory._agent_can_route("_xplaner_xrouter"))

    def test_create_agents_command_resolves_to_xplaner_planning_job(self) -> None:
        route = agents_configurator.resolve_forced_route(
            "_xplaner_xrouter",
            "/create agents build a qa system with planner and worker",
            set(agents_factory.AGENTS_REGISTRY.keys()),
        )

        self.assertIsNone(route)

    def test_resolve_forced_route_parses_python_execute_route_to_agent_snippet(self) -> None:
        route = agents_configurator.resolve_forced_route(
            "_xplaner_xrouter",
            """
result, route = agents_factory.execute_route_to_agent(
    {
        \"job_name\": \"router_planner_cover_letter_sequence\",
        \"profile_id\": \"44e8fce720b6ef2f385478d7f1123027caa7a148abc37af870c7522f0ccf6040\",
        \"job_posting\": {\"a1bc3b671dee2a993f42d2ef572573218d4db04ebc381a40b2d10446f1016c30\"},
        \"options\": {\"language\": \"de\", \"tone\": \"modern\"},
    },
    source_agent_label=\"_xrouter_xplanner\",
)
""",
            set(agents_factory.AGENTS_REGISTRY.keys()),
        )

        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("job_name"), "router_planner_cover_letter_sequence")
        self.assertEqual(route.get("profile_id"), "profile:44e8fce720b6ef2f385478d7f1123027caa7a148abc37af870c7522f0ccf6040")
        self.assertEqual(
            route.get("job_posting"),
            {
                "source": "correlation_id",
                "value": "a1bc3b671dee2a993f42d2ef572573218d4db04ebc381a40b2d10446f1016c30",
            },
        )
        self.assertEqual(route.get("_origin_agent_label"), "_xrouter_xplanner")

    def test_chatcom_python_execute_route_snippet_uses_forced_route_without_model_call(self) -> None:
        with patch.object(chat_mod.ChatCompletion, "_get_client") as get_client, patch(
            "alde.agents_factory.execute_forced_route",
            return_value="Routing to _xworker",
        ) as execute_forced_route_mock:
            chat = chat_mod.ChatCom(
                _model="gpt-4o-mini",
                _input_text="""
result, route = agents_factory.execute_route_to_agent(
    {
        \"job_name\": \"router_planner_cover_letter_sequence\",
        \"profile_id\": \"44e8fce720b6ef2f385478d7f1123027caa7a148abc37af870c7522f0ccf6040\",
        \"job_posting\": {\"a1bc3b671dee2a993f42d2ef572573218d4db04ebc381a40b2d10446f1016c30\"},
        \"options\": {\"language\": \"de\", \"tone\": \"modern\"},
    },
    source_agent_label=\"_xrouter_xplanner\",
)
""",
                _name="test_python_route_snippet",
            )
            result = chat.get_response()

        self.assertIsInstance(chat._forced_route, dict)
        self.assertEqual(chat._forced_route.get("job_name"), "router_planner_cover_letter_sequence")
        self.assertEqual(result, "Routing to _xworker")
        execute_forced_route_mock.assert_called_once()
        get_client.assert_not_called()

    def test_available_job_names_include_runtime_default_jobs(self) -> None:
        job_names = agents_configurator.get_available_job_names()

        self.assertIn("generic_execution", job_names)
        self.assertIn("cover_letter_writer", job_names)
        self.assertIn("router_planner_cover_letter_sequence", job_names)

    def test_job_config_drives_default_object_projection(self) -> None:
        job_config = agents_configurator.get_job_config("cover_letter_writer")

        self.assertEqual(job_config.get("default_object_name"), "cover_letters")

    def test_worker_route_to_agent_can_delegate_to_planner(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {"target_agent": "_xplaner_xrouter", "user_question": "continue this thread"},
            source_agent_label="_xworker",
        )

        self.assertEqual(result, "Routing to _xrouter_xplanner")
        self.assertIsInstance(route, dict)
        self.assertEqual((route or {}).get("agent_label"), "_xrouter_xplanner")

    def test_worker_internal_auto_handoff_self_route_is_allowed(self) -> None:
        result, route = agents_factory.execute_tool(
            "route_to_agent",
            {
                "target_agent": "_xworker",
                "job_name": "job_posting_parser",
                "handoff_protocol": "agent_handoff_v1",
                "allow_internal_handoff": True,
                "handoff_payload": {
                    "agent_label": "_xworker",
                    "handoff_to": "_xworker",
                    "output": {
                        "type": "file",
                        "correlation_id": "sha-123",
                        "link": {"thread_id": "thread-1", "message_id": "msg-1"},
                        "file": {
                            "path": "/tmp/job_offer.pdf",
                            "content_sha256": "sha-123",
                        },
                        "db": {"processing_state": "queued"},
                        "requested_actions": [
                            "parse",
                            "extract_text",
                            "store_object_result",
                            "mark_processed_on_success",
                        ],
                    },
                },
                "handoff_metadata": {
                    "correlation_id": "sha-123",
                    "dispatcher_message_id": "dispatcher-msg-1",
                    "dispatcher_db_path": "/tmp/dispatcher_doc_db.json",
                    "obj_name": "job_postings",
                    "obj_db_path": "/tmp/job_postings_db.json",
                },
            },
            source_agent_label="_xworker",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("agent_label"), "_xworker")

    def test_worker_internal_auto_handoff_job_name_target_is_allowed(self) -> None:
        result, route = agents_factory.execute_tool(
            "route_to_agent",
            {
                "target_agent": "job_posting_parser",
                "job_name": "job_posting_parser",
                "handoff_protocol": "agent_handoff_v1",
                "allow_internal_handoff": True,
                "handoff_payload": {
                    "agent_label": "_xworker",
                    "handoff_to": "job_posting_parser",
                    "output": {
                        "type": "file",
                        "job_name": "job_posting_parser",
                        "correlation_id": "sha-456",
                        "link": {"thread_id": "thread-1", "message_id": "msg-1"},
                        "file": {
                            "path": "/tmp/job_offer.pdf",
                            "content_sha256": "sha-456",
                        },
                        "db": {"processing_state": "queued"},
                        "requested_actions": [
                            "parse",
                            "extract_text",
                            "store_object_result",
                            "mark_processed_on_success",
                        ],
                    },
                },
                "handoff_metadata": {
                    "correlation_id": "sha-456",
                    "dispatcher_message_id": "dispatcher-msg-1",
                    "dispatcher_db_path": "/tmp/dispatcher_doc_db.json",
                    "obj_name": "job_postings",
                    "obj_db_path": "/tmp/job_postings_db.json",
                    "job_name": "job_posting_parser",
                },
            },
            source_agent_label="_xworker",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("agent_label"), "_xworker")
        self.assertEqual(
            agents_factory.AGENT_EXECUTION_SELECTION_SERVICE.load_job_name(route),
            "job_posting_parser",
        )

    def test_worker_internal_handoff_flag_allows_cross_agent_route(self) -> None:
        result, route = agents_factory.execute_tool(
            "route_to_agent",
            {
                "target_agent": "_xplaner_xrouter",
                "allow_internal_handoff": True,
                "user_question": "continue this thread",
            },
            source_agent_label="_xworker",
        )

        self.assertEqual(result, "Routing to _xrouter_xplanner")
        self.assertIsInstance(route, dict)
        self.assertEqual((route or {}).get("agent_label"), "_xrouter_xplanner")

    def test_dispatch_action_ignores_outer_target_agent_for_internal_worker_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "posting.pdf")
            dispatcher_db_path = os.path.join(tmpdir, "dispatcher.json")
            with open(pdf_path, "wb") as handle:
                handle.write(b"fake-pdf-content")

            raw_result = tools_mod.ACTION_REQUEST_SERVICE.execute_dispatch_documents_action(
                request_payload={
                    "scan_dir": tmpdir,
                    "dispatcher_message_id": "msg-1",
                    "target_agent": "_job_posting_parser",
                    "db_path": dispatcher_db_path,
                },
                resolution_config={},
                execution_config={},
            )

            self.assertIsInstance(raw_result, str)
            result_payload = json.loads(raw_result)
            handoff_messages = result_payload.get("handoff_messages") or []

            self.assertEqual(len(handoff_messages), 1)
            self.assertEqual(handoff_messages[0].get("target_agent"), "_xworker")

            handoff_args = agents_factory._extract_tool_handoff_messages(
                {"handoff_messages": handoff_messages}
            )[0]
            result, route = agents_factory.execute_tool(
                "route_to_agent",
                handoff_args,
                source_agent_label="_xworker",
            )

            self.assertEqual(result, "Routing to _xworker")
            self.assertIsInstance(route, dict)
            self.assertEqual(route.get("agent_label"), "_xworker")

    def test_route_contract_prefers_two_agent_schema(self) -> None:
        contract = agents_configurator.get_handoff_route_contract(
            "_xplaner_xrouter",
            "_xworker",
        )

        self.assertEqual(contract["protocol"], "agent_handoff_v1")
        self.assertEqual(contract["handoff_schema"], "xrouter_to_xworker")
        self.assertEqual(contract["handoff_id"], "structured")
        self.assertEqual(contract["workflow_name"], "xworker_leaf")

    def test_document_dispatch_contract_uses_renamed_dispatch_workflow(self) -> None:
        contract = agents_configurator.get_handoff_route_contract(
            "_xrouter_xplanner",
            "_xworker",
            protocol="message_text",
            handoff_metadata={"job_name": "document_dispatch"},
        )

        self.assertEqual(contract.get("workflow_name"), "xworker_documents_dispatch_chain")

        renamed_workflow = agents_configurator.get_workflow_config("xworker_documents_dispatch_chain")
        self.assertEqual(renamed_workflow.get("entry_state"), "dispatcher_ready")

        removed_workflow = agents_configurator.get_workflow_config("xworker_dispatch_chain")
        self.assertEqual(removed_workflow, {})

    def test_route_to_agent_normalizes_structured_agent_handoff(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "handoff_protocol": "agent_handoff_v1",
                "agent_response": {
                    "agent_label": "_xplaner_xrouter",
                    "output": {"status": "ready", "value": 42},
                    "handoff_to": "_xworker",
                    "job_name": "cover_letter_writer",
                },
                "handoff_metadata": {"correlation_id": "corr-1"},
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsNotNone(route)
        self.assertEqual(route["agent_label"], "_xworker")
        self.assertGreaterEqual(len(route["messages"]), 3)

        structured_handoff_messages = [
            message
            for message in route["messages"]
            if isinstance(message, dict)
            and str(message.get("role") or "") == "system"
            and "Structured handoff context" in str(message.get("content") or "")
        ]
        self.assertTrue(structured_handoff_messages)

        handoff_message = structured_handoff_messages[0]
        handoff_context = json.loads(handoff_message["content"].split("\n", 1)[1])
        self.assertEqual(handoff_context["protocol"], "agent_handoff_v1")
        self.assertEqual(handoff_context["target_agent"], "_xworker")
        self.assertEqual(handoff_context["metadata"]["correlation_id"], "corr-1")

        handoff_payload_messages = [
            message
            for message in route["messages"]
            if isinstance(message, dict)
            and str(message.get("role") or "") == "user"
            and '"status": "ready"' in str(message.get("content") or "")
        ]
        self.assertTrue(handoff_payload_messages)

        self.assertEqual(route["handoff_context"]["contract"]["handoff_schema"], "xrouter_to_xworker")
        self.assertEqual(route["handoff_context"]["contract"]["handoff_id"], "cover_letter_writer")
        self.assertEqual(route["handoff_context"]["contract"]["job_name"], "cover_letter_writer")

    def test_route_to_agent_rejects_missing_job_name_for_xworker(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "user_question": "continue this thread",
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Invalid route_to_agent payload for _xworker: missing required job_name or tool_name")
        self.assertIsNone(route)

    def test_route_to_agent_promotes_xworker_job_hint_from_tools(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "tool_name": "cover_letter_writer",
                "tools": ["cover_letter_writer"],
                "handoff_protocol": "agent_handoff_v1",
                "handoff_payload": {
                    "output": {
                        "action": "generate_cover_letter",
                        "job_posting_result": {"title": "Engineer"},
                    }
                },
            },
            source_agent_label="_xrouter_xplanner",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("agent_label"), "_xworker")
        self.assertEqual(
            agents_factory.AGENT_EXECUTION_SELECTION_SERVICE.load_job_name(route),
            "cover_letter_writer",
        )

    def test_route_to_agent_rejects_async_without_max_agents(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "job_name": "cover_letter_writer",
                "user_question": "run async writer",
                "run_async": True,
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Invalid route_to_agent payload: run_async=true requires max_agents >= 1")
        self.assertIsNone(route)

    def test_route_to_agent_async_with_max_agents_normalizes_handoff_metadata(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "job_name": "cover_letter_writer",
                "user_question": "run async writer",
                "run_async": True,
                "max_agents": 3,
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        handoff = (route or {}).get("handoff") if isinstance((route or {}).get("handoff"), dict) else {}
        metadata = handoff.get("metadata") if isinstance(handoff.get("metadata"), dict) else {}
        self.assertTrue(bool(metadata.get("run_async")))
        self.assertEqual(int(metadata.get("max_agents") or 0), 3)

    def test_route_to_agent_defaults_target_from_job_runtime_agent(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "job_name": "document_dispatch",
                "message_text": "Dispatch documents for parsing in the specified directory.",
                "handoff_protocol": "agent_handoff_v1",
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual((route or {}).get("agent_label"), "_xworker")
        self.assertEqual(((route or {}).get("handoff") or {}).get("target_agent"), "_xworker")
        self.assertEqual(
            (((route or {}).get("handoff_context") or {}).get("contract") or {}).get("job_name"),
            "document_dispatch",
        )

    def test_route_to_agent_rejects_unstructured_parser_dispatch_message(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "job_name": "job_posting_parser",
                "message_text": (
                    "Parse job descriptions from the PDFs listed in the dispatcher DB "
                    "located at /dispatch/home/example/dispatcher_doc_db.json."
                ),
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("agent_label"), "_xworker")

    def test_route_to_agent_uses_workflow_route_guard_config(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "job_name": "job_posting_parser",
                "handoff_protocol": "agent_handoff_v1",
                "handoff_payload": {
                    "agent_label": "_xworker",
                    "handoff_to": "_xworker",
                    "output": {
                        "type": "file",
                    },
                },
                "handoff_metadata": {
                    "correlation_id": "corr-1",
                },
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("agent_label"), "_xworker")

    def test_route_to_agent_accepts_structured_parser_dispatch_payload(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "job_name": "job_posting_parser",
                "handoff_protocol": "agent_handoff_v1",
                "user_question": "Parse the dispatched file payload",
                "handoff_payload": {
                    "agent_label": "_xplaner_xrouter",
                    "handoff_to": "_xworker",
                    "output": {
                        "file": {
                            "path": "/tmp/job_posting.pdf",
                        }
                    },
                },
                "handoff_metadata": {
                    "dispatcher_db_path": "/tmp/dispatcher_doc_db.json",
                    "correlation_id": "corr-1",
                },
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("agent_label"), "_xworker")

    def test_route_to_agent_router_planner_job_applies_sequence_defaults(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "job_name": "router_planner_cover_letter_sequence",
                "profile_id": "profile:ada-lovelace",
                "job_posting": {
                    "job_title": "AI Engineer",
                },
                "options": {
                    "language": "de",
                    "tone": "modern",
                },
            },
            source_agent_label="_xrouter_xplanner",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("agent_label"), "_xworker")

        handoff = route.get("handoff") if isinstance(route.get("handoff"), dict) else {}
        metadata = handoff.get("metadata") if isinstance(handoff.get("metadata"), dict) else {}
        payload = handoff.get("handoff_payload") if isinstance(handoff.get("handoff_payload"), dict) else {}
        output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        sequence = output.get("sequence") if isinstance(output.get("sequence"), dict) else {}

        self.assertEqual(metadata.get("sequence_name"), "dispatch_parse_generate_cover_letter")
        self.assertEqual(metadata.get("parser_job_name"), "job_posting_parser")
        self.assertEqual(metadata.get("writer_job_name"), "cover_letter_writer")
        self.assertEqual(output.get("action"), "generate_cover_letter")
        self.assertEqual(output.get("profile_id"), "profile:ada-lovelace")
        self.assertEqual(sequence.get("name"), "dispatch_parse_generate_cover_letter")
        self.assertEqual(
            output.get("applicant_profile"),
            {"source": "profile_id", "value": "profile:ada-lovelace"},
        )
        self.assertEqual((output.get("job_posting") or {}).get("job_title"), "AI Engineer")

    def test_parser_job_config_declares_workflow_name(self) -> None:
        job_config = agents_configurator.get_job_config("job_posting_parser")
        workflow_name = str(job_config.get("workflow_name") or "")

        self.assertEqual(workflow_name, "xworker_job_posting_parser_leaf")

        workflow_config = agents_configurator.get_workflow_config(workflow_name)
        self.assertEqual(workflow_config.get("entry_state"), "job_posting_parser_active")
        route_guard = workflow_config.get("route_guard") if isinstance(workflow_config.get("route_guard"), dict) else {}
        self.assertEqual(route_guard, {})

        transitions = workflow_config.get("transitions") if isinstance(workflow_config.get("transitions"), list) else []
        activation_transition = next(
            (
                transition
                for transition in transitions
                if transition.get("from") == "job_posting_parser_active"
                and isinstance(transition.get("on"), dict)
                and transition.get("on", {}).get("kind") == "state"
                and transition.get("to") == "job_posting_parser_complete"
            ),
            None,
        )
        self.assertIsNotNone(activation_transition)
        activation_on = ((activation_transition or {}).get("on") or {})
        activation_names = activation_on.get("name") if isinstance(activation_on.get("name"), list) else []
        self.assertIn("followup_complete", activation_names)
        self.assertIn("routed_agent_complete", activation_names)
        activation_conditions = (activation_on.get("conditions") or {}).get("any")
        self.assertIn({"result": {"exists": True}}, activation_conditions or [])
        self.assertIn({"target_agent": "_xworker"}, activation_conditions or [])

    def test_route_to_agent_defaults_parser_jobs_to_doc_reader_tools(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "_xworker",
                "job_name": "applicant_profile_parser",
                "user_question": "Parse the applicant profile from this folder",
            },
            source_agent_label="_xplaner_xrouter",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        tool_names = {
            str((tool.get("function") or {}).get("name") or "")
            for tool in (route or {}).get("tools") or []
            if isinstance(tool, dict)
        }
        self.assertIn("read_document", tool_names)
        self.assertIn("pypdf_read_document", tool_names)
        self.assertIn("list_documents", tool_names)
        self.assertNotIn("execute_action_request", tool_names)

    def test_route_to_agent_resolves_job_name_target_before_handoff_policy(self) -> None:
        result, route = agents_factory.execute_route_to_agent(
            {
                "target_agent": "applicant_profile_parser",
                "user_question": "Parse this applicant profile",
            },
            source_agent_label="_xrouter_xplanner",
        )

        self.assertEqual(result, "Routing to _xworker")
        self.assertIsInstance(route, dict)
        self.assertEqual(route.get("agent_label"), "_xworker")
        self.assertEqual(
            agents_factory.AGENT_EXECUTION_SELECTION_SERVICE.load_job_name(route),
            "applicant_profile_parser",
        )

    def test_parser_job_default_tools_are_runtime_config_driven(self) -> None:
        self.assertFalse(hasattr(agents_factory.AgentExecutionSelectionService, "_XWORKER_DEFAULT_JOB_TOOLS"))

        parser_job_config = agents_configurator.get_job_config("applicant_profile_parser")
        default_tool_names = parser_job_config.get("default_tool_names") if isinstance(parser_job_config.get("default_tool_names"), list) else []

        self.assertEqual(
            default_tool_names,
            ["read_document", "pypdf_read_document", "list_documents"],
        )

    def test_dispatch_documents_auto_fanout_routes_once_per_handoff_message(self) -> None:
        history = agents_factory.get_history()
        history._thread_iD = 656
        history._history_ = [{"role": "user", "content": "scan folder and parse all", "thread-id": history._thread_iD}]

        dispatch_result = {
            "agent": "xworker",
            "job_name": "document_dispatch",
            "scan_dir": "/tmp/jobs",
            "handoff_messages": [
                {
                    "target_agent": "_xworker",
                    "handoff_protocol": "agent_handoff_v1",
                    "handoff_payload": {
                        "agent_label": "_xworker",
                        "handoff_to": "_xworker",
                        "output": {
                            "job_name": "job_posting_parser",
                            "correlation_id": "sha-1",
                            "file": {"path": "/tmp/jobs/a.pdf", "content_sha256": "sha-1"},
                            "requested_actions": ["parse"],
                        },
                    },
                    "handoff_metadata": {"correlation_id": "sha-1"},
                },
                {
                    "target_agent": "_xworker",
                    "handoff_protocol": "agent_handoff_v1",
                    "handoff_payload": {
                        "agent_label": "_xworker",
                        "handoff_to": "_xworker",
                        "output": {
                            "correlation_id": "sha-2",
                            "file": {"path": "/tmp/jobs/b.pdf", "content_sha256": "sha-2"},
                            "requested_actions": ["parse"],
                        },
                    },
                    "handoff_metadata": {"correlation_id": "sha-2", "parser_job_name": "job_posting_parser"},
                },
            ],
        }

        execute_calls: list[tuple[str, dict]] = []

        def _execute_tool(name: str, args: dict, tool_call_id: str | None = None, source_agent_label: str | None = None):
            execute_calls.append((name, dict(args or {})))
            if name == "dispatch_documents":
                return dispatch_result, None
            if name == "route_to_agent":
                return (
                    "Routing to _xworker",
                    {
                        "messages": [
                            {"role": "system", "content": "xworker system"},
                            {"role": "user", "content": "parse payload"},
                        ],
                        "tools": [],
                        "model": "gpt-test",
                        "agent_label": "_xworker",
                        "include_history": False,
                    },
                )
            return "ok", None

        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "dispatch_documents",
                    json.dumps({"scan_dir": "/tmp/jobs", "db_path": "/tmp/dispatcher.json"}, ensure_ascii=False),
                    call_id="call_dispatcher_fanout",
                )
            ],
        )

        with patch("alde.chat_completion.ChatComE", _FakeChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_execute_tool,
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xworker")

        route_calls = [args for tool_name, args in execute_calls if tool_name == "route_to_agent"]
        self.assertEqual(len(route_calls), 2)
        self.assertTrue(all(str(route_args.get("job_name") or "") == "job_posting_parser" for route_args in route_calls))

        parsed_result = json.loads(result)
        self.assertEqual(len(parsed_result.get("handoff_results") or []), 2)
        self.assertEqual(parsed_result["handoff_results"], ["xworker ok", "xworker ok"])

    def test_dispatch_documents_parallel_fanout_preserves_handoff_result_order(self) -> None:
        history = agents_factory.get_history()
        history._thread_iD = 657
        history._history_ = [{"role": "user", "content": "scan folder and parse all", "thread-id": history._thread_iD}]

        dispatch_result = {
            "agent": "xworker",
            "job_name": "document_dispatch",
            "scan_dir": "/tmp/jobs",
            "handoff_messages": [
                {
                    "target_agent": "_xworker",
                    "handoff_protocol": "agent_handoff_v1",
                    "handoff_payload": {
                        "agent_label": "_xworker",
                        "handoff_to": "_xworker",
                        "output": {
                            "job_name": "job_posting_parser",
                            "correlation_id": "sha-1",
                            "file": {"path": "/tmp/jobs/a.pdf", "content_sha256": "sha-1"},
                            "requested_actions": ["parse"],
                        },
                    },
                    "handoff_metadata": {"correlation_id": "sha-1"},
                },
                {
                    "target_agent": "_xworker",
                    "handoff_protocol": "agent_handoff_v1",
                    "handoff_payload": {
                        "agent_label": "_xworker",
                        "handoff_to": "_xworker",
                        "output": {
                            "correlation_id": "sha-2",
                            "file": {"path": "/tmp/jobs/b.pdf", "content_sha256": "sha-2"},
                            "requested_actions": ["parse"],
                        },
                    },
                    "handoff_metadata": {"correlation_id": "sha-2", "parser_job_name": "job_posting_parser"},
                },
            ],
        }

        execute_calls: list[tuple[str, dict]] = []

        class _EchoChatComE:
            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self.messages = list(_messages)

            def _response(self):
                user_content = ""
                for message in reversed(self.messages):
                    if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "user":
                        user_content = str(message.get("content") or "")
                        break
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=user_content, tool_calls=None)
                        )
                    ]
                )

        def _execute_tool(name: str, args: dict, tool_call_id: str | None = None, source_agent_label: str | None = None):
            execute_calls.append((name, dict(args or {})))
            if name == "dispatch_documents":
                return dispatch_result, None
            if name == "route_to_agent":
                correlation_id = str((args.get("handoff_metadata") or {}).get("correlation_id") or "")
                if correlation_id == "sha-1":
                    time.sleep(0.05)
                else:
                    time.sleep(0.01)
                return (
                    f"Routing to _xworker ({correlation_id})",
                    {
                        "messages": [
                            {"role": "system", "content": "xworker system"},
                            {"role": "user", "content": correlation_id},
                        ],
                        "tools": [],
                        "model": "gpt-test",
                        "agent_label": "_xworker",
                        "include_history": False,
                    },
                )
            return "ok", None

        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "dispatch_documents",
                    json.dumps({"scan_dir": "/tmp/jobs", "db_path": "/tmp/dispatcher.json"}, ensure_ascii=False),
                    call_id="call_dispatcher_parallel",
                )
            ],
        )

        with patch.dict(
            os.environ,
            {
                "ALDE_HANDOFF_PARALLEL_ENABLED": "1",
                "ALDE_HANDOFF_PARALLEL_WORKERS": "3",
            },
            clear=False,
        ), patch("alde.chat_completion.ChatComE", _EchoChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_execute_tool,
        ):
            config = agents_factory.TOOL_CALL_EXECUTION_SERVICE.load_handoff_parallel_object_config("tool_handoff")
            self.assertTrue(config["parallel_enabled"])
            self.assertEqual(config["worker_count"], 3)
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xworker")

        route_calls = [args for tool_name, args in execute_calls if tool_name == "route_to_agent"]
        self.assertEqual(len(route_calls), 2)

        parsed_result = json.loads(result)
        self.assertEqual(parsed_result.get("handoff_results"), ["sha-1", "sha-2"])

    def test_dispatch_documents_cover_letter_flow_chains_parser_to_writer(self) -> None:
        history = agents_factory.get_history()
        history._thread_iD = 660
        history._history_ = [{"role": "user", "content": "generate cover letter from job pdf", "thread-id": history._thread_iD}]

        dispatch_result = {
            "agent": "xworker",
            "job_name": "document_dispatch",
            "scan_dir": "/tmp/jobs",
            "handoff_messages": [
                {
                    "target_agent": "_xworker",
                    "handoff_protocol": "agent_handoff_v1",
                    "handoff_payload": {
                        "agent_label": "_xworker",
                        "handoff_to": "_xworker",
                        "output": {
                            "type": "file",
                            "job_name": "job_posting_parser",
                            "action": "generate_cover_letter",
                            "correlation_id": "sha-cover-1",
                            "link": {"thread_id": "thread-1", "message_id": "msg-1"},
                            "file": {"path": "/tmp/jobs/a.pdf", "content_sha256": "sha-cover-1"},
                            "db": {"processing_state": "queued"},
                            "requested_actions": ["parse", "extract_text", "store_object_result", "mark_processed_on_success"],
                            "applicant_profile": {
                                "source": "text",
                                "value": {
                                    "profile_id": "profile:test",
                                    "preferences": {"language": "de"},
                                },
                            },
                            "options": {"language": "de", "tone": "modern", "max_words": 320},
                        },
                    },
                    "handoff_metadata": {
                        "correlation_id": "sha-cover-1",
                        "dispatcher_message_id": "msg-1",
                        "dispatcher_db_path": "/tmp/dispatcher.json",
                        "obj_name": "job_postings",
                        "obj_db_path": "/tmp/job_postings.json",
                        "job_name": "job_posting_parser",
                        "sequence_name": "dispatch_parse_generate_cover_letter",
                        "parser_job_name": "job_posting_parser",
                        "writer_job_name": "cover_letter_writer",
                    },
                }
            ],
        }

        original_execute_tool = agents_factory.execute_tool

        class _SequenceChatComE:
            call_count = 0

            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self.messages = list(_messages)

            def _response(self):
                type(self).call_count += 1
                if type(self).call_count == 1:
                    content = json.dumps(
                        {
                            "agent": "xworker",
                            "job_name": "job_posting_parser",
                            "correlation_id": "sha-cover-1",
                            "parse": {"is_job_posting": True, "language": "de", "errors": [], "warnings": []},
                            "job_posting": {"job_title": "Platform Engineer", "company_name": "Example Co"},
                            "db_updates": {"processing_state": "processed", "processed": True},
                            "file": {"content_sha256": "sha-cover-1"},
                        },
                        ensure_ascii=False,
                    )
                else:
                    content = json.dumps(
                        {
                            "agent": "xworker",
                            "job_name": "cover_letter_writer",
                            "correlation": {
                                "correlation_id": "sha-cover-1",
                                "job_posting_correlation_id": "sha-cover-1",
                                "profile_correlation_id": "profile:test",
                            },
                            "cover_letter": {"full_text": "Anschreiben"},
                            "cv": {"full_text": "Lebenslauf"},
                            "document": {"full_text": "Anschreiben\n<!-- pagebreak -->\nLebenslauf"},
                            "pages": [
                                {"page": 1, "title": "Application", "page_content": "Anschreiben"},
                                {"page": 2, "title": "CV", "page_content": "Lebenslauf"},
                            ],
                        },
                        ensure_ascii=False,
                    )
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])

        def _execute_tool(name: str, args: dict, tool_call_id: str | None = None, source_agent_label: str | None = None):
            if name == "dispatch_documents":
                return dispatch_result, None
            return original_execute_tool(name, args, tool_call_id, source_agent_label=source_agent_label)

        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "dispatch_documents",
                    json.dumps({"scan_dir": "/tmp/jobs", "db_path": "/tmp/dispatcher.json"}, ensure_ascii=False),
                    call_id="call_dispatcher_cover_letter",
                )
            ],
        )

        with patch("alde.chat_completion.ChatComE", _SequenceChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_execute_tool,
        ), patch.object(agents_factory.DOCUMENT_OBJECT_SERVICE, "upsert_object_record", return_value={"ok": True}), patch(
            "alde.agents_factory.write_document",
            return_value={"path": "/tmp/cover_letter.md"},
        ), patch(
            "alde.agents_factory.md_to_pdf",
            return_value={"pdf_path": "/tmp/cover_letter.pdf"},
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xworker")

        parsed_result = json.loads(result)
        handoff_results = parsed_result.get("handoff_results") or []
        self.assertEqual(len(handoff_results), 1)
        writer_result = json.loads(handoff_results[0])
        self.assertEqual(writer_result.get("job_name"), "cover_letter_writer")
        self.assertEqual(writer_result.get("correlation", {}).get("correlation_id"), "sha-cover-1")

    def test_nested_handoff_planner_worker_worker_parallel_branch_payload(self) -> None:
        history = agents_factory.get_history()
        history._thread_iD = 661
        history._history_ = [{"role": "user", "content": "fanout nested route", "thread-id": history._thread_iD}]

        class _NestedBranchChatComE:
            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self.messages = list(_messages)

            def _response(self):
                user_content = ""
                for message in reversed(self.messages):
                    if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "user":
                        user_content = str(message.get("content") or "")
                        break

                has_parallel_tool_result = any(
                    isinstance(message, dict)
                    and str(message.get("role") or "").strip().lower() == "tool"
                    and "router_parallel_branches" in str(message.get("content") or "")
                    for message in self.messages
                )

                if user_content == "fanout-root" and not has_parallel_tool_result:
                    msg = SimpleNamespace(
                        content="",
                        tool_calls=[
                            _tool_call(
                                "route_to_agent",
                                json.dumps(
                                    {
                                        "target_agent": "_xworker",
                                        "job_name": "cover_letter_writer",
                                        "user_question": "leaf-a",
                                        "run_async": True,
                                        "max_agents": 2,
                                    },
                                    ensure_ascii=False,
                                ),
                                call_id="nested_route_a",
                            ),
                            _tool_call(
                                "route_to_agent",
                                json.dumps(
                                    {
                                        "target_agent": "_xworker",
                                        "job_name": "cover_letter_writer",
                                        "user_question": "leaf-b",
                                        "run_async": True,
                                        "max_agents": 2,
                                    },
                                    ensure_ascii=False,
                                ),
                                call_id="nested_route_b",
                            ),
                        ],
                    )
                    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

                if user_content in {"leaf-a", "leaf-b"}:
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content=user_content, tool_calls=None)
                            )
                        ]
                    )

                if has_parallel_tool_result:
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(content="fanout-complete", tool_calls=None)
                            )
                        ]
                    )

                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="ok", tool_calls=None)
                        )
                    ]
                )

        execute_calls: list[tuple[str, dict[str, object], str | None]] = []
        original_execute_tool = agents_factory.execute_tool

        def _execute_tool(name: str, args: dict, tool_call_id: str | None = None, source_agent_label: str | None = None):
            execute_calls.append((name, dict(args or {}), source_agent_label))
            return original_execute_tool(name, args, tool_call_id, source_agent_label=source_agent_label)

        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "route_to_agent",
                    json.dumps(
                        {
                            "target_agent": "_xworker",
                            "job_name": "adb_worker",
                            "user_question": "fanout-root",
                        },
                        ensure_ascii=False,
                    ),
                    call_id="root_route",
                )
            ],
        )

        with patch.dict(
            os.environ,
            {
                "ALDE_ROUTER_BRANCH_PARALLEL_ENABLED": "1",
                "ALDE_ROUTER_BRANCH_PARALLEL_WORKERS": "2",
            },
            clear=False,
        ), patch("alde.chat_completion.ChatComE", _NestedBranchChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_execute_tool,
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xplaner_xrouter")

        self.assertTrue(isinstance(result, str) and result.strip())
        worker_route_calls = [
            route_args
            for tool_name, route_args, source_agent_label in execute_calls
            if tool_name == "route_to_agent" and source_agent_label == "_xworker"
        ]
        self.assertEqual(len(worker_route_calls), 2)
        self.assertTrue(all(bool(route_args.get("run_async")) for route_args in worker_route_calls))
        self.assertTrue(all(int(route_args.get("max_agents") or 0) == 2 for route_args in worker_route_calls))

    def test_router_parallel_branch_mode_uses_async_max_agents_override(self) -> None:
        history = agents_factory.get_history()
        history._thread_iD = 662
        history._history_ = [{"role": "user", "content": "run async branch override", "thread-id": history._thread_iD}]

        class _EchoChatComE:
            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self.messages = list(_messages)

            def _response(self):
                user_content = ""
                for message in reversed(self.messages):
                    if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "user":
                        user_content = str(message.get("content") or "")
                        break
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=user_content, tool_calls=None)
                        )
                    ]
                )

        def _execute_tool(name: str, args: dict, tool_call_id: str | None = None, source_agent_label: str | None = None):
            if name == "route_to_agent":
                branch_name = str(args.get("user_question") or "")
                if branch_name == "branch-1":
                    time.sleep(0.03)
                else:
                    time.sleep(0.01)
                return (
                    f"Routing to _xworker ({branch_name})",
                    {
                        "messages": [
                            {"role": "system", "content": "xworker system"},
                            {"role": "user", "content": branch_name},
                        ],
                        "tools": [],
                        "model": "gpt-test",
                        "agent_label": "_xworker",
                        "include_history": False,
                    },
                )
            return "ok", None

        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "route_to_agent",
                    json.dumps(
                        {
                            "target_agent": "_xworker",
                            "job_name": "cover_letter_writer",
                            "user_question": "branch-1",
                            "run_async": True,
                            "max_agents": 2,
                        },
                        ensure_ascii=False,
                    ),
                    call_id="override_branch_1",
                ),
                _tool_call(
                    "route_to_agent",
                    json.dumps(
                        {
                            "target_agent": "_xworker",
                            "job_name": "cover_letter_writer",
                            "user_question": "branch-2",
                            "run_async": True,
                            "max_agents": 2,
                        },
                        ensure_ascii=False,
                    ),
                    call_id="override_branch_2",
                ),
            ],
        )

        with patch.dict(
            os.environ,
            {
                "ALDE_ROUTER_BRANCH_PARALLEL_ENABLED": "0",
                "ALDE_ROUTER_BRANCH_PARALLEL_WORKERS": "1",
            },
            clear=False,
        ), patch("alde.chat_completion.ChatComE", _EchoChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_execute_tool,
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xplaner_xrouter")

        parsed_result = json.loads(result)
        self.assertEqual(parsed_result.get("mode"), "router_parallel_branches")
        self.assertEqual(parsed_result.get("configured_worker_count"), 2)
        self.assertEqual(parsed_result.get("requested_max_agents"), 2)
        self.assertEqual(parsed_result.get("branch_results"), ["branch-1", "branch-2"])

    def test_router_parallel_branch_mode_merges_results_in_tool_call_order(self) -> None:
        history = agents_factory.get_history()
        history._thread_iD = 658
        history._history_ = [{"role": "user", "content": "run two routed branches", "thread-id": history._thread_iD}]

        execute_calls: list[tuple[str, dict]] = []

        class _EchoChatComE:
            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self.messages = list(_messages)

            def _response(self):
                user_content = ""
                for message in reversed(self.messages):
                    if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "user":
                        user_content = str(message.get("content") or "")
                        break
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=user_content, tool_calls=None)
                        )
                    ]
                )

        def _execute_tool(name: str, args: dict, tool_call_id: str | None = None, source_agent_label: str | None = None):
            execute_calls.append((name, dict(args or {})))
            if name == "route_to_agent":
                branch_name = str(args.get("user_question") or "")
                if branch_name == "branch-1":
                    time.sleep(0.05)
                else:
                    time.sleep(0.01)
                return (
                    f"Routing to _xworker ({branch_name})",
                    {
                        "messages": [
                            {"role": "system", "content": "xworker system"},
                            {"role": "user", "content": branch_name},
                        ],
                        "tools": [],
                        "model": "gpt-test",
                        "agent_label": "_xworker",
                        "include_history": False,
                    },
                )
            return "ok", None

        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "route_to_agent",
                    json.dumps(
                        {
                            "target_agent": "_xworker",
                            "job_name": "cover_letter_writer",
                            "user_question": "branch-1",
                        },
                        ensure_ascii=False,
                    ),
                    call_id="route_branch_1",
                ),
                _tool_call(
                    "route_to_agent",
                    json.dumps(
                        {
                            "target_agent": "_xworker",
                            "job_name": "cover_letter_writer",
                            "user_question": "branch-2",
                        },
                        ensure_ascii=False,
                    ),
                    call_id="route_branch_2",
                ),
            ],
        )

        with patch.dict(
            os.environ,
            {
                "ALDE_ROUTER_BRANCH_PARALLEL_ENABLED": "1",
                "ALDE_ROUTER_BRANCH_PARALLEL_WORKERS": "4",
            },
            clear=False,
        ), patch("alde.chat_completion.ChatComE", _EchoChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_execute_tool,
        ):
            config = agents_factory.TOOL_CALL_EXECUTION_SERVICE.load_router_branch_parallel_object_config("router_branch")
            self.assertTrue(config["parallel_enabled"])
            self.assertEqual(config["worker_count"], 4)
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xplaner_xrouter")

        route_calls = [args for tool_name, args in execute_calls if tool_name == "route_to_agent"]
        self.assertEqual(len(route_calls), 2)

        parsed_result = json.loads(result)
        self.assertEqual(parsed_result.get("mode"), "router_parallel_branches")
        self.assertEqual(parsed_result.get("branch_count"), 2)
        self.assertEqual(parsed_result.get("branch_results"), ["branch-1", "branch-2"])

    def test_router_parallel_branch_mode_marks_partial_fail_on_timeout(self) -> None:
        history = agents_factory.get_history()
        history._thread_iD = 659
        history._history_ = [{"role": "user", "content": "run timeout branch", "thread-id": history._thread_iD}]

        class _EchoChatComE:
            def __init__(self, _model: str, _messages: list, tools: list[dict], tool_choice: str) -> None:
                self.messages = list(_messages)

            def _response(self):
                user_content = ""
                for message in reversed(self.messages):
                    if isinstance(message, dict) and str(message.get("role") or "").strip().lower() == "user":
                        user_content = str(message.get("content") or "")
                        break
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=user_content, tool_calls=None)
                        )
                    ]
                )

        def _execute_tool(name: str, args: dict, tool_call_id: str | None = None, source_agent_label: str | None = None):
            if name == "route_to_agent":
                branch_name = str(args.get("user_question") or "")
                if branch_name == "branch-timeout":
                    time.sleep(0.12)
                else:
                    time.sleep(0.01)
                return (
                    f"Routing to _xworker ({branch_name})",
                    {
                        "messages": [
                            {"role": "system", "content": "xworker system"},
                            {"role": "user", "content": branch_name},
                        ],
                        "tools": [],
                        "model": "gpt-test",
                        "agent_label": "_xworker",
                        "include_history": False,
                    },
                )
            return "ok", None

        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "route_to_agent",
                    json.dumps(
                        {
                            "target_agent": "_xworker",
                            "job_name": "cover_letter_writer",
                            "user_question": "branch-timeout",
                        },
                        ensure_ascii=False,
                    ),
                    call_id="route_branch_timeout",
                ),
                _tool_call(
                    "route_to_agent",
                    json.dumps(
                        {
                            "target_agent": "_xworker",
                            "job_name": "cover_letter_writer",
                            "user_question": "branch-ok",
                        },
                        ensure_ascii=False,
                    ),
                    call_id="route_branch_ok",
                ),
            ],
        )

        with patch.dict(
            os.environ,
            {
                "ALDE_ROUTER_BRANCH_PARALLEL_ENABLED": "1",
                "ALDE_ROUTER_BRANCH_PARALLEL_WORKERS": "2",
                "ALDE_ROUTER_BRANCH_TIMEOUT_SECONDS": "0.05",
            },
            clear=False,
        ), patch("alde.chat_completion.ChatComE", _EchoChatComE), patch(
            "alde.agents_factory.execute_tool",
            side_effect=_execute_tool,
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xplaner_xrouter")
            runtime_status = agents_factory.get_router_parallel_runtime_status()

        parsed_result = json.loads(result)
        self.assertEqual(parsed_result.get("mode"), "router_parallel_branches")
        self.assertTrue(parsed_result.get("partial_fail"))
        self.assertEqual(parsed_result.get("timeout_branch_count"), 1)
        self.assertEqual((parsed_result.get("branch_status_counts") or {}).get("timeout"), 1)
        branch_results = parsed_result.get("branch_results") or []
        self.assertEqual(len(branch_results), 2)
        self.assertIsInstance(branch_results[0], dict)
        self.assertEqual(branch_results[0].get("status"), "timeout")
        self.assertEqual(branch_results[1], "branch-ok")
        self.assertGreaterEqual(int(runtime_status.get("total_timeout_branches") or 0), 1)
        self.assertTrue(bool((runtime_status.get("config") or {}).get("parallel_enabled")))

    def test_router_parallel_branch_config_reads_hard_timeout_flag(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ALDE_ROUTER_BRANCH_PARALLEL_ENABLED": "1",
                "ALDE_ROUTER_BRANCH_PARALLEL_WORKERS": "5",
                "ALDE_ROUTER_BRANCH_TIMEOUT_SECONDS": "2.5",
                "ALDE_ROUTER_BRANCH_HARD_TIMEOUT_ENABLED": "1",
            },
            clear=False,
        ):
            config = agents_factory.TOOL_CALL_EXECUTION_SERVICE.load_router_branch_parallel_object_config("router_branch")

        self.assertTrue(config["parallel_enabled"])
        self.assertEqual(config["worker_count"], 5)
        self.assertEqual(config["timeout_seconds"], 2.5)
        self.assertTrue(config["hard_timeout_enabled"])

    def test_dispatch_documents_returns_tool_result_without_followup_when_no_handoff_exists(self) -> None:
        history = agents_factory.get_history()
        history._thread_iD = 655
        history._history_ = [{"role": "user", "content": "scan this folder", "thread-id": history._thread_iD}]

        dispatch_result = {
            "agent": "xworker",
            "job_name": "document_dispatch",
            "scan_dir": "/tmp/jobs",
            "processed": 0,
            "handoff_messages": [],
        }
        agent_msg = SimpleNamespace(
            content="",
            tool_calls=[
                _tool_call(
                    "dispatch_documents",
                    json.dumps(
                        {
                            "scan_dir": "/tmp/jobs",
                            "db_path": "/tmp/dispatcher.json",
                        },
                        ensure_ascii=False,
                    ),
                    call_id="call_dispatcher_terminal",
                )
            ],
        )

        with patch("alde.chat_completion.ChatComE") as chat_cls, patch(
            "alde.agents_factory.execute_tool",
            return_value=(dispatch_result, None),
        ):
            result = agents_factory._handle_tool_calls(agent_msg, agent_label="_xworker")

        self.assertEqual(result, json.dumps(dispatch_result, ensure_ascii=False))
        chat_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()