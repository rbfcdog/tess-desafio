#!/usr/bin/env python3
"""Deploy only to an explicitly selected, existing Railway service."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "backend": (
        "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY",
        "OPENAI_API_KEY", "ENCRYPTION_KEY", "PUBLIC_APP_URL",
    ),
    "frontend": (
        "NEXT_PUBLIC_SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", "BACKEND_URL",
    ),
}
OPTIONAL_BACKEND = (
    "OPENAI_MODEL", "MAX_OUTPUT_TOKENS", "CONTEXT_INPUT_TOKENS", "CONTEXT_WINDOW_TOKENS",
    "MAX_MODEL_STEPS", "MAX_TOOL_CALLS", "SUMMARY_OUTPUT_TOKENS", "MCP_TRUSTED_HOSTS",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
)

def configuration(component, environ, *, auth_only=False):
    target = {}
    for key in ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID"):
        value = environ.get(key, "").strip()
        try:
            target[key] = str(UUID(value))
        except ValueError:
            raise ValueError(f"{key} must identify an existing authorized resource (UUID).") from None
    if auth_only:
        return target, {}
    variables = {}
    for key in REQUIRED[component]:
        value = environ.get(key, "").strip()
        if not value:
            raise ValueError(f"Missing {key}.")
        variables[key] = value
    for key, value in variables.items():
        if key.endswith("URL"):
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment):
                raise ValueError(f"{key} must be an absolute HTTPS URL without credentials/query/fragment.")
        if "PUBLISHABLE_KEY" in key:
            if value.startswith("sb_publishable_") and len(value) > len("sb_publishable_"):
                continue
            try:
                payload = value.split(".")[1]
                role = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["role"]
            except (IndexError, ValueError, KeyError, TypeError):
                raise ValueError(f"{key} must be a public Supabase publishable/anon key.") from None
            if role != "anon":
                raise ValueError(f"{key} must never contain a service-role or secret key.")
    if component == "backend":
        variables.update({key: environ[key] for key in OPTIONAL_BACKEND if key in environ})
    return target, variables


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=REQUIRED)
    parser.add_argument("--check-auth", action="store_true", help="Read-only service authorization probe; no changes/upload.")
    args = parser.parse_args()
    try:
        target, variables = configuration(args.component, os.environ, auth_only=args.check_auth)
    except ValueError as error:
        parser.error(str(error))
    binary = os.environ.get("RAILWAY_BIN") or shutil.which("railway")
    if not binary:
        parser.error("Install the official @railway/cli, or set RAILWAY_BIN to its native executable.")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
    if version.returncode:
        parser.error("Railway CLI cannot start. The npm launcher may lack bin/railway; repair with: npm rebuild --global @railway/cli --foreground-scripts")
    scope = ["--project", target["RAILWAY_PROJECT_ID"], "--environment", target["RAILWAY_ENVIRONMENT_ID"], "--service", target["RAILWAY_SERVICE_ID"]]
    # Capture all variable output: Railway JSON contains plaintext secrets.
    authorization = subprocess.run([binary, "variable", *scope, "--json"], capture_output=True, timeout=30)
    if authorization.returncode:
        parser.error("Service authorization failed. Supply an authorized RAILWAY_TOKEN/RAILWAY_API_TOKEN or run railway login; verify the explicit resource IDs.")
    print("Authorized access to the selected existing service confirmed.")
    if args.check_auth:
        return
    for key, value in variables.items():
        result = subprocess.run(
            [binary, "variable", "set", *scope, key, "--stdin", "--skip-deploys"],
            input=value, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            parser.error(f"Setting {key} failed; upload was not attempted. Previously set variables remain configured.")
    result = subprocess.run(
        [binary, "up", str(ROOT / args.component), "--path-as-root", *scope, "--ci"],
        cwd=ROOT, check=False,
    )
    if result.returncode:
        sys.exit(result.returncode)
    print("Upload/build command completed. Confirm the deployment status and public health URL; this alone is not proof of a live deployment.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.TimeoutExpired):
        sys.exit("Railway command could not execute or timed out; no deployment success is asserted.")
