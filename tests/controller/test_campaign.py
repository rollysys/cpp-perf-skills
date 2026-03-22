from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.cpp_perf_campaign.config import load_config
from tools.cpp_perf_campaign.db import init_db, open_db
from tools.cpp_perf_campaign.discovery import discover_targets, upsert_targets
from tools.cpp_perf_campaign.runner import run_once, status_summary
from tools.cpp_perf_campaign.watchdog import requeue_stale_runs


HOOK_SCRIPT = """#!/usr/bin/env python3
import json
import os
from pathlib import Path

hook = os.environ["CPP_PERF_HOOK_NAME"]
strategy = os.environ["CPP_PERF_STRATEGY"]
strategy_pass = int(os.environ.get("CPP_PERF_STRATEGY_PASS", "1"))
result_path = Path(os.environ["CPP_PERF_RESULT_PATH"])
result_path.parent.mkdir(parents=True, exist_ok=True)

if hook == "prepare_case":
    payload = {"ok": True}
elif hook == "baseline":
    payload = {"stats": {"median": 100.0, "p99": 120.0, "stable": True}}
elif hook == "optimize":
    payload = {
        "changed": True,
        "rebuild": True,
        "files_touched": [os.environ["CPP_PERF_RELATIVE_TARGET_PATH"]],
        "summary": f"applied {strategy}",
        "notes": f"applied {strategy}",
    }
elif hook == "benchmark":
    median = 80.0 if strategy == "vectorize" else 60.0
    payload = {
        "stats": {"median": median, "p99": median * 1.1, "stable": True},
        "correctness": True,
        "terminal_state": "hardware_limit" if strategy == "layout" and strategy_pass >= 2 else None,
    }
else:
    raise SystemExit(f"unexpected hook: {hook}")

result_path.write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
"""


class CampaignControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "src").mkdir()
        (self.root / "include").mkdir()
        (self.root / "src" / "alpha.cpp").write_text("int alpha() { return 1; }\n", encoding="utf-8")
        (self.root / "src" / "beta.cc").write_text("int beta() { return 2; }\n", encoding="utf-8")
        (self.root / "include" / "beta.h").write_text("int beta();\n", encoding="utf-8")

        self.hook_script = self.root / "fake_hook.py"
        self.hook_script.write_text(HOOK_SCRIPT, encoding="utf-8")
        self.hook_script.chmod(0o755)

        self.config_path = self.root / "campaign.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "campaign_id": "test-campaign",
                    "repo_root": str(self.root),
                    "runtime_root": str(self.root / ".cpp-perf" / "campaigns" / "test-campaign"),
                    "discover": {
                        "include_globs": ["src/**/*.cpp", "src/**/*.cc"],
                        "exclude_globs": [".cpp-perf/**"],
                        "shard_depth": 1,
                        "max_targets": 1,
                    },
                    "budget": {
                        "max_attempts_per_target": 3,
                        "max_low_gain_streak": 3,
                        "stale_after_seconds": 1,
                        "heartbeat_interval_seconds": 1,
                    },
                    "selection": {
                        "keep_min_speedup": 1.05,
                        "low_gain_speedup": 1.01,
                    },
                    "strategies": [
                        {"name": "vectorize", "kind": "explore"},
                        {"name": "layout", "kind": "exploit"},
                    ],
                    "hooks": {
                        "prepare_case": ["python3", str(self.hook_script)],
                        "baseline": ["python3", str(self.hook_script)],
                        "optimize": ["python3", str(self.hook_script)],
                        "benchmark": ["python3", str(self.hook_script)],
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _prepare(self):
        config = load_config(self.config_path)
        init_db(config)
        targets = discover_targets(config)
        with open_db(config.db_path) as connection:
            upsert_targets(connection, targets)
        return config

    def _write_hook_script(self, path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_discover_and_run_twice(self) -> None:
        config = self._prepare()

        first = run_once(config, worker_id="worker-a")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["outcome"], "keep")
        self.assertEqual(first["strategy"], "vectorize")
        self.assertEqual(first["strategy_pass"], 1)
        self.assertIsNone(first["terminal_state"])

        second = run_once(config, worker_id="worker-a")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["outcome"], "keep")
        self.assertEqual(second["strategy"], "layout")
        self.assertEqual(second["strategy_pass"], 1)
        self.assertIsNone(second["terminal_state"])

        third = run_once(config, worker_id="worker-a")
        self.assertEqual(third["status"], "ok")
        self.assertEqual(third["outcome"], "keep")
        self.assertEqual(third["strategy"], "layout")
        self.assertEqual(third["strategy_pass"], 2)
        self.assertEqual(third["terminal_state"], "hardware_limit")

        summary = status_summary(config)
        self.assertIn("completed", summary["counts"])
        self.assertGreaterEqual(summary["counts"]["completed"], 1)

    def test_frontier_priorities_are_respected(self) -> None:
        raw_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw_config["discover"]["max_targets"] = None
        self.config_path.write_text(json.dumps(raw_config, indent=2), encoding="utf-8")
        config = load_config(self.config_path)
        init_db(config)

        frontier_path = self.root / "frontier.jsonl"
        frontier_path.write_text(
            "\n".join(
                [
                    json.dumps({"path": "src/beta.cc", "priority": 9.0, "reason": "hotspot"}),
                    json.dumps({"path": "src/alpha.cpp", "priority": 2.0, "reason": "warm"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        targets = discover_targets(config, frontier_path)
        with open_db(config.db_path) as connection:
            upsert_targets(connection, targets)
            rows = connection.execute(
                "SELECT path, priority, seed_reason FROM targets ORDER BY priority DESC, path ASC"
            ).fetchall()

        self.assertEqual(rows[0]["path"], "src/beta.cc")
        self.assertEqual(rows[0]["seed_reason"], "hotspot")
        self.assertGreater(rows[0]["priority"], rows[1]["priority"])

    def test_watchdog_requeues_stale_runs(self) -> None:
        config = self._prepare()

        with open_db(config.db_path) as connection:
            connection.execute(
                """
                UPDATE targets
                SET status = 'running',
                    locked_by = 'worker-z',
                    last_started_at = '2000-01-01T00:00:00Z',
                    last_heartbeat_at = '2000-01-01T00:00:00Z'
                WHERE path = 'src/alpha.cpp'
                """
            )
            target_id = connection.execute(
                "SELECT id FROM targets WHERE path = 'src/alpha.cpp'"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO experiments(target_id, strategy, worker_id, status, case_dir, started_at, last_heartbeat_at)
                VALUES(?, 'vectorize', 'worker-z', 'running', ?, '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z')
                """,
                (target_id, str(config.cases_root / "stale")),
            )
            connection.commit()

            reclaimed = requeue_stale_runs(connection, config)
            self.assertEqual(reclaimed, 1)

            target_status = connection.execute(
                "SELECT status FROM targets WHERE id = ?",
                (target_id,),
            ).fetchone()["status"]
            experiment_status = connection.execute(
                "SELECT status FROM experiments WHERE target_id = ? ORDER BY id DESC LIMIT 1",
                (target_id,),
            ).fetchone()["status"]

        self.assertEqual(target_status, "queued")
        self.assertEqual(experiment_status, "interrupted")

    def test_run_once_writes_target_memory_and_history(self) -> None:
        config = self._prepare()

        result = run_once(config, worker_id="worker-a")
        self.assertEqual(result["status"], "ok")

        case_dir = Path(result["case_dir"])
        self.assertTrue((case_dir / "target_memory.json").exists())
        self.assertTrue((case_dir / "experiment.summary.json").exists())

        summary_payload = json.loads((case_dir / "experiment.summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary_payload["strategy"], "vectorize")
        self.assertEqual(summary_payload["outcome"], "keep")
        self.assertEqual(summary_payload["files_touched"], ["src/alpha.cpp"])
        self.assertIsNone(summary_payload["terminal_state"])

        target_state_dir = config.runtime_root / "targets"
        latest_files = sorted(target_state_dir.glob("*.latest.json"))
        history_files = sorted(target_state_dir.glob("*.history.jsonl"))
        self.assertEqual(len(latest_files), 1)
        self.assertEqual(len(history_files), 1)

        latest_payload = json.loads(latest_files[0].read_text(encoding="utf-8"))
        self.assertIn("recent_experiments", latest_payload)
        self.assertIn("guidance", latest_payload)
        self.assertEqual(latest_payload["current_attempt"]["strategy"], "vectorize")
        self.assertEqual(latest_payload["current_attempt"]["strategy_pass"], 1)

        history_lines = [
            json.loads(line)
            for line in history_files[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(history_lines), 1)
        self.assertEqual(history_lines[0]["strategy"], "vectorize")
        self.assertEqual(history_lines[0]["strategy_pass"], 1)

    def test_exploit_strategy_can_refine_after_keep(self) -> None:
        raw_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw_config["strategies"] = [
            {"name": "vectorize", "kind": "explore", "max_passes": 1},
            {"name": "layout", "kind": "exploit", "max_passes": 2},
        ]
        self.config_path.write_text(json.dumps(raw_config, indent=2), encoding="utf-8")
        config = self._prepare()

        first = run_once(config, worker_id="worker-a")
        second = run_once(config, worker_id="worker-a")
        third = run_once(config, worker_id="worker-a")

        self.assertEqual((first["strategy"], first["strategy_pass"]), ("vectorize", 1))
        self.assertEqual((second["strategy"], second["strategy_pass"]), ("layout", 1))
        self.assertEqual((third["strategy"], third["strategy_pass"]), ("layout", 2))
        self.assertEqual(third["outcome"], "keep")
        self.assertEqual(third["terminal_state"], "hardware_limit")

        summary = status_summary(config)
        self.assertIn("completed", summary["counts"])
        self.assertGreaterEqual(summary["counts"]["completed"], 1)

    def test_terminal_state_no_more_ideas_completes_target(self) -> None:
        no_ideas_hook = self._write_hook_script(
            self.root / "no_ideas_hook.py",
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path

hook = os.environ["CPP_PERF_HOOK_NAME"]
result_path = Path(os.environ["CPP_PERF_RESULT_PATH"])
result_path.parent.mkdir(parents=True, exist_ok=True)

if hook == "prepare_case":
    payload = {"ok": True}
elif hook == "baseline":
    payload = {"stats": {"median": 100.0, "p99": 120.0, "stable": True}}
elif hook == "optimize":
    payload = {
        "changed": False,
        "rebuild": False,
        "correctness": True,
        "files_touched": [],
        "summary": "no credible optimization left",
        "notes": "tried the obvious loop, layout, and branch directions",
        "terminal_state": "no_more_ideas",
    }
elif hook == "benchmark":
    raise SystemExit("benchmark should not run when optimize makes no edits")
else:
    raise SystemExit(f"unexpected hook: {hook}")

result_path.write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
""",
        )

        raw_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw_config["hooks"] = {
            "prepare_case": ["python3", str(no_ideas_hook)],
            "baseline": ["python3", str(no_ideas_hook)],
            "optimize": ["python3", str(no_ideas_hook)],
            "benchmark": ["python3", str(no_ideas_hook)],
        }
        self.config_path.write_text(json.dumps(raw_config, indent=2), encoding="utf-8")
        config = self._prepare()

        result = run_once(config, worker_id="worker-a")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["outcome"], "discard")
        self.assertEqual(result["terminal_state"], "no_more_ideas")

        summary = status_summary(config)
        self.assertIn("completed", summary["counts"])
        self.assertGreaterEqual(summary["counts"]["completed"], 1)

    def test_crash_restores_target_file(self) -> None:
        restore_hook = self._write_hook_script(
            self.root / "restore_hook.py",
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path

hook = os.environ["CPP_PERF_HOOK_NAME"]
result_path = Path(os.environ["CPP_PERF_RESULT_PATH"])
target_path = Path(os.environ["CPP_PERF_TARGET_PATH"])
result_path.parent.mkdir(parents=True, exist_ok=True)

if hook == "prepare_case":
    payload = {"ok": True}
elif hook == "baseline":
    payload = {"stats": {"median": 100.0, "p99": 120.0, "stable": True}}
elif hook == "optimize":
    target_path.write_text("int alpha() { return 99; }\\n", encoding="utf-8")
    payload = {
        "changed": True,
        "rebuild": True,
        "correctness": True,
        "files_touched": [os.environ["CPP_PERF_RELATIVE_TARGET_PATH"]],
        "summary": "applied risky rewrite",
        "notes": "rewrite should be reverted after crash",
    }
elif hook == "benchmark":
    raise SystemExit("benchmark exploded")
else:
    raise SystemExit(f"unexpected hook: {hook}")

result_path.write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
""",
        )

        raw_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw_config["hooks"] = {
            "prepare_case": ["python3", str(restore_hook)],
            "baseline": ["python3", str(restore_hook)],
            "optimize": ["python3", str(restore_hook)],
            "benchmark": ["python3", str(restore_hook)],
        }
        self.config_path.write_text(json.dumps(raw_config, indent=2), encoding="utf-8")
        config = self._prepare()

        target_path = self.root / "src" / "alpha.cpp"
        original = target_path.read_text(encoding="utf-8")
        result = run_once(config, worker_id="worker-a")

        self.assertEqual(result["status"], "crash")
        self.assertTrue(result["restored_target_file"])
        self.assertEqual(target_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
