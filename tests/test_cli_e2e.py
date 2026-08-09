from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from renweave.rpa import RpaArchive


class _TranslationHandler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        user_payload = json.loads(request["messages"][-1]["content"])
        rows = [
            {"id": line["id"], "text": f"PT: {line['source']}"}
            for line in user_payload["scene"]["lines"]
        ]
        response = json.dumps({
            "choices": [{"message": {"content": json.dumps({"translations": rows})}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }).encode("utf-8")
        type(self).calls += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args) -> None:
        return


class CliEndToEndTests(unittest.TestCase):
    def test_cli_http_translation_to_verified_rpa(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "HttpGame"
            game = project / "game"
            game.mkdir(parents=True)
            (game / "script.rpy").write_text(
                'label start:\n    narrator "Hello [player]."\n    jump ending\n\n'
                'label ending:\n    narrator "Goodbye."\n    return\n',
                encoding="utf-8",
            )
            workspace = root / "workspace"
            server = ThreadingHTTPServer(("127.0.0.1", 0), _TranslationHandler)
            _TranslationHandler.calls = 0
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                provider = root / "provider.json"
                provider.write_text(json.dumps({
                    "kind": "openai_compatible",
                    "name": "Local E2E",
                    "model": "local-test-model",
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "max_retries": 0,
                }), encoding="utf-8")
                repository = Path(__file__).resolve().parents[1]
                environment = os.environ.copy()
                if environment.get("RENWEAVE_E2E_INSTALLED") != "1":
                    source_path = str(repository / "src")
                    environment["PYTHONPATH"] = (
                        source_path + os.pathsep + environment.get("PYTHONPATH", "")
                    )
                completed = subprocess.run(
                    [
                        sys.executable, "-X", "utf8", "-m", "renweave", "run",
                        str(project),
                        "--workspace", str(workspace),
                        "--provider", str(provider),
                        "--source-language", "English",
                        "--target-language", "pt-BR",
                        "--no-ai-knowledge",
                        "--no-refine",
                    ],
                    cwd=repository,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["stage"], "complete")
            self.assertEqual(state["renpy_language"], "pt_br")
            self.assertEqual(state["total_model_calls"], 2)
            self.assertEqual(state["total_prompt_tokens"], 40)
            self.assertEqual(_TranslationHandler.calls, 2)
            archive_path = Path(state["package_path"])
            self.assertTrue(archive_path.is_file())
            with RpaArchive(archive_path) as archive:
                self.assertEqual(archive.names(), ("tl/pt_br/script.rpy",))
                generated = archive.read("tl/pt_br/script.rpy").decode("utf-8")
            self.assertIn('narrator "PT: Hello [player]."', generated)
            self.assertIn('narrator "PT: Goodbye."', generated)


if __name__ == "__main__":
    unittest.main()
