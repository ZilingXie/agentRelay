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
        result = subprocess.run(
            [
                "python3",
                "scripts/upsert_agent_identity.py",
                "Alice Example",
                "--auth-file",
                str(auth_file),
                "--db-path",
                str(db_path),
                "--env-dir",
                str(env_dir),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        identities = json.loads(auth_file.read_text())
        if len(identities) != 1:
            raise AssertionError("expected one identity")
        identity = identities[0]
        if identity["username"] != "Alice Example":
            raise AssertionError("username not stored")
        if identity["agent_id"] != "alice-example-agent":
            raise AssertionError("default agent id not derived")

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ?",
                ("alice-example-agent",),
            ).fetchone()
        if not row:
            raise AssertionError("agent row was not created")
        if row["owner"] != "Alice Example":
            raise AssertionError("agent owner not stored")
        if row["name"] != "Alice Example Agent":
            raise AssertionError("agent name not stored")

        env_file = env_dir / "alice-example.env"
        env_text = env_file.read_text()
        for expected in [
            "AGENTRELAY_AGENT_ID=alice-example-agent",
            "AGENTRELAY_USERNAME=Alice Example",
            "AGENTRELAY_TOKEN=",
        ]:
            if expected not in env_text:
                raise AssertionError(f"missing env line: {expected}")
        print(json.dumps({"ok": True, "agent_id": identity["agent_id"]}, indent=2))
        if "Created/updated agent registry row" not in result.stdout:
            raise AssertionError("script did not report agent registry update")


if __name__ == "__main__":
    main()
