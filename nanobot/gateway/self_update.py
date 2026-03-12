"""Self-update orchestration for safe agent code changes."""

from __future__ import annotations

import hashlib
import json
import py_compile
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class SelfUpdateContext:
    """Runtime context for one self-update transaction."""

    session_key: str
    update_id: str
    backup_root: Path
    pre_hashes: dict[str, str]
    pre_existing: set[str]
    baseline_revision: str


@dataclass
class SelfUpdateResult:
    """Result from finalizing one self-update transaction."""

    changed_files: list[str]
    applied: bool
    restarted: bool
    rolled_back: bool
    validation_error: str | None = None
    commit_sha: str | None = None


class SelfUpdateManager:
    """Manage snapshot, validation and rollback for agent self-updates."""

    _MONITOR_DIRS = ("nanobot", "tests")
    _MONITOR_SUFFIXES = (".py", ".pyi", ".json", ".toml", ".yaml", ".yml")
    _SOURCE_SUFFIXES = (
        ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt",
        ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs",
        ".php", ".rb", ".swift", ".scala",
    )
    _IGNORE_DIRS = {".git", ".nanobot", "node_modules", ".venv", "venv", "__pycache__"}

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.runtime_dir = self.workspace / ".nanobot" / "runtime" / "self_update"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def begin(self, session_key: str) -> SelfUpdateContext:
        """Create a point-in-time backup and file hash snapshot."""
        update_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_session = "".join(c if c.isalnum() or c in "._-" else "_" for c in session_key)
        backup_root = self.runtime_dir / safe_session / update_id / "backup"
        backup_root.mkdir(parents=True, exist_ok=True)

        pre_hashes: dict[str, str] = {}
        pre_existing: set[str] = set()

        for rel_path, abs_path in self._iter_monitored_files():
            pre_existing.add(rel_path)
            pre_hashes[rel_path] = self._sha256_file(abs_path)

            backup_path = backup_root / rel_path
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abs_path, backup_path)

        manifest = {
            "session_key": session_key,
            "update_id": update_id,
            "created_at": datetime.now().isoformat(),
            "baseline_revision": self._git_current_revision(),
            "pre_existing": sorted(pre_existing),
            "pre_hashes": pre_hashes,
        }
        (backup_root.parent / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return SelfUpdateContext(
            session_key=session_key,
            update_id=update_id,
            backup_root=backup_root,
            pre_hashes=pre_hashes,
            pre_existing=pre_existing,
            baseline_revision=manifest["baseline_revision"],
        )

    def validate_workspace_preconditions(self) -> str | None:
        """Validate workspace is a git-backed source repository."""
        git_path = self.workspace / ".git"
        if not git_path.exists():
            return (
                "Self-update blocked: workspace is not a git repository (.git missing). "
                "Clone/open a git-backed source workspace first."
            )

        has_source = False
        for path in self.workspace.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(self.workspace).parts
            if any(part in self._IGNORE_DIRS for part in rel_parts):
                continue
            if path.suffix in self._SOURCE_SUFFIXES:
                has_source = True
                break

        if not has_source:
            return (
                "Self-update blocked: no source files found in workspace root. "
                "Open a source workspace before requesting code-level self-update."
            )

        dirty = self._git_status_porcelain()
        if dirty:
            return (
                "Self-update blocked: git working tree is not clean. "
                "Commit or stash existing changes before self-update."
            )

        return None

    def finalize(self, ctx: SelfUpdateContext, instruction: str = "") -> SelfUpdateResult:
        """Validate changes and rollback if needed."""
        changed_files = self._collect_changed_files(ctx)

        if not changed_files:
            self._write_result(ctx, changed_files, applied=False, restarted=False, rolled_back=False)
            return SelfUpdateResult(
                changed_files=[],
                applied=False,
                restarted=False,
                rolled_back=False,
            )

        py_files = [p for p in changed_files if p.endswith((".py", ".pyi"))]
        validation_error = self._validate_python_files(py_files)
        if validation_error:
            self._rollback(ctx, changed_files)
            self._write_result(
                ctx,
                changed_files,
                applied=False,
                restarted=True,
                rolled_back=True,
                validation_error=validation_error,
            )
            return SelfUpdateResult(
                changed_files=changed_files,
                applied=False,
                restarted=True,
                rolled_back=True,
                validation_error=validation_error,
            )

        pytest_error = self._run_full_tests()
        if pytest_error:
            self._rollback(ctx, changed_files)
            self._write_result(
                ctx,
                changed_files,
                applied=False,
                restarted=True,
                rolled_back=True,
                validation_error=pytest_error,
            )
            return SelfUpdateResult(
                changed_files=changed_files,
                applied=False,
                restarted=True,
                rolled_back=True,
                validation_error=pytest_error,
            )

        commit_sha, commit_error = self._commit_changes(changed_files, instruction)
        if commit_error:
            self._rollback(ctx, changed_files)
            self._write_result(
                ctx,
                changed_files,
                applied=False,
                restarted=True,
                rolled_back=True,
                validation_error=commit_error,
            )
            return SelfUpdateResult(
                changed_files=changed_files,
                applied=False,
                restarted=True,
                rolled_back=True,
                validation_error=commit_error,
            )

        self._write_result(
            ctx,
            changed_files,
            applied=True,
            restarted=True,
            rolled_back=False,
            commit_sha=commit_sha,
        )
        return SelfUpdateResult(
            changed_files=changed_files,
            applied=True,
            restarted=True,
            rolled_back=False,
            commit_sha=commit_sha,
        )

    def rollback_to_commit(self, target_commit: str) -> tuple[bool, str, str | None]:
        """Rollback workspace to a given commit with compile/test gates.

        Returns:
            (ok, message, head_sha)
        """
        precheck = self.validate_workspace_preconditions()
        if precheck:
            return False, precheck, None

        if not target_commit.strip():
            return False, "Self-rollback blocked: commit SHA is empty.", None

        verify = self._run_cmd(["git", "rev-parse", "--verify", f"{target_commit}^{{commit}}"], timeout=10)
        if verify.returncode != 0:
            return False, f"Self-rollback blocked: commit not found: {target_commit}", None
        resolved_target = verify.stdout.strip() or target_commit

        current = self._git_current_revision()
        if current == "unknown":
            return False, "Self-rollback blocked: unable to read current HEAD.", None
        if current.startswith(resolved_target) or resolved_target.startswith(current):
            summary = self._build_rollback_summary(
                before_head=current,
                target_commit=resolved_target,
                after_head=current,
                compile_seconds=None,
                test_seconds=None,
            )
            return True, f"Self-rollback skipped: already at target commit.\n{summary}", current

        ancestor = self._run_cmd(["git", "merge-base", "--is-ancestor", resolved_target, "HEAD"], timeout=10)
        if ancestor.returncode != 0:
            summary = self._build_rollback_summary(
                before_head=current,
                target_commit=resolved_target,
                after_head=current,
                compile_seconds=None,
                test_seconds=None,
            )
            return False, (
                "Self-rollback blocked: target commit is not an ancestor of HEAD. "
                "Only rollback to reachable history is supported."
                f"\n{summary}"
            ), None

        reset = self._run_cmd(["git", "reset", "--hard", resolved_target], timeout=30)
        if reset.returncode != 0:
            output = (reset.stdout + "\n" + reset.stderr).strip()
            after_head = self._git_current_revision()
            summary = self._build_rollback_summary(
                before_head=current,
                target_commit=resolved_target,
                after_head=after_head,
                compile_seconds=None,
                test_seconds=None,
            )
            return False, f"Self-rollback failed during git reset --hard:\n{output}\n{summary}", None

        py_files = [p for p, _ in self._iter_monitored_files() if p.endswith((".py", ".pyi"))]
        compile_start = time.monotonic()
        compile_error = self._validate_python_files(py_files)
        compile_seconds = time.monotonic() - compile_start
        if compile_error:
            self._run_cmd(["git", "reset", "--hard", current], timeout=30)
            after_head = self._git_current_revision()
            summary = self._build_rollback_summary(
                before_head=current,
                target_commit=resolved_target,
                after_head=after_head,
                compile_seconds=compile_seconds,
                test_seconds=None,
            )
            return False, (
                "Self-rollback failed compile gate and was reverted to previous HEAD.\n"
                f"Reason: {compile_error}"
                f"\n{summary}"
            ), None

        test_start = time.monotonic()
        test_error = self._run_full_tests()
        test_seconds = time.monotonic() - test_start
        if test_error:
            self._run_cmd(["git", "reset", "--hard", current], timeout=30)
            after_head = self._git_current_revision()
            summary = self._build_rollback_summary(
                before_head=current,
                target_commit=resolved_target,
                after_head=after_head,
                compile_seconds=compile_seconds,
                test_seconds=test_seconds,
            )
            return False, (
                "Self-rollback failed test gate and was reverted to previous HEAD.\n"
                f"Reason: {test_error}"
                f"\n{summary}"
            ), None

        head = self._git_current_revision()
        summary = self._build_rollback_summary(
            before_head=current,
            target_commit=resolved_target,
            after_head=head,
            compile_seconds=compile_seconds,
            test_seconds=test_seconds,
        )
        return True, f"Self-rollback succeeded.\n{summary}", head if head != "unknown" else None

    def _build_rollback_summary(
        self,
        *,
        before_head: str,
        target_commit: str,
        after_head: str,
        compile_seconds: float | None,
        test_seconds: float | None,
    ) -> str:
        return (
            f"Rollback before HEAD: {before_head}\n"
            f"Rollback target commit: {target_commit}\n"
            f"Rollback after HEAD: {after_head}\n"
            f"Compile duration: {self._format_duration(compile_seconds)}\n"
            f"Test duration: {self._format_duration(test_seconds)}"
        )

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None:
            return "N/A"
        return f"{seconds:.3f}s"

    def _collect_changed_files(self, ctx: SelfUpdateContext) -> list[str]:
        current_hashes: dict[str, str] = {}
        current_files: set[str] = set()

        for rel_path, abs_path in self._iter_monitored_files():
            current_files.add(rel_path)
            current_hashes[rel_path] = self._sha256_file(abs_path)

        changed: set[str] = set()

        for rel_path in current_files | ctx.pre_existing:
            before = ctx.pre_hashes.get(rel_path)
            after = current_hashes.get(rel_path)
            if before != after:
                changed.add(rel_path)

        return sorted(changed)

    def _validate_python_files(self, rel_paths: list[str]) -> str | None:
        for rel_path in rel_paths:
            file_path = self.workspace / rel_path
            if not file_path.exists():
                continue
            try:
                py_compile.compile(str(file_path), doraise=True)
            except py_compile.PyCompileError as e:
                return str(e)
            except Exception as e:
                return f"Validation failed for {rel_path}: {e}"
        return None

    def _rollback(self, ctx: SelfUpdateContext, changed_files: list[str]) -> None:
        for rel_path in changed_files:
            target = self.workspace / rel_path
            backup = ctx.backup_root / rel_path
            was_existing = rel_path in ctx.pre_existing

            if was_existing:
                backup.parent.mkdir(parents=True, exist_ok=True)
                target.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    shutil.copy2(backup, target)
            else:
                if target.exists():
                    target.unlink()

        if changed_files:
            self._git_unstage_files(changed_files)

    def _write_result(
        self,
        ctx: SelfUpdateContext,
        changed_files: list[str],
        *,
        applied: bool,
        restarted: bool,
        rolled_back: bool,
        validation_error: str | None = None,
        commit_sha: str | None = None,
    ) -> None:
        result = {
            "session_key": ctx.session_key,
            "update_id": ctx.update_id,
            "updated_at": datetime.now().isoformat(),
            "baseline_revision": ctx.baseline_revision,
            "changed_files": changed_files,
            "applied": applied,
            "restarted": restarted,
            "rolled_back": rolled_back,
            "validation_error": validation_error,
            "commit_sha": commit_sha,
        }
        (ctx.backup_root.parent / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _run_full_tests(self) -> str | None:
        completed = self._run_cmd(["pytest", "-q"], timeout=600)
        if completed.returncode == 0:
            return None
        output = (completed.stdout + "\n" + completed.stderr).strip()
        if len(output) > 1500:
            output = output[:1500] + "\n... (truncated)"
        return "Unit test gate failed: pytest -q\n" + output

    def _commit_changes(self, changed_files: list[str], instruction: str) -> tuple[str | None, str | None]:
        add_ret = self._run_cmd(["git", "add", *changed_files], timeout=30)
        if add_ret.returncode != 0:
            return None, "Failed to stage self-update changes."

        subject, body = self._build_commit_message(changed_files, instruction)
        commit_ret = self._run_cmd(["git", "commit", "-m", subject, "-m", body], timeout=60)
        if commit_ret.returncode != 0:
            output = (commit_ret.stdout + "\n" + commit_ret.stderr).strip()
            if len(output) > 1000:
                output = output[:1000] + "\n... (truncated)"
            return None, "Commit gate failed:\n" + output

        sha_ret = self._run_cmd(["git", "rev-parse", "HEAD"], timeout=10)
        if sha_ret.returncode != 0:
            return None, "Unable to resolve commit SHA after self-update commit."
        return sha_ret.stdout.strip(), None

    def _build_commit_message(self, changed_files: list[str], instruction: str) -> tuple[str, str]:
        subject = "chore(self-update): evolve agent capability"
        files = ", ".join(changed_files[:8])
        if len(changed_files) > 8:
            files += f", ... (+{len(changed_files) - 8} files)"
        body = (
            f"Why: {instruction.strip() or 'self capability evolution'}\n"
            f"What: updated files: {files}\n"
            "Validation: compile=pass, tests=pytest -q => pass"
        )
        return subject, body

    def _git_status_porcelain(self) -> str:
        ret = self._run_cmd(["git", "status", "--porcelain"], timeout=10)
        if ret.returncode != 0:
            return "git-status-error"
        return ret.stdout.strip()

    def _git_current_revision(self) -> str:
        ret = self._run_cmd(["git", "rev-parse", "HEAD"], timeout=10)
        return ret.stdout.strip() if ret.returncode == 0 else "unknown"

    def _git_unstage_files(self, changed_files: list[str]) -> None:
        if not changed_files:
            return
        self._run_cmd(["git", "restore", "--staged", *changed_files], timeout=20)

    def _run_cmd(self, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=self.workspace,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def _iter_monitored_files(self) -> list[tuple[str, Path]]:
        files: list[tuple[str, Path]] = []
        for dirname in self._MONITOR_DIRS:
            base = self.workspace / dirname
            if not base.exists() or not base.is_dir():
                continue
            for path in base.rglob("*"):
                if not path.is_file() or path.suffix not in self._MONITOR_SUFFIXES:
                    continue
                rel = str(path.relative_to(self.workspace)).replace("\\", "/")
                files.append((rel, path))
        return files

    @staticmethod
    def _sha256_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
