"""Run with: python -m unittest discover -s deploy -p 'test_*.py'."""
import base64
import json
import unittest

from railway import configuration


class DeploymentBoundaryTest(unittest.TestCase):
    def test_existing_target_and_public_build_credentials_only(self):
        environment = {
            "RAILWAY_PROJECT_ID": "00000000-0000-0000-0000-000000000001",
            "RAILWAY_ENVIRONMENT_ID": "00000000-0000-0000-0000-000000000002",
            "RAILWAY_SERVICE_ID": "00000000-0000-0000-0000-000000000003",
            "NEXT_PUBLIC_SUPABASE_URL": "https://project.supabase.co",
            "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_public",
            "BACKEND_URL": "https://api.example.com",
            "SUPABASE_SECRET_KEY": "private-never-forward-to-frontend",
            "OPENAI_API_KEY": "private-never-forward-to-frontend",
        }
        _, variables = configuration("frontend", environment)
        self.assertEqual(set(variables), {
            "NEXT_PUBLIC_SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", "BACKEND_URL",
        })
        for key in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "BACKEND_URL"):
            with self.subTest(missing=key), self.assertRaises(ValueError):
                configuration("frontend", {name: value for name, value in environment.items() if name != key})
        role = base64.urlsafe_b64encode(json.dumps({"role": "service_role"}).encode()).decode().rstrip("=")
        for value in ("sb_secret_private", f"header.{role}.signature"):
            with self.subTest(secret_public_key=value), self.assertRaises(ValueError):
                configuration("frontend", {**environment, "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY": value})
        for value in ("http://api.example.com", "https://user:password@api.example.com", "https://api.example.com/?secret=x"):
            with self.subTest(unsafe_backend=value), self.assertRaises(ValueError):
                configuration("frontend", {**environment, "BACKEND_URL": value})
        only_target = {key: value for key, value in environment.items() if key.startswith("RAILWAY_")}
        _, variables = configuration("backend", only_target, auth_only=True)
        self.assertEqual(variables, {})


if __name__ == "__main__":
    unittest.main()
