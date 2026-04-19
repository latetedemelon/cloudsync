"""Tests for cloudsync's config loader and systemd template renderer.

Run: python3 -m pytest tests/
  or just: python3 tests/test_cloudsync.py
"""
from __future__ import annotations

import importlib.util
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
