"""Shared bounded replay checks for live benchmark integration tests."""

import json
import re
import shutil
import subprocess
from pathlib import Path

from benchmark.tests.test_m1_interface import ENGINE


def verify_replay(test_case, engine, final, directory, fixtures):
    """Check every recorded boundary and the final hash, including logged mismatch errors."""
    replay = Path(final["data"]["replay_directory"]) / "commands.txt"
    text = replay.read_text()
    test_case.assertIn('"type":"benchmark-action"', text)
    test_case.assertEqual(len(re.findall(r"^turn ", text, re.MULTILINE)), final["turn"])
    test_case.assertTrue(replay.with_name("metadata.json").is_file())
    profile = directory / "replay"
    profile.mkdir()
    env = engine.env.copy()
    for key in ("DATA", "CONFIG", "CACHE", "STATE"):
        env[f"XDG_{key}_HOME"] = str(profile / key.lower())
    fixture_name = json.loads((fixtures / "mod.json").read_text())["name"]
    shutil.copytree(fixtures, profile / "data/0ad/mods" / fixture_name)
    log_path = profile / "replay.log"
    with log_path.open("w") as log:
        result = subprocess.run(
            [str(ENGINE), f"--replay={replay}", "--hashtest-full=true"],
            env=env,
            cwd=profile,
            stdout=log,
            stderr=log,
            timeout=120,
            check=False,
        )
    log = log_path.read_text()
    test_case.assertEqual(result.returncode, 0, log[-3000:])
    test_case.assertNotIn("MISMATCH", log)
    test_case.assertNotIn("ERROR:", log)
    test_case.assertEqual(log.count("hash ok"), len(re.findall(r"^hash ", text, re.MULTILINE)))
    test_case.assertIn("# Final state: " + final["data"]["state_hash"], log)
