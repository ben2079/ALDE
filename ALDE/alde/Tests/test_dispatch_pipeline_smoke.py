from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PKG_ROOT = Path(__file__).resolve().parents[2]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from alde.agents_tools import (
    DOCUMENT_OBJECT_SERVICE,
    DOCUMENT_DISPATCH_SERVICE,
    DOCUMENT_REPOSITORY,
    execute_action_request_tool,
    read_document,
)


# Tiny but valid single-page PDF fixture for end-to-end dispatch smoke tests.
_MINIMAL_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>\nendobj\n"
    b"4 0 obj\n<< /Length 44 >>\nstream\nBT /F1 12 Tf 50 100 Td (Dispatch Smoke) Tj ET\nendstream\nendobj\n"
    b"xref\n0 5\n0000000000 65535 f \n0000000010 00000 n \n0000000062 00000 n \n0000000118 00000 n \n0000000205 00000 n \n"
    b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n300\n%%EOF\n"
)


class TestDispatchPipelineSmoke(unittest.TestCase):
    def test_dispatch_generates_parser_handoff_for_real_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scan_dir = tmp_path / "scan"
            scan_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = scan_dir / "job_offer.pdf"
            pdf_path.write_bytes(_MINIMAL_PDF_BYTES)

            dispatcher_db_path = tmp_path / "dispatcher_doc_db.json"

            result = DOCUMENT_DISPATCH_SERVICE.dispatch_documents(
                scan_dir=str(scan_dir),
                db_path=str(dispatcher_db_path),
                thread_id="thread-smoke",
                dispatcher_message_id="msg-smoke",
                recursive=False,
                dry_run=False,
            )

            self.assertEqual(result.get("job_name"), "document_dispatch")
            self.assertEqual(result.get("summary", {}).get("pdf_found"), 1)
            self.assertEqual(result.get("summary", {}).get("new"), 1)
            self.assertEqual(result.get("summary", {}).get("errors"), 0)

            forwarded = result.get("forwarded") or []
            self.assertEqual(len(forwarded), 1)
            correlation_id = str(forwarded[0].get("content_sha256") or "")
            self.assertTrue(correlation_id)

            handoff_messages = result.get("handoff_messages") or []
            self.assertEqual(len(handoff_messages), 1)
            handoff = handoff_messages[0] if isinstance(handoff_messages[0], dict) else {}
            self.assertEqual(handoff.get("protocol"), "agent_handoff_v1")

            payload = handoff.get("handoff_payload") if isinstance(handoff.get("handoff_payload"), dict) else {}
            metadata = handoff.get("metadata") if isinstance(handoff.get("metadata"), dict) else {}
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}

            self.assertEqual(payload.get("handoff_to"), "_xworker")
            self.assertEqual(output.get("type"), "file")
            self.assertEqual(output.get("correlation_id"), correlation_id)
            self.assertEqual(output.get("requested_actions"), ["parse", "extract_text", "store_object_result", "mark_processed_on_success"])
            self.assertEqual(metadata.get("correlation_id"), correlation_id)
            self.assertEqual(metadata.get("dispatcher_message_id"), "msg-smoke")
            self.assertEqual(metadata.get("dispatcher_db_path"), str(dispatcher_db_path.resolve()))
            self.assertEqual(metadata.get("obj_name"), "job_postings")

            dispatcher_record = DOCUMENT_REPOSITORY.get_dispatcher_record(
                correlation_id,
                db_path=str(dispatcher_db_path),
            )
            self.assertIsInstance(dispatcher_record, dict)
            self.assertEqual((dispatcher_record or {}).get("processing_state"), "queued")
            self.assertEqual((dispatcher_record or {}).get("processed"), False)

    def test_dispatch_treats_job_name_in_agent_name_as_parser_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scan_dir = tmp_path / "scan"
            scan_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = scan_dir / "job_offer.pdf"
            pdf_path.write_bytes(_MINIMAL_PDF_BYTES)

            dispatcher_db_path = tmp_path / "dispatcher_doc_db.json"

            result = DOCUMENT_DISPATCH_SERVICE.dispatch_documents(
                scan_dir=str(scan_dir),
                db_path=str(dispatcher_db_path),
                thread_id="thread-smoke",
                dispatcher_message_id="msg-smoke",
                recursive=False,
                dry_run=False,
                agent_name="job_posting_parser",
            )

            handoff_messages = result.get("handoff_messages") or []
            self.assertEqual(len(handoff_messages), 1)
            handoff = handoff_messages[0] if isinstance(handoff_messages[0], dict) else {}
            payload = handoff.get("handoff_payload") if isinstance(handoff.get("handoff_payload"), dict) else {}
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
            metadata = handoff.get("metadata") if isinstance(handoff.get("metadata"), dict) else {}

            self.assertEqual(handoff.get("target_agent"), "_xworker")
            self.assertEqual(payload.get("handoff_to"), "_xworker")
            self.assertEqual(output.get("job_name"), "job_posting_parser")
            self.assertEqual(metadata.get("job_name"), "job_posting_parser")

    def test_dispatch_treats_job_name_in_parser_agent_name_as_parser_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scan_dir = tmp_path / "scan"
            scan_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = scan_dir / "job_offer.pdf"
            pdf_path.write_bytes(_MINIMAL_PDF_BYTES)

            dispatcher_db_path = tmp_path / "dispatcher_doc_db.json"

            result = DOCUMENT_DISPATCH_SERVICE.dispatch_documents(
                scan_dir=str(scan_dir),
                db_path=str(dispatcher_db_path),
                thread_id="thread-smoke",
                dispatcher_message_id="msg-smoke",
                recursive=False,
                dry_run=False,
                parser_agent_name="_job_posting_parser",
            )

            handoff_messages = result.get("handoff_messages") or []
            self.assertEqual(len(handoff_messages), 1)
            handoff = handoff_messages[0] if isinstance(handoff_messages[0], dict) else {}
            payload = handoff.get("handoff_payload") if isinstance(handoff.get("handoff_payload"), dict) else {}
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
            metadata = handoff.get("metadata") if isinstance(handoff.get("metadata"), dict) else {}

            self.assertEqual(handoff.get("target_agent"), "_xworker")
            self.assertEqual(payload.get("handoff_to"), "_xworker")
            self.assertEqual(output.get("job_name"), "job_posting_parser")
            self.assertEqual(metadata.get("job_name"), "job_posting_parser")

    def test_dispatch_action_preserves_profile_id_for_cover_letter_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scan_dir = tmp_path / "scan"
            scan_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = scan_dir / "job_offer.pdf"
            pdf_path.write_bytes(_MINIMAL_PDF_BYTES)

            dispatcher_db_path = tmp_path / "dispatcher_doc_db.json"
            result_raw = execute_action_request_tool(
                action_request={
                    "action": "dispatch_documents",
                    "scan_dir": str(scan_dir),
                    "db_path": str(dispatcher_db_path),
                    "thread_id": "thread-profile-id",
                    "dispatcher_message_id": "msg-profile-id",
                    "recursive": False,
                    "profile_id": "profile:test",
                    "job_posting": {"job_title": "AI Engineer"},
                    "options": {"language": "de", "tone": "modern"},
                }
            )

            self.assertIsInstance(result_raw, str)
            result = json.loads(result_raw)
            handoff_messages = result.get("handoff_messages") or []
            self.assertEqual(len(handoff_messages), 1)

            handoff = handoff_messages[0] if isinstance(handoff_messages[0], dict) else {}
            payload = handoff.get("handoff_payload") if isinstance(handoff.get("handoff_payload"), dict) else {}
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}

            self.assertEqual(output.get("profile_id"), "profile:test")
            self.assertEqual(
                output.get("applicant_profile"),
                {"source": "profile_id", "value": "profile:test"},
            )
            self.assertEqual(output.get("action"), "generate_cover_letter")
            self.assertEqual((output.get("job_posting") or {}).get("job_title"), "AI Engineer")

    def test_execute_action_request_accepts_legacy_document_dispatch_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scan_dir = tmp_path / "scan"
            scan_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = scan_dir / "job_offer.pdf"
            pdf_path.write_bytes(_MINIMAL_PDF_BYTES)

            dispatcher_db_path = tmp_path / "dispatcher_doc_db.json"
            result_raw = execute_action_request_tool(
                action_request={
                    "action": "document_dispatch",
                    "scan_dir": str(scan_dir),
                    "db_path": str(dispatcher_db_path),
                    "thread_id": "thread-action",
                    "dispatcher_message_id": "msg-action",
                    "recursive": False,
                }
            )

            self.assertIsInstance(result_raw, str)
            result = json.loads(result_raw)
            self.assertEqual(result.get("job_name"), "document_dispatch")
            self.assertIsNone(result.get("error"))
            self.assertEqual(result.get("db", {}).get("reachable"), True)
            self.assertEqual(result.get("summary", {}).get("pdf_found"), 1)
            self.assertEqual(result.get("summary", {}).get("errors"), 0)

    def test_dispatch_resumes_known_processed_cover_letter_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scan_dir = tmp_path / "scan"
            scan_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = scan_dir / "job_offer.pdf"
            pdf_path.write_bytes(_MINIMAL_PDF_BYTES)

            correlation_id = hashlib.sha256(_MINIMAL_PDF_BYTES).hexdigest()
            dispatcher_db_path = tmp_path / "dispatcher_doc_db.json"
            job_postings_db_path = tmp_path / "job_postings_db.json"

            DOCUMENT_REPOSITORY.upsert_dispatcher_record_fields(
                correlation_id=correlation_id,
                db_path=str(dispatcher_db_path),
                record_updates={
                    "id": correlation_id,
                    "content_sha256": correlation_id,
                    "source_path": str(pdf_path),
                    "file_size_bytes": pdf_path.stat().st_size,
                    "mtime_epoch": int(pdf_path.stat().st_mtime),
                    "processing_state": "processed",
                    "processed": True,
                },
            )
            DOCUMENT_OBJECT_SERVICE.store_result(
                object_result={
                    "agent": "job_posting_parser",
                    "correlation_id": correlation_id,
                    "parse": {"language": "de", "is_job_posting": True},
                    "job_posting": {
                        "job_title": "Platform Engineer",
                        "company_name": "Example Co",
                    },
                    "db_updates": {"processing_state": "processed", "processed": True},
                    "file": {"content_sha256": correlation_id},
                    "link": {"thread_id": "thread-existing", "message_id": "msg-existing"},
                },
                correlation_id=correlation_id,
                db_path=str(job_postings_db_path),
                obj_name="job_postings",
            )

            with patch("alde.agents_tools._default_document_db_path", return_value=str(job_postings_db_path)):
                result = DOCUMENT_DISPATCH_SERVICE.dispatch_documents(
                    scan_dir=str(scan_dir),
                    db_path=str(dispatcher_db_path),
                    thread_id="thread-resume",
                    dispatcher_message_id="msg-resume",
                    recursive=False,
                    action="generate_cover_letter",
                    applicant_profile={
                        "source": "text",
                        "value": {
                            "profile_id": "profile:test",
                            "preferences": {"language": "de"},
                        },
                    },
                    options={"language": "de", "tone": "modern", "max_words": 300},
                    dry_run=False,
                )

            self.assertEqual(result.get("summary", {}).get("known_processed"), 1)
            forwarded = result.get("forwarded") or []
            self.assertEqual(len(forwarded), 1)

            handoff_messages = result.get("handoff_messages") or []
            self.assertEqual(len(handoff_messages), 1)
            handoff = handoff_messages[0] if isinstance(handoff_messages[0], dict) else {}
            metadata = handoff.get("metadata") if isinstance(handoff.get("metadata"), dict) else {}
            payload = handoff.get("handoff_payload") if isinstance(handoff.get("handoff_payload"), dict) else {}
            output = payload.get("output") if isinstance(payload.get("output"), dict) else {}

            self.assertEqual(handoff.get("target_agent"), "_xworker")
            self.assertEqual(metadata.get("job_name"), "cover_letter_writer")
            self.assertEqual(metadata.get("correlation_id"), correlation_id)
            self.assertEqual(output.get("action"), "generate_cover_letter")
            self.assertEqual(output.get("job_posting_result", {}).get("correlation_id"), correlation_id)
            self.assertEqual(
                output.get("profile_result", {}).get("profile", {}).get("profile_id"),
                "profile:test",
            )
            self.assertEqual(output.get("options", {}).get("language"), "de")

    def test_read_document_repairs_dispatch_prefixed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dispatcher_db_path = tmp_path / "dispatcher_doc_db.json"
            dispatcher_db_path.write_text('{"documents": {"corr-1": {"processing_state": "queued"}}}', encoding="utf-8")

            prefixed_path = f"/dispatch{dispatcher_db_path.as_posix()}"
            result = read_document(prefixed_path)

            self.assertIn('"documents"', result)
            self.assertIn('"corr-1"', result)


if __name__ == "__main__":
    unittest.main()
