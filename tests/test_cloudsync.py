"""Tests for cloudsync's config loader and systemd template renderer.

Run: python3 -m pytest tests/
  or just: python3 tests/test_cloudsync.py
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_cloudsync():
    """Load bin/cloudsync as a module (it has no .py extension)."""
    path = REPO_ROOT / "bin" / "cloudsync"
    spec = importlib.util.spec_from_loader(
        "cloudsync",
        importlib.machinery.SourceFileLoader("cloudsync", str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cloudsync"] = mod
    spec.loader.exec_module(mod)
    return mod


cloudsync = _load_cloudsync()


def _write(tmp: Path, content: str) -> Path:
    p = tmp / "mappings.yaml"
    p.write_text(textwrap.dedent(content))
    return p


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_minimal_sync_mapping(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/docs
                destination: primary:Backup/docs
                mode: sync
        """)
        _, mappings = cloudsync.load_config(p)
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m.id, "docs")
        self.assertEqual(m.mode, "sync")
        self.assertEqual(m.trigger, "scheduled")
        self.assertEqual(m.retention.prune, "always")

    def test_defaults_merge_before_per_mapping_flags(self):
        p = _write(self.tmp, """\
            defaults:
              rclone_flags: ["--fast-list"]
              exclude: [".DS_Store"]
            mappings:
              - id: docs
                source: /srv/docs
                destination: primary:docs
                mode: sync
                rclone_flags: ["--transfers", "2"]
                exclude: ["*.tmp"]
        """)
        _, mappings = cloudsync.load_config(p)
        m = mappings[0]
        self.assertEqual(m.rclone_flags, ["--fast-list", "--transfers", "2"])
        self.assertEqual(m.exclude, [".DS_Store", "*.tmp"])

    def test_rejects_invalid_id(self):
        for bad_id in ("Bad", "with space", "../evil", "slash/id", ""):
            p = _write(self.tmp, f"""\
                mappings:
                  - id: "{bad_id}"
                    source: /srv/x
                    destination: r:x
                    mode: sync
            """)
            with self.assertRaises(cloudsync.ConfigError, msg=f"id={bad_id!r}"):
                cloudsync.load_config(p)

    def test_rejects_control_chars_in_source(self):
        p = _write(self.tmp, """\
            mappings:
              - id: ok
                source: "/srv/x\\nInjected=yes"
                destination: r:x
                mode: sync
        """)
        with self.assertRaises(cloudsync.ConfigError):
            cloudsync.load_config(p)

    def test_rejects_unknown_retention_key(self):
        p = _write(self.tmp, """\
            mappings:
              - id: photos
                source: /srv/p
                destination: r:p
                mode: backup
                password_file: /tmp/pw
                retention:
                  keep_dayly: 7
        """)
        with self.assertRaises(cloudsync.ConfigError):
            cloudsync.load_config(p)

    def test_rejects_bad_prune_value(self):
        p = _write(self.tmp, """\
            mappings:
              - id: photos
                source: /srv/p
                destination: r:p
                mode: backup
                password_file: /tmp/pw
                retention:
                  prune: sometimes
        """)
        with self.assertRaises(cloudsync.ConfigError):
            cloudsync.load_config(p)

    def test_missing_required_field(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                destination: r:docs
                mode: sync
        """)
        with self.assertRaises(cloudsync.ConfigError):
            cloudsync.load_config(p)


class TemplateRenderTests(unittest.TestCase):
    def test_service_template_contains_hardening(self):
        rendered = cloudsync.SERVICE_TEMPLATE.format(
            mode="sync", id="docs",
            source="/srv/docs", destination="primary:docs",
            timeout="6h",
        )
        self.assertIn("NoNewPrivileges=true", rendered)
        self.assertIn("ProtectSystem=strict", rendered)
        self.assertIn("ConditionPathExists=/srv/docs", rendered)
        self.assertIn("OnFailure=cloudsync-failure@%n.service", rendered)
        self.assertIn("TimeoutStartSec=6h", rendered)
        self.assertIn("RuntimeDirectory=cloudsync", rendered)

    def test_realtime_service_has_conditionpath(self):
        rendered = cloudsync.REALTIME_SERVICE_TEMPLATE.format(
            id="code", source="/home/rob/code",
            debounce=45, timeout="6h",
        )
        self.assertIn("ConditionPathExists=/home/rob/code", rendered)
        self.assertIn("Environment=CLOUDSYNC_DEBOUNCE=45", rendered)

    def test_timer_template(self):
        rendered = cloudsync.TIMER_TEMPLATE.format(
            id="docs", oncalendar="hourly",
        )
        self.assertIn("OnCalendar=hourly", rendered)
        self.assertIn("Unit=cloudsync-docs.service", rendered)


class RetryConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_retry(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/x
                destination: r:x
                mode: sync
        """)
        _, mappings = cloudsync.load_config(p)
        m = mappings[0]
        self.assertEqual(m.retry.max_attempts, 3)
        self.assertEqual(m.retry.backoff_seconds, [60, 300, 1800])

    def test_per_mapping_retry(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/x
                destination: r:x
                mode: sync
                retry:
                  max_attempts: 5
                  backoff_seconds: [10, 20, 40, 80, 160]
        """)
        _, mappings = cloudsync.load_config(p)
        m = mappings[0]
        self.assertEqual(m.retry.max_attempts, 5)
        self.assertEqual(m.retry.backoff_seconds, [10, 20, 40, 80, 160])

    def test_rejects_bad_retry_keys(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/x
                destination: r:x
                mode: sync
                retry:
                  attempts: 5
        """)
        with self.assertRaises(cloudsync.ConfigError):
            cloudsync.load_config(p)

    def test_rejects_zero_attempts(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/x
                destination: r:x
                mode: sync
                retry:
                  max_attempts: 0
        """)
        with self.assertRaises(cloudsync.ConfigError):
            cloudsync.load_config(p)


class RetryLoopTests(unittest.TestCase):
    def _mapping(self, max_attempts=3, backoff=(0, 0, 0)):
        return cloudsync.Mapping(
            id="t", source=Path("/x"), destination="r:x", mode="sync",
            retry=cloudsync.Retry(
                max_attempts=max_attempts, backoff_seconds=list(backoff)
            ),
        )

    def test_retry_succeeds_after_transient_failure(self):
        attempts = {"n": 0}
        def fn():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise subprocess.CalledProcessError(1, ["cmd"])
        cloudsync._retry(self._mapping(), fn)
        self.assertEqual(attempts["n"], 3)

    def test_retry_gives_up_and_raises(self):
        attempts = {"n": 0}
        def fn():
            attempts["n"] += 1
            raise subprocess.CalledProcessError(2, ["cmd"])
        with self.assertRaises(subprocess.CalledProcessError):
            cloudsync._retry(self._mapping(), fn)
        self.assertEqual(attempts["n"], 3)

    def test_retry_single_attempt_no_backoff(self):
        attempts = {"n": 0}
        def fn():
            attempts["n"] += 1
            raise subprocess.CalledProcessError(2, ["cmd"])
        with self.assertRaises(subprocess.CalledProcessError):
            cloudsync._retry(self._mapping(max_attempts=1), fn)
        self.assertEqual(attempts["n"], 1)

    def test_retry_catches_non_subprocess_exceptions(self):
        # Non-CalledProcessError exceptions (e.g. a missing password file
        # raising FileNotFoundError) must still trip retry bookkeeping so
        # that `status`, `metrics`, and `on_failure` hooks observe them.
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig = cloudsync.STATE_DIR
        cloudsync.STATE_DIR = Path(tmp.name)
        self.addCleanup(lambda: setattr(cloudsync, "STATE_DIR", orig))

        attempts = {"n": 0}
        def fn():
            attempts["n"] += 1
            raise FileNotFoundError("password file missing")
        with self.assertRaises(FileNotFoundError):
            cloudsync._retry(self._mapping(), fn)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(cloudsync._read_state("t", "attempts"), "3")
        err = cloudsync._read_state("t", "last-error")
        self.assertIsNotNone(err)
        self.assertIn("FileNotFoundError", err)


class StateTrackingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state_dir = cloudsync.STATE_DIR
        cloudsync.STATE_DIR = Path(self._tmp.name)

    def tearDown(self):
        cloudsync.STATE_DIR = self._orig_state_dir
        self._tmp.cleanup()

    def test_mark_success_clears_error(self):
        cloudsync._mark_run_failure("m1", "boom", attempt=2)
        self.assertIsNotNone(cloudsync._read_state("m1", "last-error"))
        self.assertEqual(cloudsync._read_state("m1", "attempts"), "2")
        cloudsync._mark_run_success("m1")
        self.assertIsNone(cloudsync._read_state("m1", "last-error"))
        self.assertIsNone(cloudsync._read_state("m1", "attempts"))
        self.assertIsNotNone(cloudsync._read_state("m1", "last-success"))

    def test_mark_run_start(self):
        cloudsync._mark_run_start("m1")
        self.assertIsNotNone(cloudsync._read_state("m1", "last-run"))


class HooksConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_hooks_empty(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/x
                destination: r:x
                mode: sync
        """)
        _, mappings = cloudsync.load_config(p)
        h = mappings[0].hooks
        self.assertEqual(h.pre_run, [])
        self.assertEqual(h.on_success, [])
        self.assertEqual(h.on_failure, [])

    def test_parses_hook_commands(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/x
                destination: r:x
                mode: sync
                hooks:
                  pre_run: ["echo start"]
                  on_success: ["echo ok", "echo done"]
                  on_failure: ["echo bad"]
        """)
        _, mappings = cloudsync.load_config(p)
        h = mappings[0].hooks
        self.assertEqual(h.pre_run, ["echo start"])
        self.assertEqual(h.on_success, ["echo ok", "echo done"])
        self.assertEqual(h.on_failure, ["echo bad"])

    def test_rejects_unknown_hook_key(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/x
                destination: r:x
                mode: sync
                hooks:
                  before_run: ["echo x"]
        """)
        with self.assertRaises(cloudsync.ConfigError):
            cloudsync.load_config(p)

    def test_rejects_non_list_hook(self):
        p = _write(self.tmp, """\
            mappings:
              - id: docs
                source: /srv/x
                destination: r:x
                mode: sync
                hooks:
                  pre_run: "echo nope"
        """)
        with self.assertRaises(cloudsync.ConfigError):
            cloudsync.load_config(p)


