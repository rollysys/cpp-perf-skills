"""Git worktree lifecycle management for isolated optimization experiments.

Provides create / cleanup / merge operations so the target repo is never
polluted by in-progress or failed optimization attempts.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    branch: str
    path: Path
    repo_root: Path


def _git(repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=check,
    )


def current_head(repo_root: Path) -> str:
    result = _git(repo_root, "rev-parse", "HEAD")
    return result.stdout.strip()


def create(repo_root: Path, branch: str, base_ref: str = "HEAD") -> Worktree:
    """Create an isolated worktree for an optimization experiment.

    Creates a new branch from *base_ref* and checks it out in a sibling
    directory named ``.<branch>`` next to *repo_root*.
    """
    worktree_path = repo_root.parent / f".worktree-{branch}"
    if worktree_path.exists():
        shutil.rmtree(worktree_path)

    _git(repo_root, "worktree", "add", "-b", branch, str(worktree_path), base_ref)
    return Worktree(branch=branch, path=worktree_path, repo_root=repo_root)


def cleanup(wt: Worktree) -> None:
    """Remove a worktree and its branch.  Safe to call even if already gone."""
    _git(wt.repo_root, "worktree", "remove", "--force", str(wt.path), check=False)
    if wt.path.exists():
        shutil.rmtree(wt.path, ignore_errors=True)
    _git(wt.repo_root, "worktree", "prune", check=False)
    _git(wt.repo_root, "branch", "-D", wt.branch, check=False)


def has_changes(wt: Worktree) -> bool:
    """Return True if the worktree has uncommitted changes."""
    result = _git(wt.path, "status", "--porcelain")
    return bool(result.stdout.strip())


def commit_all(wt: Worktree, message: str) -> str:
    """Stage and commit all changes in the worktree.  Returns the commit hash."""
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-m", message)
    result = _git(wt.path, "rev-parse", "HEAD")
    return result.stdout.strip()


def diff_stat(wt: Worktree, base_ref: str = "HEAD~1") -> str:
    """Return a compact diff stat for the worktree vs base."""
    result = _git(wt.path, "diff", "--stat", base_ref, check=False)
    return result.stdout.strip()


def merge_back(wt: Worktree, target_branch: str = "HEAD") -> str:
    """Cherry-pick the worktree's tip commit onto the main repo.

    Returns the new commit hash in the main repo.
    """
    tip = _git(wt.path, "rev-parse", "HEAD").stdout.strip()
    _git(wt.repo_root, "cherry-pick", tip)
    new_head = _git(wt.repo_root, "rev-parse", "HEAD").stdout.strip()
    return new_head
