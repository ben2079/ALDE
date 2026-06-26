import os
import unittest
from unittest.mock import patch

from alde.agents_db import AgentDbSocketRepository


class AgentsDbSocketTimeoutDefaultsTest(unittest.TestCase):
    def test_socket_timeout_defaults_to_longer_window(self):
        self.assertGreaterEqual(AgentDbSocketRepository._load_socket_timeout_seconds(), 90.0)

    def test_healthcheck_requests_use_shorter_timeout(self):
        with patch.dict(os.environ, {"AI_IDE_KNOWLEDGE_AGENTS_DB_HEALTHCHECK_TIMEOUT_SECONDS": "1.25"}, clear=False):
            repository = AgentDbSocketRepository.create_from_uri("agentsdb://127.0.0.1:2331")
            self.assertAlmostEqual(
                repository._load_request_timeout_seconds({"cmd": "health", "payload": {}}),
                1.25,
            )


if __name__ == "__main__":
    unittest.main()
