#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/fix_eval_branches.py
─────────────────────────────
One-off script to correct the s03 and s06 eval branches that were created
with buggy _apply_patch output, and to add the 'status' enum field to the
main branch so s06 has a valid base to patch against.

Steps:
  1. Add status enum field to main branch openapi.yaml
  2. Re-apply s03 patch (path rename v2→v3) and push to eval/s03 branch
  3. Re-apply s06 patch (enum rename active→enabled) and push to eval/s06 branch
"""
from __future__ import annotations

import asyncio
import base64
import copy
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

import httpx
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger("fix_eval_branches")

REPO = "K11-Software-Solutions/k11-user-service"
TOKEN = os.getenv("GITHUB_TOKEN", "")
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "Authorization": f"Bearer {TOKEN}",
}


# ── GitHub helpers ────────────────────────────────────────────────────────────

async def get_file(client: httpx.AsyncClient, path: str, ref: str) -> tuple[str, str]:
    """Returns (content_str, blob_sha)."""
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    r = await client.get(url, headers=GH_HEADERS, params={"ref": ref})
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


async def update_file(
    client: httpx.AsyncClient,
    path: str,
    content: str,
    message: str,
    branch: str,
    file_sha: str,
) -> None:
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "sha": file_sha,
        "branch": branch,
    }
    r = await client.put(url, headers=GH_HEADERS, json=body)
    r.raise_for_status()
    logger.info("Updated %s on branch %s", path, branch)


# ── Patch helpers ─────────────────────────────────────────────────────────────

def apply_rename_paths(spec: dict, rename_map: dict) -> dict:
    """Rename path prefixes: {"v2": "v3"} → /api/v2/foo → /api/v3/foo."""
    spec = copy.deepcopy(spec)
    for old_marker, new_marker in rename_map.items():
        new_paths = {}
        for path_key, path_val in spec.get("paths", {}).items():
            new_key = path_key.replace(f"/api/{old_marker}/", f"/api/{new_marker}/")
            new_paths[new_key] = path_val
        spec["paths"] = new_paths
    spec["info"]["version"] = "2.0.0"
    return spec


def apply_change_enum(spec: dict, schema: str, prop: str, enum_values: list) -> dict:
    """Update enum values for an existing schema property."""
    spec = copy.deepcopy(spec)
    schema_props = spec["components"]["schemas"][schema]["properties"]
    if prop in schema_props:
        schema_props[prop]["enum"] = enum_values
    spec["info"]["version"] = "1.1.0"
    return spec


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:

        # ── Step 1: Add 'status' field to main branch ─────────────────────────
        logger.info("=== Step 1: Adding status field to main branch ===")
        main_yaml, main_sha = await get_file(client, "openapi.yaml", "main")
        main_spec = yaml.safe_load(main_yaml)

        user_props = main_spec["components"]["schemas"]["User"]["properties"]
        if "status" not in user_props:
            user_props["status"] = {
                "type": "string",
                "enum": ["active", "inactive"],
                "description": "User account status",
            }
            updated_yaml = yaml.dump(main_spec, default_flow_style=False, sort_keys=False)
            await update_file(
                client, "openapi.yaml", updated_yaml,
                "feat: add status enum field to User schema",
                "main", main_sha,
            )
            # Refresh main spec and sha after update
            main_yaml, main_sha = await get_file(client, "openapi.yaml", "main")
            main_spec = yaml.safe_load(main_yaml)
            logger.info("status field added to main")
        else:
            logger.info("status field already present on main — skipping")

        # ── Step 2: Fix s03 branch (v2→v3 path rename) ───────────────────────
        logger.info("=== Step 2: Fixing s03 branch (path rename v2→v3) ===")
        branch_s03 = "eval/s03-api-v3-migration"
        _, s03_file_sha = await get_file(client, "openapi.yaml", branch_s03)

        s03_spec = apply_rename_paths(main_spec, {"v2": "v3"})
        s03_yaml = yaml.dump(s03_spec, default_flow_style=False, sort_keys=False)

        await update_file(
            client, "openapi.yaml", s03_yaml,
            "BREAKING: Migrate all endpoints from /api/v2/ to /api/v3/",
            branch_s03, s03_file_sha,
        )

        # ── Step 3: Fix s06 branch (enum rename active→enabled) ──────────────
        logger.info("=== Step 3: Fixing s06 branch (enum rename) ===")
        branch_s06 = "eval/s06-status-enum-rename"
        _, s06_file_sha = await get_file(client, "openapi.yaml", branch_s06)

        s06_spec = apply_change_enum(main_spec, "User", "status", ["enabled", "disabled"])
        s06_yaml = yaml.dump(s06_spec, default_flow_style=False, sort_keys=False)

        await update_file(
            client, "openapi.yaml", s06_yaml,
            "UNCERTAIN: Rename status enum values (active→enabled, inactive→disabled)",
            branch_s06, s06_file_sha,
        )

        # ── Report new head SHAs ──────────────────────────────────────────────
        for branch_name, label in [(branch_s03, "s03"), (branch_s06, "s06")]:
            r = await client.get(
                f"https://api.github.com/repos/{REPO}/git/ref/heads/{branch_name}",
                headers=GH_HEADERS,
            )
            r.raise_for_status()
            sha = r.json()["object"]["sha"]
            logger.info("%s new head SHA: %s", label, sha[:12])

    logger.info("=== Done. Re-run seed_evaluation.py --run-only to collect calibration data ===")


if __name__ == "__main__":
    asyncio.run(main())
