from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        auth_file = tmpdir / "auth.json"
        db_path = tmpdir / "agentrelay.sqlite3"
        env_dir = tmpdir / "env"
        onboarding_dir = tmpdir / "onboarding"

        prepare = run(
            [
                "python3",
                "scripts/onboard_agent.py",
                "--auth-file",
                str(auth_file),
                "--db-path",
                str(db_path),
                "--env-dir",
                str(env_dir),
                "--onboarding-dir",
                str(onboarding_dir),
                "prepare",
                "Acme Team",
            ]
        )
        prepare_data = json.loads(prepare.stdout)
        if prepare_data["slug"] != "acme-team":
            raise AssertionError("prepare did not normalize slug")
        if prepare_data["status"] != "prepared":
            raise AssertionError("prepare did not mark status prepared")

        identities = json.loads(auth_file.read_text())
        if len(identities) != 2:
            raise AssertionError("prepare should create two conformance identities")
        tokens = [identity["token"] for identity in identities]
        assert_no_token_leak(prepare.stdout, tokens)

        for agent_id in ["acme-team-conformance-a-agent", "acme-team-conformance-b-agent"]:
            assert_agent_exists(db_path, agent_id)

        manifest_path = onboarding_dir / "acme-team.json"
        manifest = json.loads(manifest_path.read_text())
        if "token" in json.dumps(manifest):
            raise AssertionError("onboarding manifest must not contain tokens")

        blocked = subprocess.run(
            [
                "python3",
                "scripts/onboard_agent.py",
                "--auth-file",
                str(auth_file),
                "--db-path",
                str(db_path),
                "--env-dir",
                str(env_dir),
                "--onboarding-dir",
                str(onboarding_dir),
                "promote",
                "Acme User",
                "--onboarding-slug",
                "acme-team",
                "--require-conformance",
            ],
            text=True,
            capture_output=True,
        )
        if blocked.returncode == 0:
            raise AssertionError("promote --require-conformance should fail before conformance passes")

        manifest["status"] = "conformance_passed"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        promote = run(
            [
                "python3",
                "scripts/onboard_agent.py",
                "--auth-file",
                str(auth_file),
                "--db-path",
                str(db_path),
                "--env-dir",
                str(env_dir),
                "--onboarding-dir",
                str(onboarding_dir),
                "promote",
                "Acme User",
                "--onboarding-slug",
                "acme-team",
                "--require-conformance",
            ]
        )
        promote_data = json.loads(promote.stdout)
        if promote_data["agent"]["agent_id"] != "acme-user-agent":
            raise AssertionError("promote did not derive real agent id")
        assert_agent_exists(db_path, "acme-user-agent")

        updated_identities = json.loads(auth_file.read_text())
        if len(updated_identities) != 3:
            raise AssertionError("promote should add the real identity")
        assert_no_token_leak(promote.stdout, [identity["token"] for identity in updated_identities])

        env_text = (env_dir / "acme-user.env").read_text()
        for expected in [
            "AGENTRELAY_BASE_URL=https://server.stellarix.space/agentrelay/api",
            "AGENTRELAY_AGENT_ID=acme-user-agent",
            "AGENTRELAY_USERNAME=Acme User",
            "AGENTRELAY_TOKEN=",
        ]:
            if expected not in env_text:
                raise AssertionError(f"missing env line: {expected}")

        print(json.dumps({"ok": True, "agent_id": promote_data["agent"]["agent_id"]}, indent=2))


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True)


def assert_agent_exists(db_path: Path, agent_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if not row:
        raise AssertionError(f"missing agent row: {agent_id}")


def assert_no_token_leak(output: str, tokens: list[str]) -> None:
    for token in tokens:
        if token and token in output:
            raise AssertionError("command stdout leaked a token")


if __name__ == "__main__":
    main()
