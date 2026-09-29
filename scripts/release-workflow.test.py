from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yaml"
if not RELEASE.is_file():
    RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def job_block(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\s*\n(.*?)(?=^  [a-zA-Z0-9_-]+:\s*$|\Z)",
        workflow,
    )
    if not match:
        raise AssertionError(f"Missing release job: {name}")
    return match.group(1)


class CandidateFirstWorkflowTest(unittest.TestCase):
    def test_release_pipeline_orders_validation_publish_and_promotion(self) -> None:
        workflow = RELEASE.read_text(encoding="utf-8")
        names = ["candidate", "validate", "publish", "promote"]
        positions = [workflow.index(f"\n  {name}:") for name in names]
        self.assertEqual(positions, sorted(positions))
        self.assertRegex(job_block(workflow, "validate"), r"candidate_sha:")
        self.assertRegex(job_block(workflow, "publish"), r"needs:.*validate")
        self.assertRegex(job_block(workflow, "promote"), r"needs:.*publish")
        self.assertRegex(job_block(workflow, "candidate"), r"candidate_ref_present=")
        self.assertRegex(job_block(workflow, "publish"), r"assert_candidate_ref_state")
        self.assertRegex(job_block(workflow, "promote"), r"assert_candidate_ref_state")
        self.assertRegex(job_block(workflow, "promote"), r"promote_refs?")
        self.assertNotRegex(job_block(workflow, "publish"), r"promote_refs?")
        self.assertRegex(workflow, r"release-candidate/")
        self.assertRegex(workflow, r"assert_ref_state")
        publish = job_block(workflow, "publish")
        self.assertIn("validate-windows", publish)
        self.assertLess(
            publish.index("scripts/smoke-windows-backend.mjs"),
            publish.index("Upload and verify Windows release assets"),
        )

    def test_reusable_ci_checks_explicit_candidate_sha(self) -> None:
        coverage = (ROOT / ".github" / "workflows" / "coverage.yml").read_text(
            encoding="utf-8"
        )
        windows = (
            ROOT / ".github" / "workflows" / "windows-launcher.yml"
        ).read_text(encoding="utf-8")
        for workflow in (coverage, windows):
            self.assertRegex(workflow, r"workflow_call:")
            self.assertRegex(workflow, r"candidate_sha:")
            self.assertRegex(workflow, r"ref:.*inputs\.candidate_sha")
            self.assertNotRegex(workflow, r"format --write|spotlessApply")


if __name__ == "__main__":
    unittest.main()
