from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
yaml = pytest.importorskip("yaml")


def _module():
    spec = importlib.util.spec_from_file_location("next_version", ROOT / "scripts/next_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nv = _module()


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Add a guided tutorial with per-screen guides", "minor"),
        ("feat(pdf): bleed presets", "minor"),
        ("Implement print planner", "minor"),
        ("Fix deck cardback in build-xml", "patch"),
        ("Move tour popovers with transforms", "patch"),
        ("Addressing review comments", "patch"),
        ("Tidy imports [minor]", "minor"),
        ("refactor!: new storage layout", "major"),
        ("Rework storage\n\nBREAKING CHANGE: old backups no longer load", "major"),
        ("Bump everything [major]", "major"),
        ("Docs typo [skip release]", None),
    ],
)
def test_each_commit_is_classified(message, kind):
    assert nv.classify(message) == kind


def test_the_biggest_change_wins_and_skips_are_ignored():
    assert nv.bump_for(["Fix a", "Add b", "Fix c"]) == "minor"
    assert nv.bump_for(["Fix a", "x [major]"]) == "major"
    assert nv.bump_for(["Fix a [skip release]", "Tweak b"]) == "patch"
    assert nv.bump_for(["Only docs [skip release]"]) is None


def test_versions_stay_small_and_ordered():
    assert nv.next_version((2, 6, 1), "patch") == (2, 6, 2)
    assert nv.next_version((2, 6, 8), "patch") == (2, 6, 9)
    assert nv.next_version((2, 6, 9), "patch") == (2, 7, 0)
    assert nv.next_version((2, 6, 4), "minor") == (2, 7, 0)
    assert nv.next_version((2, 6, 4), "major") == (3, 0, 0)

    version = (0, 0, 0)
    seen = []
    for _ in range(250):
        version = nv.next_version(version, "patch")
        assert version[2] <= nv.MAX_PATCH
        seen.append(version)
    assert seen == sorted(seen) and len(set(seen)) == len(seen)


def test_only_plain_semver_tags_count():
    assert nv.parse_tag("v2.6.1") == (2, 6, 1)
    for tag in ("2.6.1", "v2.6", "v2.6.1-rc1", "dev-abc", "v2.6.1.4"):
        assert nv.parse_tag(tag) is None


def test_stamp_rewrites_only_the_version(tmp_path):
    target = tmp_path / "__init__.py"
    target.write_text('# nota\n__version__ = "0.0.0"\nOTRO = "x"\n', encoding="utf-8")
    nv.stamp("2.7.0", target)
    assert target.read_text(encoding="utf-8") == '# nota\n__version__ = "2.7.0"\nOTRO = "x"\n'
    with pytest.raises(SystemExit):
        nv.stamp("2.7", target)


def test_source_tree_carries_the_development_marker():
    from mpc_forge import __version__

    assert __version__ == "0.0.0", "La versión la sella la release; no la edites a mano"


@pytest.mark.skipif(shutil.which("git") is None, reason="necesita git")
class TestPlanOnARealRepository:
    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        def git(*args):
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        monkeypatch.setattr(nv, "ROOT", tmp_path)

        def commit(message, tag=None):
            git("commit", "-q", "--allow-empty", "-m", message)
            if tag:
                git("tag", tag)
            return subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        return commit

    def test_first_release_and_following_ones(self, repo):
        repo("Initial import")
        plan = nv.plan()
        assert plan["release"] and plan["tag"] == "v0.0.1" and plan["previous"] is None

        repo("Initial import of the app", tag="v2.6.1")
        repo("Fix a crash")
        assert nv.plan()["tag"] == "v2.6.2"
        repo("Add a tutorial")
        plan = nv.plan()
        assert (plan["tag"], plan["bump"], plan["previous"], plan["commits"]) == (
            "v2.7.0",
            "minor",
            "v2.6.1",
            2,
        )
        assert nv.plan(forced="major")["tag"] == "v3.0.0"

    def test_released_or_superseded_commits_are_skipped(self, repo):
        repo("Base", tag="v1.0.0")
        older = repo("Fix one")
        repo("Fix two", tag="v1.0.1")
        assert nv.plan()["release"] is False
        late = nv.plan(ref=older)
        assert late["release"] is False and "v1.0.1" in late["reason"]

    def test_skip_release_and_patch_rollover(self, repo):
        repo("Base", tag="v1.4.9")
        repo("Docs only [skip release]")
        assert nv.plan()["release"] is False
        repo("Fix something")
        assert nv.plan()["tag"] == "v1.5.0"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))


class TestReleaseWorkflow:
    def test_runs_after_a_green_ci_on_main_one_at_a_time(self, wf):
        trigger = wf[True]
        assert trigger["workflow_run"] == {
            "workflows": ["CI"],
            "types": ["completed"],
            "branches": ["main"],
        }
        assert "workflow_dispatch" in trigger and "push" not in trigger
        assert wf["concurrency"] == {"group": "release", "cancel-in-progress": False}
        ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        assert ci["name"] == "CI"
        plan_if = wf["jobs"]["plan"]["if"]
        assert "conclusion == 'success'" in plan_if and "event == 'push'" in plan_if

    def test_tags_only_after_every_build_succeeds(self, wf):
        jobs = wf["jobs"]
        assert jobs["build"]["needs"] == ["plan", "test"]
        assert jobs["publish"]["needs"] == ["plan", "build"]
        assert jobs["build"]["strategy"]["fail-fast"] is False
        text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        assert text.count("action-gh-release") == 1
        publish = yaml.safe_dump(jobs["publish"])
        assert "action-gh-release" in publish and "make_latest" in publish
        build = yaml.safe_dump(jobs["build"])
        assert "next_version.py stamp" in build and "action-gh-release" not in build
        for name in ("test", "build"):
            checkout = next(s for s in jobs[name]["steps"] if "checkout" in str(s.get("uses")))
            assert checkout["with"]["ref"] == "${{ needs.plan.outputs.sha }}", name
