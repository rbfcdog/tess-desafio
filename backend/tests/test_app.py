import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from app import app


class HealthContractTest(unittest.TestCase):
    def test_health_and_http_boundaries(self):
        with TestClient(app) as client:
            health = client.get("/api/health?probe=1")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.headers["cache-control"], "no-store")
            body = health.json()
            self.assertEqual(body["status"], "ok")
            self.assertIsNotNone(datetime.fromisoformat(body["timestamp"]).tzinfo)
            self.assertGreaterEqual(body["uptimeSeconds"], 0)

            missing = client.get("/api/unknown")
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.headers["cache-control"], "no-store")

            unsupported = client.post("/api/health")
            self.assertEqual(unsupported.status_code, 405)
            self.assertEqual(
                {method.strip() for method in unsupported.headers["allow"].split(",")},
                {"GET", "HEAD"},
            )

            head = client.head("/api/health")
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b"")


if __name__ == "__main__":
    unittest.main()
