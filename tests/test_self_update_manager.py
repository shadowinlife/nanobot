from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.gateway.self_update import SelfUpdateManager


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_finalize_no_changes(tmp_path: Path) -> None:
    _write(tmp_path / "nanobot" / "demo.py", "x = 1\n")

    manager = SelfUpdateManager(tmp_path)
    ctx = manager.begin("cli:demo")
    result = manager.finalize(ctx)

    assert result.changed_files == []
    assert result.applied is False
    assert result.rolled_back is False


def test_finalize_rollback_on_invalid_python(tmp_path: Path) -> None:
    target = tmp_path / "nanobot" / "demo.py"
    _write(target, "x = 1\n")

    manager = SelfUpdateManager(tmp_path)
    ctx = manager.begin("cli:demo")

    target.write_text("def broken(:\n", encoding="utf-8")
    result = manager.finalize(ctx)

    assert result.applied is False
    assert result.rolled_back is True
    assert result.validation_error is not None
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_finalize_apply_valid_changes(tmp_path: Path) -> None:
    target = tmp_path / "nanobot" / "demo.py"
    _write(target, "x = 1\n")

    manager = SelfUpdateManager(tmp_path)
    manager._run_full_tests = lambda: None  # type: ignore[attr-defined]
    manager._commit_changes = lambda changed, instruction: ("abc123", None)  # type: ignore[attr-defined]
    ctx = manager.begin("cli:demo")

    target.write_text("x = 2\n", encoding="utf-8")
    result = manager.finalize(ctx)

    assert result.applied is True
    assert result.rolled_back is False
    assert result.restarted is True
    assert result.changed_files == ["nanobot/demo.py"]
    assert result.commit_sha == "abc123"


def test_preconditions_require_git_repo(tmp_path: Path) -> None:
    _write(tmp_path / "nanobot" / "demo.py", "x = 1\n")

    manager = SelfUpdateManager(tmp_path)
    err = manager.validate_workspace_preconditions()

    assert err is not None
    assert ".git" in err


def test_preconditions_require_source_files(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(parents=True)
    _write(tmp_path / "README.md", "hello\n")

    manager = SelfUpdateManager(tmp_path)
    err = manager.validate_workspace_preconditions()

    assert err is not None
    assert "source files" in err.lower()


def test_preconditions_pass_for_git_source_repo(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(parents=True)
    _write(tmp_path / "nanobot" / "demo.py", "x = 1\n")

    manager = SelfUpdateManager(tmp_path)
    manager._run_cmd = lambda args, timeout: subprocess.CompletedProcess(args, 0, "", "")  # type: ignore[attr-defined]
    err = manager.validate_workspace_preconditions()

    assert err is None


def test_preconditions_require_clean_git_tree(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(parents=True)
    _write(tmp_path / "nanobot" / "demo.py", "x = 1\n")

    manager = SelfUpdateManager(tmp_path)

    def fake_run_cmd(args: list[str], timeout: int):
        if args[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, " M nanobot/demo.py\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    manager._run_cmd = fake_run_cmd  # type: ignore[attr-defined]

    err = manager.validate_workspace_preconditions()
    assert err is not None
    assert "not clean" in err


def test_build_commit_message_contains_required_sections(tmp_path: Path) -> None:
    manager = SelfUpdateManager(tmp_path)
    subject, body = manager._build_commit_message(
        ["nanobot/agent/loop.py", "tests/test_agent.py"],
        "add new tool and route behavior",
    )

    assert subject.startswith("chore(self-update):")
    assert "Why:" in body
    assert "What:" in body
    assert "Validation:" in body


def test_rollback_to_commit_success(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(parents=True)
    _write(tmp_path / "nanobot" / "demo.py", "x = 1\n")

    manager = SelfUpdateManager(tmp_path)
    manager._validate_python_files = lambda rel_paths: None  # type: ignore[attr-defined]
    manager._run_full_tests = lambda: None  # type: ignore[attr-defined]

    current_head = "f" * 40
    target = "a" * 40
    post_head = "b" * 40

    def fake_run_cmd(args: list[str], timeout: int):
        if args[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:4] == ["git", "rev-parse", "--verify", f"{target}^{{commit}}"]:
            return subprocess.CompletedProcess(args, 0, target + "\n", "")
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["git", "reset", "--hard"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "rev-parse"]:
            if args[-1] == "HEAD":
                val = current_head if not hasattr(fake_run_cmd, "after_reset") else post_head
                return subprocess.CompletedProcess(args, 0, val + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_reset(args: list[str], timeout: int):
        if args[:3] == ["git", "reset", "--hard"]:
            setattr(fake_run_cmd, "after_reset", True)
        return fake_run_cmd(args, timeout)

    manager._run_cmd = fake_reset  # type: ignore[attr-defined]

    ok, message, head_sha = manager.rollback_to_commit(target)
    assert ok is True
    assert "succeeded" in message.lower()
    assert "Rollback before HEAD:" in message
    assert "Rollback target commit:" in message
    assert "Rollback after HEAD:" in message
    assert "Compile duration:" in message
    assert "Test duration:" in message
    assert head_sha == post_head


def test_rollback_to_commit_rejects_non_ancestor(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(parents=True)
    _write(tmp_path / "nanobot" / "demo.py", "x = 1\n")
    manager = SelfUpdateManager(tmp_path)

    target = "a" * 40

    def fake_run_cmd(args: list[str], timeout: int):
        if args[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:4] == ["git", "rev-parse", "--verify", f"{target}^{{commit}}"]:
            return subprocess.CompletedProcess(args, 0, target + "\n", "")
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[:2] == ["git", "rev-parse"] and args[-1] == "HEAD":
            return subprocess.CompletedProcess(args, 0, "f" * 40 + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    manager._run_cmd = fake_run_cmd  # type: ignore[attr-defined]

    ok, message, head_sha = manager.rollback_to_commit(target)
    assert ok is False
    assert "ancestor" in message.lower()
    assert head_sha is None
