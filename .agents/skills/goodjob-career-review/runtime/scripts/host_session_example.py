#!/usr/bin/env python3
"""Reference example showing how a host agent integrates with the GoodJob session broker.

This script demonstrates the official lifecycle:
1. Initialize SessionClient with task-scoped workspace and state directory.
2. Run preflight check before execution.
3. Manage broker subprocess lifetime via context manager.
4. Obtain owner consent and authorize source analysis.
5. Validate target role / job description input.
6. Trigger scan and inspect structured overview with grouped issue summaries.
7. Automatically and deterministically reap the broker subprocess upon completion.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add runtime src to sys.path
RUNTIME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_DIR / "src"))

from goodjob.session_client import (  # noqa: E402
    SessionClient,
    SessionClientError,
    SessionPreflightError,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GoodJob Host Session Client Reference Integration"
    )
    parser.add_argument("--workspace", required=True, help="Path to authorized workspace")
    parser.add_argument("--data-dir", default=None, help="Path to GoodJob state directory")
    parser.add_argument("--role", default="系统工程师", help="Target role name for career review")
    args = parser.parse_args()

    workspace_path = Path(args.workspace).resolve()
    print(f"[host] Initializing session client for workspace: {workspace_path}")

    client = SessionClient(
        workspace=workspace_path,
        data_dir=args.data_dir,
    )

    # Step 1: Preflight
    print("[host] Running platform preflight check...")
    try:
        preflight_report = client.run_preflight()
        status = preflight_report.get("status")
        can_start = preflight_report.get("can_start_broker")
        print(f"[host] Preflight status: {status} (can_start: {can_start})")
        if not can_start:
            print("[host] Preflight failed; cannot start broker session.", file=sys.stderr)
            return 2
    except SessionPreflightError as exc:
        print(f"[host] Preflight error: {exc}", file=sys.stderr)
        return 2

    # Step 2: Task-scoped broker session
    print("[host] Starting broker session...")
    try:
        with client:
            # Step 3: Owner consent & authorization
            print("[host] Authorizing source analysis with explicit owner confirmation...")
            auth_resp = client.authorize_source_analysis(confirmed=True)
            if auth_resp.get("status") != "ok":
                print(f"[host] Authorization failed: {auth_resp}", file=sys.stderr)
                return 1
            receipt_id = auth_resp["receipt"]["authorization_receipt_id"]
            print(f"[host] Authorization confirmed. Receipt ID: {receipt_id}")

            # Step 4: Validate Job Input
            print(f"[host] Validating target role '{args.role}'...")
            val_resp = client.validate_job_input(
                authorization_receipt_id=receipt_id, target_role=args.role
            )
            if val_resp.get("status") != "ok":
                print(f"[host] Job input validation failed: {val_resp}", file=sys.stderr)
                return 1
            val_sha = val_resp["job_input"]["validation_sha256"]

            # Step 5: Scan
            print("[host] Triggering workspace scan...")
            scan_resp = client.scan(
                authorization_receipt_id=receipt_id,
                job_input_validation_sha256=val_sha,
            )
            if scan_resp.get("status") != "ok":
                print(f"[host] Scan failed: {scan_resp}", file=sys.stderr)
                return 1
            print(
                f"[host] Scan completed with status: {scan_resp.get('scan_run', {}).get('status')}"
            )

            # Step 6: Scan Overview
            overview_resp = client.scan_overview(
                authorization_receipt_id=receipt_id,
                job_input_validation_sha256=val_sha,
            )
            overview = overview_resp.get("scan_overview", {})
            limits = overview.get("limits", {})
            groups = overview.get("issue_groups", [])
            avail = limits.get("available_issues")
            print(f"[host] Overview: total issues = {avail}, groups = {len(groups)}")
            for group in groups:
                sev = group.get("severity")
                kind = group.get("kind")
                cnt = group.get("count")
                omit = group.get("omitted_count")
                print(f"  - [{sev}] {kind}: count={cnt}, omitted={omit}")

        print("[host] Session completed cleanly. Broker subprocess reaped.")
        return 0

    except SessionClientError as exc:
        print(f"[host] Session error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
