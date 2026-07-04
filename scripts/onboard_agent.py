from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.store import Store


DEFAULT_AUTH_FILE = Path("data/agentrelay-auth.json")
DEFAULT_DB_PATH = Path("data/agentrelay.sqlite3")
DEFAULT_ENV_DIR = Path("data/local-env")
DEFAULT_ONBOARDING_DIR = Path("data/onboarding")
DEFAULT_BASE_URL = "https://server.stellarix.space/agentrelay/api"


def main() -> None:
    args = parse_args()
    if args.command == "prepare":
        result = prepare(args)
    elif args.command == "conformance":
        result = conformance(args)
    elif args.command == "promote":
        result = promote(args)
    else:
        raise SystemExit(f"unknown command: {args.command}")
    print(json.dumps(result, indent=2, sort_keys=True))


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    slug = slugify(args.slug)
    auth_file = Path(args.auth_file)
    db_path = Path(args.db_path)
    env_dir = Path(args.env_dir)
    onboarding_dir = Path(args.onboarding_dir)
    base_url = args.base_url

    identities = read_identities(auth_file)
    agent_a = conformance_identity(slug, "a")
    agent_b = conformance_identity(slug, "b")

    created = []
    store = Store(str(db_path))
    for label, identity in [("agent_a", agent_a), ("agent_b", agent_b)]:
        token = secrets.token_urlsafe(32)
        identities = upsert_identity(identities, identity["username"], identity["agent_id"], token)
        store.upsert_agent(
            agent_id=identity["agent_id"],
            owner="AgentRelay Conformance",
            name=f"{slug} onboarding conformance {label[-1].upper()}",
            description=f"Disposable Protocol v0.3 conformance identity for onboarding {slug}.",
        )
        env_file = write_env_file(
            env_dir / "onboarding" / slug / f"{identity['username']}.env",
            base_url=base_url,
            username=identity["username"],
            agent_id=identity["agent_id"],
            token=token,
        )
        created.append(
            {
                "label": label,
                "username": identity["username"],
                "agent_id": identity["agent_id"],
                "env_file": str(env_file),
            }
        )

    write_identities(auth_file, identities)
    manifest = {
        "slug": slug,
        "status": "prepared",
        "updated_at": now(),
        "base_url": base_url,
        "conformance": {
            "agent_a": created[0],
            "agent_b": created[1],
        },
    }
    write_manifest(onboarding_dir, slug, manifest)

    return {
        "ok": True,
        "action": "prepare",
        "slug": slug,
        "status": "prepared",
        "auth_file": str(auth_file),
        "manifest_file": str(manifest_path(onboarding_dir, slug)),
        "conformance_agents": created,
        "next_action": (
            "Restart/reload the relay so the new conformance identities are active, "
            "then run scripts/onboard_agent.py conformance "
            f"{slug} --base-url {base_url}"
        ),
    }


