import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alde import ai_ide_v1756


class GuiEnvConfigTest(unittest.TestCase):
    def test_normalize_gui_env_entries_reads_nested_env_payload(self) -> None:
        payload = {
            "format": "ai_ide_gui_env_v1",
            "env": {
                "AI_IDE_SAFE": True,
                "AI_IDE_CONTROL_PLANE_REFRESH_MS": 45000,
                "OPENAI_API_KEY": "",
                "description": "ignored",
            },
        }

        result = ai_ide_v1756._normalize_gui_env_entries(payload)

        self.assertEqual(result["AI_IDE_SAFE"], "1")
        self.assertEqual(result["AI_IDE_CONTROL_PLANE_REFRESH_MS"], "45000")
        self.assertIn("OPENAI_API_KEY", result)
        self.assertNotIn("description", result)

    def test_loader_skips_empty_values_but_loads_non_empty_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "gui_env.json"
            config_path.write_text(
                json.dumps(
                    {
                        "format": "ai_ide_gui_env_v1",
                        "env": {
                            "AI_IDE_CONTROL_PLANE_REFRESH_MS": 45000,
                            "QT_QPA_PLATFORM": "offscreen",
                            "OPENAI_API_KEY": "",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"AI_IDE_GUI_ENV_CONFIG_PATH": str(config_path)},
                clear=True,
            ):
                loaded_path = ai_ide_v1756._load_gui_env_config_into_process()

                self.assertEqual(loaded_path, config_path)
                self.assertEqual(os.getenv("AI_IDE_CONTROL_PLANE_REFRESH_MS"), "45000")
                self.assertEqual(os.getenv("QT_QPA_PLATFORM"), "offscreen")
                self.assertNotIn("OPENAI_API_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()