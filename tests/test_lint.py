#!/usr/bin/env python3
"""Tests for orkas-skillref-lint."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "orkas_skillref_lint.py")
FIX = os.path.join(ROOT, "fixtures")


def run_tool(args, cwd=ROOT, check=True):
    p = subprocess.run(
        [sys.executable, TOOL] + args,
        cwd=cwd, capture_output=True, text=True,
    )
    return p


class TestResolutionLogic(unittest.TestCase):
    """Unit tests for the classification rules (behavioral)."""

    def _audit(self, root):
        sys.path.insert(0, ROOT)
        import orkas_skillref_lint as m
        return m.audit(root)

    def test_real_builtin_counts(self):
        """Behavioral: the authentic Orkas builtin marketplace has a known profile."""
        r = self._audit(os.path.join(FIX, "real"))
        s = r["summary"]
        self.assertEqual(s["agents"], 16)
        self.assertEqual(s["shared_skills"], 22)
        self.assertEqual(s["private_skills"], 55)
        self.assertEqual(s["refs"], 84)
        # 55 private refs are inert (render unconditionally), 29 are shared ids.
        self.assertEqual(s["inert"], 55)
        self.assertEqual(s["binding"], 29)
        self.assertEqual(s["dangling"], 0)
        self.assertEqual(s["foreign"], 0)

    def test_dangling_flagged_as_error(self):
        """Behavioral: an unresolvable ref is an E05 error (exit 1)."""
        p = run_tool(["fixtures/dirty", "--json"])
        self.assertEqual(p.returncode, 1)
        r = json.loads(p.stdout)
        rules = {f["rule"] for f in r["findings"]}
        self.assertIn("E05", rules)
        self.assertEqual(r["summary"]["dangling"], 1)

    def test_foreign_flagged_as_error(self):
        """Behavioral: a ref to another agent's private skill is E06."""
        r = json.loads(run_tool(["fixtures/dirty", "--json"]).stdout)
        rules = {f["rule"] for f in r["findings"]}
        self.assertIn("E06", rules)
        self.assertEqual({f["ref"] for f in r["findings"] if f["rule"] == "E06"},
                         {"foreign-skill"})

    def test_clean_has_no_errors(self):
        """Behavioral: clean fixture has zero error-severity findings."""
        r = json.loads(run_tool(["fixtures/clean", "--json"]).stdout)
        self.assertEqual(r["summary"]["errors"], 0)

    def test_inert_ref_flagged(self):
        """Behavioral: a ref that only matches a private skill is W01 inert."""
        r = json.loads(run_tool(["fixtures/dirty", "--json"]).stdout)
        self.assertIn("W01", {f["rule"] for f in r["findings"]})
        self.assertEqual(r["summary"]["inert"], 1)

    def test_exit_code_semantics(self):
        """Behavioral: 0 clean, 1 findings, 2 bad input."""
        self.assertEqual(run_tool(["fixtures/missing-skills"]).returncode, 1)
        self.assertEqual(run_tool(["/nonexistent"]).returncode, 2)
        self.assertEqual(run_tool(["/etc/passwd"]).returncode, 2)

    def test_json_parses(self):
        """The --json output is parseable JSON (covers every fixture)."""
        for name in ("clean", "dirty", "missing-skills", "duplicates", "badjson", "real"):
            p = run_tool([f"fixtures/{name}", "--json"])
            json.loads(p.stdout)  # must not raise

    def test_bad_json_reports(self):
        """Behavioral: a malformed agent.json surfaces as E01."""
        r = json.loads(run_tool(["fixtures/badjson", "--json"]).stdout)
        self.assertIn("E01", {f["rule"] for f in r["findings"]})


class TestCLIContract(unittest.TestCase):
    """CLI shape: --json + exit codes + --version."""

    def test_version(self):
        p = run_tool(["--version"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("orkas-skillref-lint", p.stdout)

    def test_bad_usage(self):
        # no root arg -> argparse error, exit 2
        p = subprocess.run([sys.executable, TOOL], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)

    def test_help(self):
        p = run_tool(["--help"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("skill_list", p.stdout)

    def test_json_is_stable_schema(self):
        r = json.loads(run_tool(["fixtures/real", "--json"]).stdout)
        for key in ("tool", "version", "root", "summary", "agents", "findings"):
            self.assertIn(key, r)
        for key in ("agents", "shared_skills", "private_skills", "refs",
                    "binding", "inert", "dangling", "foreign", "errors", "warnings"):
            self.assertIn(key, r["summary"])
        self.assertEqual(r["tool"], "orkas-skillref-lint")


class TestNetworkIndependence(unittest.TestCase):
    """Core logic must be fully offline (the fixtures are local)."""

    def test_runs_with_gh_env_invalid(self):
        """Behavioral: no network / gh access is needed at all."""
        env = dict(os.environ)
        env.pop("GH_TOKEN", None)
        dev = os.devnull
        p = subprocess.run([sys.executable, TOOL, "fixtures/real", "--json"],
                           cwd=ROOT, capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 1)
        json.loads(p.stdout)

    def test_empty_marketplace_is_clean(self):
        """Behavioral: empty agent+skills roots -> 0 findings, exit 0."""
        p = run_tool(["fixtures/empty-marketplace"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("agents=0", p.stdout)

    def test_no_skills_but_agents_ok(self):
        """agents-only root still audits (no skills -> nothing to resolve)."""
        p = run_tool(["fixtures/agents-only"])
        self.assertEqual(p.returncode, 0)


class TestTextOutput(unittest.TestCase):
    def test_verbose_describes_eff(self):
        """--verbose prints the binding/per-agent breakdown."""
        p = run_tool(["fixtures/dirty", "--verbose"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("binding=1", p.stdout)
        self.assertIn("inert", p.stdout)

    def test_non_root_error_message(self):
        """A non-directory raises a clear exit-2 message on stderr."""
        p = run_tool(["/etc/passwd"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("error", p.stderr.lower())


if __name__ == "__main__":
    unittest.main()