class RunHooksTests(unittest.TestCase):
    def _mapping(self, hooks):
        return cloudsync.Mapping(
            id="t", source=Path("/x"), destination="r:x", mode="sync",
            hooks=hooks,
        )

    def test_pre_run_failure_is_fatal(self):
        m = self._mapping(cloudsync.Hooks(pre_run=["false"]))
        with self.assertRaises(subprocess.CalledProcessError):
            cloudsync._run_hooks(m, "pre_run", m.hooks.pre_run, fatal=True)

    def test_on_success_failure_is_non_fatal(self):
        m = self._mapping(cloudsync.Hooks(on_success=["false"]))
        # Should not raise.
        cloudsync._run_hooks(m, "on_success", m.hooks.on_success, fatal=False)

    def test_hook_env_exports_mapping_info(self):
        m = self._mapping(cloudsync.Hooks())
        env = cloudsync._hook_env(m, error="boom")
        self.assertEqual(env["CLOUDSYNC_MAPPING_ID"], "t")
        self.assertEqual(env["CLOUDSYNC_MODE"], "sync")
        self.assertEqual(env["CLOUDSYNC_SOURCE"], "/x")
        self.assertEqual(env["CLOUDSYNC_DESTINATION"], "r:x")
        self.assertEqual(env["CLOUDSYNC_ERROR"], "boom")

    def test_hook_env_no_error_key_by_default(self):
        m = self._mapping(cloudsync.Hooks())
        env = cloudsync._hook_env(m)
        self.assertNotIn("CLOUDSYNC_ERROR", env)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state_dir = cloudsync.STATE_DIR
        cloudsync.STATE_DIR = Path(self._tmp.name)

    def tearDown(self):
        cloudsync.STATE_DIR = self._orig_state_dir
        self._tmp.cleanup()

    def _mapping(self, mid="docs", mode="sync", trigger="scheduled"):
        return cloudsync.Mapping(
            id=mid, source=Path("/x"), destination="r:x",
            mode=mode, trigger=trigger,
        )

    def test_metrics_has_all_series(self):
        m = self._mapping()
        out = cloudsync._render_metrics([m])
        self.assertIn("cloudsync_last_run_timestamp_seconds", out)
        self.assertIn("cloudsync_last_success_timestamp_seconds", out)
        self.assertIn("cloudsync_last_failure_timestamp_seconds", out)
        self.assertIn("cloudsync_consecutive_failures", out)
        self.assertIn("cloudsync_mapping_info", out)

    def test_metrics_zero_when_no_state(self):
        m = self._mapping()
        out = cloudsync._render_metrics([m])
        self.assertIn(
            'cloudsync_last_run_timestamp_seconds{id="docs",mode="sync"} 0',
            out,
        )
        self.assertIn(
            'cloudsync_consecutive_failures{id="docs",mode="sync"} 0',
            out,
        )
        self.assertIn(
            'cloudsync_mapping_info{id="docs",mode="sync",trigger="scheduled"} 1',
            out,
        )

    def test_metrics_reflects_failure_state(self):
        m = self._mapping()
        cloudsync._mark_run_failure("docs", "boom", attempt=2)
        out = cloudsync._render_metrics([m])
        self.assertIn(
            'cloudsync_consecutive_failures{id="docs",mode="sync"} 2',
            out,
        )
        import re as _re
        m_ts = _re.search(
            r'cloudsync_last_failure_timestamp_seconds\{id="docs",mode="sync"\} (\d+)',
            out,
        )
        self.assertIsNotNone(m_ts)
        self.assertGreater(int(m_ts.group(1)), 0)


