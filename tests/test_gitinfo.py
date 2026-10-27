from __future__ import annotations

import subprocess

from stateledger.gitinfo import (
    capture_git_state,
    changed_paths,
    commit_distance,
    commit_exists,
    is_git_repository,
    uncommitted_paths,
)


def test_capture_outside_repository(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()

    snapshot = capture_git_state(root)

    assert snapshot.commit is None
    assert snapshot.dirty is None
    assert snapshot.capture_error == "not_a_git_repository"
    assert snapshot.repo_root == str(root.resolve())
    assert is_git_repository(root) is False


def test_capture_missing_repository(tmp_path):
    snapshot = capture_git_state(tmp_path / "missing")

    assert snapshot.commit is None
    assert snapshot.capture_error == "repo_missing"


def test_capture_clean_repository(git_repo):
    snapshot = capture_git_state(git_repo.root)

    assert snapshot.capture_error is None
    assert snapshot.commit == git_repo.head()
    assert snapshot.branch in {"main", "master"}
    assert snapshot.dirty is False
    assert snapshot.repo_root
    assert snapshot.captured_at is not None


def test_capture_dirty_repository(git_repo):
    git_repo.write("src/app.py", "print('one')\n")

    snapshot = capture_git_state(git_repo.root)

    assert snapshot.dirty is True
    assert snapshot.commit == git_repo.head()


def test_capture_unborn_repository(tmp_path):
    root = tmp_path / "unborn"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)

    snapshot = capture_git_state(root)

    assert snapshot.commit is None
    assert snapshot.dirty is None
    assert snapshot.capture_error == "no_commits"


def test_capture_status_timeout_does_not_raise(git_repo, monkeypatch):
    import stateledger.gitinfo as gitinfo

    original = gitinfo._run_git

    def flaky(root, args, **kwargs):
        if args and args[0] == "status":
            raise subprocess.TimeoutExpired("git", 1)
        return original(root, args, **kwargs)

    monkeypatch.setattr(gitinfo, "_run_git", flaky)
    snapshot = capture_git_state(git_repo.root)

    assert snapshot.commit == git_repo.head()
    assert snapshot.dirty is None
    assert snapshot.capture_error == "git_status_failed"


def test_capture_timeout_on_first_command_does_not_raise(git_repo, monkeypatch):
    import stateledger.gitinfo as gitinfo

    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(gitinfo, "_run_git", boom)
    snapshot = capture_git_state(git_repo.root)

    assert snapshot.commit is None
    assert snapshot.dirty is None
    assert snapshot.capture_error == "git_unavailable"


def test_commit_helpers(git_repo):
    git_repo.write("src/app.py", "print('one')\n")
    first = git_repo.commit("feat: first")
    git_repo.write("src/app.py", "print('two')\n")
    git_repo.write("README.md", "docs\n")
    second = git_repo.commit("feat: second")

    assert commit_exists(git_repo.root, first) is True
    assert commit_exists(git_repo.root, second) is True
    assert commit_exists(git_repo.root, "f" * 40) is False
    assert commit_distance(git_repo.root, first) == 1
    assert commit_distance(git_repo.root, second) == 0

    all_changed = changed_paths(git_repo.root, first)
    assert all_changed is not None
    assert set(all_changed) == {"README.md", "src/app.py"}

    scoped = changed_paths(git_repo.root, first, ["src/"])
    assert scoped == ["src/app.py"]


def test_uncommitted_paths_respect_scope(git_repo):
    git_repo.write("src/app.py", "print('one')\n")
    git_repo.commit("feat: first")

    git_repo.write("src/app.py", "print('two')\n")
    git_repo.write("src/untracked.py", "x = 1\n")
    git_repo.write("README.md", "dirty but out of scope\n")

    scoped = uncommitted_paths(git_repo.root, ["src/"])
    assert scoped is not None
    assert set(scoped) == {"src/app.py", "src/untracked.py"}

    unscoped = uncommitted_paths(git_repo.root)
    assert unscoped is not None
    assert "README.md" in unscoped