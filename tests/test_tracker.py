"""Behavioral tests use fictional data and temporary workspaces only."""
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("tracker", ROOT / "skills/job-search-copilot/scripts/job_tracker.py")
tracker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tracker)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "private"
        self.profile = json.loads((ROOT / "examples/profile.example.json").read_text())
        self.profile["authorization"].update(mode="apply", platforms=["example"], scope_note="虚构测试：用户允许向example平台符合条件岗位发送指定简历")
        self.profile["followup"]["minimum_hours"] = 0
        source = self.base / "profile-source.json"
        source.write_text(json.dumps(self.profile))
        self.call("init", "--profile", str(source))
        self.proof = self.base / "receipt.txt"
        self.proof.write_text("Fictional test receipt, not a real recruiting confirmation.")
        self.resume = self.base / "resume.txt"
        self.resume.write_text("Fictional resume version one")
        self.job = self.add()["job_id"]

    def call(self, *argv):
        return tracker.run(tracker.parser().parse_args(["--workspace", str(self.workspace), *argv]))

    def add(self, url="https://jobs.example.org/job/001", platform="example", external=None):
        argv = ["add", "--platform", platform, "--url", url, "--company", "示例企业", "--title", "内容运营"]
        if external:
            argv += ["--external-id", external]
        return self.call(*argv)

    def save_profile(self):
        (self.workspace / "profile.json").write_text(json.dumps(self.profile))

    def criteria(self, status="pass"):
        for name, rule in self.profile["criteria"].items():
            if rule["required"]:
                self.call("criterion", self.job, "--name", name, "--status", status,
                          "--note", "虚构条件证据", "--evidence", str(self.proof))

    def prepare(self, intent="apply"):
        argv = ["prepare", self.job, "--intent", intent, "--note", "虚构授权范围内的测试动作"]
        if intent == "apply":
            argv += ["--resume", str(self.resume)]
        return self.call(*argv)

    def finish(self, action, outcome="confirmed"):
        return self.call("finish", action["attempt_id"], "--outcome", outcome,
                         "--note", "虚构测试结果", "--evidence", str(self.proof))

    def test_init_never_overwrites(self):
        with self.assertRaises(ValueError):
            self.call("init", "--profile", str(ROOT / "examples/profile.example.json"))

    def test_tracking_parameters_deduplicate_but_job_query_is_preserved(self):
        self.assertTrue(self.add("https://jobs.example.org/job/001?utm_source=sample")["duplicate"])
        self.assertFalse(self.add("https://jobs.example.org/job/001?id=2")["duplicate"])
        self.assertFalse(self.add("https://jobs.example.org/job/001#different-job")["duplicate"])

    def test_explicit_id_upgrades_url_identity_without_duplicate(self):
        upgraded = self.add(external="job-001")
        self.assertEqual(upgraded["job_id"], self.job)
        self.assertTrue(self.add(external="job-001")["duplicate"])
        self.assertTrue(self.add()["duplicate"])

    def test_different_ids_on_shared_url_are_not_merged(self):
        self.add(external="first")
        second = self.add(external="second")
        self.assertFalse(second["duplicate"])
        with self.assertRaises(ValueError):
            self.add()

    def test_cross_platform_similarity_is_only_a_hint(self):
        result = self.add(platform="other")
        self.assertFalse(result["duplicate"])
        self.assertIn(self.job, result["possible_duplicates"])

    def test_unknown_conditions_allow_questions_but_hold_application(self):
        self.assertTrue(self.call("check", self.job, "--intent", "inquire")["allowed"])
        result = self.prepare()
        self.assertFalse(result["prepared"])
        self.assertGreater(len(result["unknown_required"]), 0)

    def test_unknown_application_requires_explicit_opt_in_and_is_not_matched(self):
        self.profile["authorization"]["allow_unverified_application"] = True
        self.save_profile()
        self.assertTrue(self.prepare()["prepared"])
        self.assertEqual(self.call("summary")["fully_matched"], 0)

    def test_hard_failure_blocks_even_with_exploratory_permission(self):
        self.profile["authorization"]["allow_unverified_application"] = True
        self.save_profile()
        self.criteria("fail")
        self.assertFalse(self.prepare()["prepared"])
        self.assertFalse(self.prepare("inquire")["prepared"])

    def test_draft_and_platform_scope_are_enforced(self):
        self.profile["authorization"]["mode"] = "draft"
        self.save_profile()
        self.assertFalse(self.prepare("inquire")["prepared"])
        self.profile["authorization"].update(mode="apply", platforms=["other"])
        self.save_profile()
        self.assertFalse(self.prepare("inquire")["prepared"])

    def test_pass_requires_real_nonempty_evidence(self):
        empty = self.base / "empty.txt"
        empty.touch()
        with self.assertRaises(ValueError):
            self.call("criterion", self.job, "--name", "rest_days", "--status", "pass",
                      "--note", "虚构证据", "--evidence", str(empty))

    def test_changed_evidence_becomes_unknown(self):
        self.criteria()
        self.assertEqual(self.call("summary")["fully_matched"], 1)
        self.proof.write_text("Changed evidence file")
        self.assertFalse(self.prepare()["prepared"])
        self.assertEqual(self.call("summary")["fully_matched"], 0)

    def test_changed_rule_requires_new_assessment(self):
        self.criteria()
        self.profile["criteria"]["rest_days"]["rule"] = "新的虚构规则：每周三天休息"
        self.save_profile()
        result = self.call("check", self.job, "--intent", "apply")
        self.assertIn("rest_days", result["unknown_required"])
        self.assertFalse(result["allowed"])

    def test_missing_failure_evidence_does_not_silently_allow_outreach(self):
        self.criteria("fail")
        self.proof.unlink()
        result = self.call("check", self.job, "--intent", "inquire")
        self.assertTrue(result["failed_required"])
        self.assertFalse(result["allowed"])

    def test_closed_jobs_cannot_receive_new_outreach(self):
        self.call("stage", self.job, "--value", "closed", "--note", "虚构岗位已关闭", "--evidence", str(self.proof))
        self.assertFalse(self.prepare("inquire")["prepared"])

    def test_pending_blocks_duplicate_and_records_resume_hash(self):
        self.criteria()
        action = self.prepare()
        self.assertEqual(action["resume"]["sha256"], tracker.digest(self.resume.read_bytes()))
        self.assertFalse(self.prepare()["prepared"])
        self.assertFalse(self.prepare("inquire")["prepared"])
        self.assertEqual(self.call("summary")["confirmed_actions"]["apply"], 0)

    def test_uncertain_does_not_retry_until_resolved(self):
        self.criteria()
        action = self.prepare()
        self.finish(action, "uncertain")
        self.assertFalse(self.prepare()["prepared"])
        self.assertEqual(self.call("summary")["uncertain"], 1)
        self.finish(action, "failed")
        self.assertTrue(self.prepare()["prepared"])

    def test_confirmation_requires_receipt_and_deduplicates_after_profile_change(self):
        self.criteria()
        action = self.prepare()
        with self.assertRaises(ValueError):
            self.call("finish", action["attempt_id"], "--outcome", "confirmed", "--note", "没有回执")
        self.finish(action)
        self.profile["candidate_alias"] = "更新后的虚构称呼"
        self.save_profile()
        self.assertFalse(self.prepare()["prepared"])
        self.assertEqual(self.call("summary")["confirmed_actions"]["apply"], 1)

    def test_greeting_and_actual_question_are_distinct(self):
        self.finish(self.prepare("greet"))
        self.assertTrue(self.prepare("inquire")["prepared"])
        stats = self.call("summary")
        self.assertEqual(stats["confirmed_actions"]["greet"], 1)
        self.assertEqual(stats["confirmed_actions"]["inquire"], 0)

    def test_followup_requires_touch_and_obeys_budget(self):
        self.assertFalse(self.prepare("followup")["prepared"])
        self.finish(self.prepare("inquire"))
        self.finish(self.prepare("followup"))
        self.assertFalse(self.prepare("followup")["prepared"])

    def test_resume_cannot_bypass_application_gate_in_question(self):
        with self.assertRaises(ValueError):
            self.call("prepare", self.job, "--intent", "inquire", "--note", "虚构",
                      "--resume", str(self.resume))

    def test_stage_and_export_keep_actual_results_distinct(self):
        self.call("stage", self.job, "--value", "replied", "--note", "虚构回复", "--evidence", str(self.proof))
        result = self.call("summary")
        self.assertEqual(result["stages"]["replied"], 1)
        self.assertEqual(result["confirmed_actions"]["apply"], 0)
        out = self.base / "export.json"
        self.call("export", "--format", "json", "--output", str(out))
        self.assertEqual(json.loads(out.read_text())[0]["stage"], "replied")

    def test_csv_does_not_execute_formula_like_company(self):
        self.call("add", "--platform", "example", "--url", "https://jobs.example.org/job/003",
                  "--company", "=1+1", "--title", "示例岗位")
        out = self.base / "export.csv"
        self.call("export", "--format", "csv", "--output", str(out))
        with out.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertIn("'=1+1", [r["company"] for r in rows])

    def test_credentials_and_non_web_sources_are_rejected(self):
        for url in ("javascript:alert(1)", "file:///private/resume.pdf", "https://user:password@jobs.example.org/job/1"):
            with self.assertRaises(ValueError):
                self.add(url)


if __name__ == "__main__":
    unittest.main()