class ExpectedUnitsTests(unittest.TestCase):
    def _make(self, mid, trigger):
        return cloudsync.Mapping(
            id=mid, source=Path("/x"), destination="r:x",
            mode="sync", trigger=trigger,
        )

    def test_expected_units_for_triggers(self):
        m_sched = self._make("a", "scheduled")
        m_real = self._make("b", "realtime")
        m_both = self._make("c", "both")

        sched = cloudsync._expected_units([m_sched])
        self.assertIn(cloudsync.SYSTEMD_DIR / "cloudsync-a.service", sched)
        self.assertIn(cloudsync.SYSTEMD_DIR / "cloudsync-a.timer", sched)
        self.assertNotIn(cloudsync.SYSTEMD_DIR / "cloudsync-realtime-a.path", sched)

        real = cloudsync._expected_units([m_real])
        self.assertIn(cloudsync.SYSTEMD_DIR / "cloudsync-b.service", real)
        self.assertIn(cloudsync.SYSTEMD_DIR / "cloudsync-realtime-b.service", real)
        self.assertIn(cloudsync.SYSTEMD_DIR / "cloudsync-realtime-b.path", real)
        self.assertNotIn(cloudsync.SYSTEMD_DIR / "cloudsync-b.timer", real)

        both = cloudsync._expected_units([m_both])
        self.assertEqual(len(both), 4)


if __name__ == "__main__":
    unittest.main()
