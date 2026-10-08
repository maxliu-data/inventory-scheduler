import json
from pathlib import Path
import subprocess
import sys
import unittest

from app import app, create_app


class StandaloneAppTests(unittest.TestCase):
    def test_factory_registers_inventory_api(self):
        application = create_app()
        self.assertIsNot(application, app)
        response = application.test_client().post("/inventory_schedule", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["status"], "error")
        self.assertFalse(application.debug)

    def test_application_does_not_expose_chatbot_or_monthly_api(self):
        client = app.test_client()
        for path in ["/", "/kmoai_request", "/monthly_schedule", "/socket.io/"]:
            with self.subTest(path=path):
                self.assertEqual(client.post(path, json={}).status_code, 404)

    def test_import_and_request_without_external_services(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; from app import app; "
                "response = app.test_client().post('/inventory_schedule', json={}); "
                "print(json.dumps({'status': response.status_code, "
                "'modules': sorted(sys.modules)}))",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], 400)
        for name in output["modules"]:
            self.assertNotIn(
                name.split(".")[0],
                {"azure", "redis", "openai", "langchain", "flask_socketio", "eventlet"},
            )


if __name__ == "__main__":
    unittest.main()
