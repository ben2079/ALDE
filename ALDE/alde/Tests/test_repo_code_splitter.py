from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PKG_ROOT = Path(__file__).resolve().parents[2]
if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

from alde.agents_db import AgentDbInMemoryRepository, KnowledgeObjectService
from alde.agents_tools import get_tool_spec, repo_knowledge_worker, repo_knowledge_query
from alde.repo_code_splitter import (
    RepoIndexService,
    PythonCodeSplitter,
    _build_default_runtime_config,
    load_repo_context_for_ide_agent,
)


class _FakeEmbeddingService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def process_object(self, object_name: str, obj: dict, *, owner_id: str) -> dict[str, bool]:
        _ = obj
        self.calls.append((object_name, owner_id))
        return {"stored": True}


class TestRepoCodeSplitter(unittest.TestCase):
    def test_index_repo_builds_document_entity_relation_and_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "sample.py").write_text(
                "import os\n\nclass Demo:\n    pass\n\ndef helper():\n    return os.getcwd()\n",
                encoding="utf-8",
            )

            repo = AgentDbInMemoryRepository(str(root / "agentsdb_image.json"))
            repo.ensure_index_objects()
            knowledge_service = KnowledgeObjectService(repo)
            embedding_service = _FakeEmbeddingService()
            service = RepoIndexService(
                repo,
                knowledge_service,
                embedding_service,
                workers=1,
                runtime_config=_build_default_runtime_config(),
            )

            result = service.index_repo_object(str(root), extensions=(".py",))

            self.assertEqual(result["files_found"], 1)
            self.assertGreaterEqual(result["total_blocks"], 3)
            self.assertGreaterEqual(result["total_entities"], 4)
            self.assertGreaterEqual(result["total_relations"], 3)

            documents = repo.load_objects("document", limit=10)
            entities = repo.load_objects("entity", limit=20)
            relations = repo.load_objects("relation", limit=20)

            self.assertEqual(len(documents), 1)
            self.assertIn("module", {entity.get("entity_type") for entity in entities})
            self.assertIn("defines_class", {relation.get("relation_type") for relation in relations})
            self.assertEqual(
                {owner_type for owner_type, _ in embedding_service.calls},
                {"block", "entity", "relation"},
            )

    def test_repo_knowledge_worker_is_registered_and_scans_python_files(self) -> None:
        spec = get_tool_spec("repo_knowledge_worker")
        self.assertIsNotNone(spec)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "alpha.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "ignore.md").write_text("# ignored\n", encoding="utf-8")

            result = repo_knowledge_worker("scan", root_dir=str(root), extensions=[".py"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["operation"], "scan")
            self.assertEqual([Path(path).name for path in result["files"]], ["alpha.py"])

    def test_repo_knowledge_query_is_registered_and_returns_context(self) -> None:
        """repo_knowledge_query must be registered and return a well-formed result dict."""
        spec = get_tool_spec("repo_knowledge_query")
        self.assertIsNotNone(spec)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "mymodule.py").write_text(
                "import os\n\nclass Finder:\n    def find(self):\n        return os.getcwd()\n",
                encoding="utf-8",
            )
            image_path = str(root / "agentsdb_rkq_image.json")

            # First index the test repo so there is something to query.
            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()
            knowledge_service = KnowledgeObjectService(repo)
            emb_svc = _FakeEmbeddingService()
            index_service = RepoIndexService(
                repo,
                knowledge_service,
                emb_svc,
                workers=1,
                runtime_config=_build_default_runtime_config(),
            )
            index_service.index_repo_object(str(root), extensions=(".py",))

            # Flush the image so the query helper can load it.
            if hasattr(repo, "_flush_image"):
                repo._flush_image()

            # Now query using the snapshot image (no env vars needed).
            result = repo_knowledge_query(
                "Finder class find method",
                owner_types=["block", "entity"],
                limit=5,
                image_path=image_path,
                use_vector=False,   # no model loaded in unit-test env
            )

            self.assertTrue(result["ok"], msg=result.get("error"))
            self.assertIn("chunks", result)
            self.assertIn("total", result)
            self.assertIsInstance(result["chunks"], list)

    # -----------------------------------------------------------------------
    # Unified Splitter API
    # -----------------------------------------------------------------------

    def test_python_code_splitter_split_text_returns_string_list(self) -> None:
        """PythonCodeSplitter.split_text() must return a non-empty list[str]."""
        source = (
            "import os\n\nclass Runner:\n    def run(self):\n        return os.getpid()\n"
            "\ndef helper():\n    pass\n"
        )
        splitter = PythonCodeSplitter()
        chunks = splitter.split_text(source, filename="runner.py")
        self.assertIsInstance(chunks, list)
        self.assertTrue(all(isinstance(c, str) for c in chunks))
        self.assertGreaterEqual(len(chunks), 2, msg="Expected at least imports + class blocks")

    def test_create_text_splitter_python_ast_strategy(self) -> None:
        """_create_text_splitter('python_ast') must return the AST adapter."""
        try:
            from alde.vstores import _create_text_splitter
        except ImportError as exc:
            self.skipTest(f"vstores not importable ({exc})")

        adapter = _create_text_splitter(chunk_strategy="python_ast")
        source = "import sys\n\ndef greet():\n    return 'hi'\n"
        chunks = adapter.split_text(source)
        self.assertIsInstance(chunks, list)
        self.assertTrue(len(chunks) >= 1)

    def test_load_repo_context_for_ide_agent_returns_attach_entries(self) -> None:
        """load_repo_context_for_ide_agent() must return dicts with title/language/content/source_path."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "demo.py").write_text(
                "import os\n\nclass Widget:\n    def paint(self):\n        pass\n",
                encoding="utf-8",
            )
            image_path = str(root / "ide_loader_image.json")

            repo = AgentDbInMemoryRepository(image_path)
            repo.ensure_index_objects()
            knowledge_service = KnowledgeObjectService(repo)
            emb_svc = _FakeEmbeddingService()
            index_service = RepoIndexService(
                repo, knowledge_service, emb_svc, workers=1,
                runtime_config=_build_default_runtime_config(),
            )
            index_service.index_repo_object(str(root), extensions=(".py",))
            if hasattr(repo, "_flush_image"):
                repo._flush_image()

            entries = load_repo_context_for_ide_agent(
                "Widget paint method",
                limit=3,
                image_path=image_path,
                use_vector=False,
            )

            self.assertIsInstance(entries, list)
            if entries:
                required_keys = {"title", "language", "content", "source_path"}
                for entry in entries:
                    self.assertTrue(required_keys.issubset(entry.keys()), msg=f"Missing keys in {entry}")
                    self.assertIsInstance(entry["content"], str)
                    self.assertTrue(entry["content"].strip(), msg="content must not be empty")


if __name__ == "__main__":
    unittest.main()