def conformance(args: argparse.Namespace) -> dict[str, Any]:
    slug = slugify(args.slug)
    onboarding_dir = Path(args.onboarding_dir)
    auth_file = Path(args.auth_file)
    manifest = read_manifest(onboarding_dir, slug)
    conformance_data = manifest.get("conformance") or {}
    agent_a_meta = conformance_data.get("agent_a") or {}
    agent_b_meta = conformance_data.get("agent_b") or {}

    identities = read_identities(auth_file)
    agent_a = find_identity(
        identities,
        username=required(agent_a_meta, "username"),
        agent_id=required(agent_a_meta, "agent_id"),
    )
    agent_b = find_identity(
        identities,
        username=required(agent_b_meta, "username"),
        agent_id=required(agent_b_meta, "agent_id"),
    )

    base_url = args.base_url or manifest.get("base_url") or DEFAULT_BASE_URL
    env = os.environ.copy()
    env.update(
        {
            "AGENTRELAY_CONFORMANCE_BASE_URL": base_url,
            "AGENTRELAY_CONFORMANCE_AGENT_A_ID": agent_a["agent_id"],
            "AGENTRELAY_CONFORMANCE_AGENT_A_USERNAME": agent_a["username"],
            "AGENTRELAY_CONFORMANCE_AGENT_A_TOKEN": agent_a["token"],
            "AGENTRELAY_CONFORMANCE_AGENT_B_ID": agent_b["agent_id"],
            "AGENTRELAY_CONFORMANCE_AGENT_B_USERNAME": agent_b["username"],
            "AGENTRELAY_CONFORMANCE_AGENT_B_TOKEN": agent_b["token"],
        }
    )
    proc = subprocess.run(
        [
            "python3",
            "scripts/protocol_v03_conformance_runner.py",
            "--timeout-seconds",
            str(args.timeout_seconds),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        manifest["status"] = "conformance_failed"
        manifest["updated_at"] = now()
        manifest["conformance_result"] = {
            "ok": False,
            "stderr": proc.stderr.strip(),
        }
        write_manifest(onboarding_dir, slug, manifest)
        raise SystemExit(proc.stderr.strip() or "conformance failed")

    result = json.loads(proc.stdout)
    manifest["status"] = "conformance_passed"
    manifest["updated_at"] = now()
    manifest["conformance_result"] = result
    write_manifest(onboarding_dir, slug, manifest)
    return {
        "ok": True,
        "action": "conformance",
        "slug": slug,
        "status": "conformance_passed",
        "result": result,
        "next_action": (
            "Promote the real agent with scripts/onboard_agent.py promote "
            f"<username> --onboarding-slug {slug} --require-conformance"
        ),
    }


def promote(args: argparse.Namespace) -> dict[str, Any]:
    slug = slugify(args.onboarding_slug or args.username)
    onboarding_dir = Path(args.onboarding_dir)
    manifest = read_manifest(onboarding_dir, slug) if manifest_path(onboarding_dir, slug).exists() else {"slug": slug}
    if args.require_conformance and manifest.get("status") != "conformance_passed":
        raise SystemExit(
            f"onboarding slug {slug!r} has not passed conformance; "
            "run prepare + conformance first or omit --require-conformance"
        )

    username = args.username
    agent_id = args.agent_id or default_agent_id(username)
    owner = args.owner or default_owner(username)
    auth_file = Path(args.auth_file)
    db_path = Path(args.db_path)
    env_dir = Path(args.env_dir)
    base_url = args.base_url
    token = secrets.token_urlsafe(32)

    identities = upsert_identity(read_identities(auth_file), username, agent_id, token)
    write_identities(auth_file, identities)
    agent = Store(str(db_path)).upsert_agent(
        agent_id=agent_id,
        owner=owner,
        name=args.name or f"{owner} Agent",
        description=args.description or f"Personal coordinator agent for {owner}.",
    )
    env_file = write_env_file(
        env_dir / f"{slugify(agent_id.removesuffix('-agent'))}.env",
        base_url=base_url,
        username=username,
        agent_id=agent_id,
        token=token,
    )

    manifest["status"] = "promoted"
    manifest["updated_at"] = now()
    manifest["candidate"] = {
        "username": username,
        "agent_id": agent_id,
        "owner": owner,
        "name": agent["name"],
        "description": agent["description"],
        "env_file": str(env_file),
    }
    write_manifest(onboarding_dir, slug, manifest)
    return {
        "ok": True,
        "action": "promote",
        "slug": slug,
        "status": "promoted",
        "agent": manifest["candidate"],
        "auth_file": str(auth_file),
        "manifest_file": str(manifest_path(onboarding_dir, slug)),
        "next_action": (
            "Restart/reload the relay, privately send the env file to the agent owner, "
            "then ask the owner to run the MCP doctor/health tools."
        ),
    }


def read_identities(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array")
    return raw


def write_identities(path: Path, identities: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identities, indent=2) + "\n")
    path.chmod(0o600)


def upsert_identity(
    identities: list[dict[str, Any]],
    username: str,
    agent_id: str,
    token: str,
) -> list[dict[str, Any]]:
    kept = [
        item
        for item in identities
        if item.get("username") != username and item.get("agent_id") != agent_id
    ]
    kept.append({"username": username, "agent_id": agent_id, "token": token})
    return kept


def find_identity(identities: list[dict[str, Any]], *, username: str, agent_id: str) -> dict[str, Any]:
    for identity in identities:
        if identity.get("username") == username and identity.get("agent_id") == agent_id:
            token = str(identity.get("token") or "")
            if not token:
                raise ValueError(f"identity {username}/{agent_id} is missing token")
            return {
                "username": username,
                "agent_id": agent_id,
                "token": token,
            }
    raise ValueError(f"identity {username}/{agent_id} was not found in auth file")


def write_env_file(
    path: Path,
    *,
    base_url: str,
    username: str,
    agent_id: str,
    token: str,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"AGENTRELAY_BASE_URL={base_url}\n"
        f"AGENTRELAY_AGENT_ID={agent_id}\n"
        f"AGENTRELAY_USERNAME={username}\n"
        f"AGENTRELAY_TOKEN={token}\n"
    )
    path.chmod(0o600)
    return path


def read_manifest(onboarding_dir: Path, slug: str) -> dict[str, Any]:
    path = manifest_path(onboarding_dir, slug)
    if not path.exists():
        raise FileNotFoundError(f"onboarding manifest not found: {path}")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def write_manifest(onboarding_dir: Path, slug: str, manifest: dict[str, Any]) -> None:
    path = manifest_path(onboarding_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def manifest_path(onboarding_dir: Path, slug: str) -> Path:
    return onboarding_dir / f"{slug}.json"


def conformance_identity(slug: str, side: str) -> dict[str, str]:
    username = f"{slug}-conformance-{side}"
    return {
        "username": username,
        "agent_id": f"{username}-agent",
    }


def required(value: dict[str, Any], key: str) -> str:
    result = str(value.get(key, "")).strip()
    if not result:
        raise ValueError(f"missing required field: {key}")
    return result


def default_agent_id(username: str) -> str:
    normalized = slugify(username)
    if normalized.endswith("-agent"):
        return normalized
    return f"{normalized}-agent"


def default_owner(username: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", username.strip())
    if not words:
        raise ValueError("username must contain at least one letter or digit")
    return " ".join(word[:1].upper() + word[1:] for word in words)


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not normalized:
        raise ValueError("value must contain at least one letter or digit")
    return normalized


def now() -> int:
    return int(time.time())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare, verify, and promote third-party AgentRelay agents without printing tokens."
    )
    parser.add_argument("--auth-file", default=str(DEFAULT_AUTH_FILE))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--env-dir", default=str(DEFAULT_ENV_DIR))
    parser.add_argument("--onboarding-dir", default=str(DEFAULT_ONBOARDING_DIR))

    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="Create disposable conformance identities.")
    prepare_parser.add_argument("slug", help="Short onboarding slug, for example acme or project-hermes")
    prepare_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)

    conformance_parser = subparsers.add_parser("conformance", help="Run v0.3 conformance for a prepared slug.")
    conformance_parser.add_argument("slug")
    conformance_parser.add_argument("--base-url")
    conformance_parser.add_argument("--timeout-seconds", type=int, default=15)

    promote_parser = subparsers.add_parser("promote", help="Create the real agent identity after conformance.")
    promote_parser.add_argument("username")
    promote_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    promote_parser.add_argument("--agent-id")
    promote_parser.add_argument("--owner")
    promote_parser.add_argument("--name")
    promote_parser.add_argument("--description")
    promote_parser.add_argument("--onboarding-slug")
    promote_parser.add_argument("--require-conformance", action="store_true")

    return parser.parse_args()


if __name__ == "__main__":
    main()
