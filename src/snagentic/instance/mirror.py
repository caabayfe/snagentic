"""Git plumbing for the per-instance ``servicenow-remote/<name>`` mirror branch.

The mirror branch contains only remote state. It is built from a private work tree
(``.snagentic/<name>/mirror-tree``) with a private index file, so the user's working
tree, index, and current branch are never touched while fetching.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from snagentic.errors import ConflictError, SnagenticError
from snagentic.instance.config import InstancePaths

MIRROR_AUTHOR = {
    "GIT_AUTHOR_NAME": "snagentic mirror",
    "GIT_AUTHOR_EMAIL": "snagentic-mirror@localhost",
    "GIT_COMMITTER_NAME": "snagentic mirror",
    "GIT_COMMITTER_EMAIL": "snagentic-mirror@localhost",
}
MIRRORED_SUBDIRECTORIES = ("metadata", "update-sets")


def git(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = {**os.environ, **(env or {})}
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable and argument vector
            ["git", *args],  # noqa: S607 - git is intentionally resolved from PATH
            cwd=root,
            env=merged,
            capture_output=True,
            text=True,
            input=input_text,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SnagenticError("git is required for instance mirroring") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["failed"]
        raise SnagenticError(f"git {args[0]} failed: {detail[0]}")
    return result


def worktree_status(root: Path, prefixes: tuple[str, ...]) -> list[tuple[str, str]]:
    """``(code, path)`` for changed, staged and untracked paths under ``prefixes``.

    Runs a whole-repository ``git status`` and filters here: a pathspec makes git skip the
    untracked cache and fsmonitor, which costs ~40s on a mirror with ~700k files.
    """

    output = git(
        root, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames"
    ).stdout
    wanted = tuple(prefix.rstrip("/") + "/" for prefix in prefixes)
    return [
        (entry[:2], entry[3:])
        for entry in output.split("\0")
        if len(entry) > 3 and entry[3:].startswith(wanted)
    ]


LARGE_REPOSITORY_SETTINGS: tuple[tuple[str, str], ...] = (
    ("core.untrackedCache", "true"),
    *((("core.fsmonitor", "true"),) if sys.platform in {"darwin", "win32"} else ()),
)


def tune_large_repository(root: Path) -> None:
    """Enable git's untracked cache (and the built-in fsmonitor where supported) unless the
    repository already configures them; instance mirrors hold hundreds of thousands of files."""

    for key, value in LARGE_REPOSITORY_SETTINGS:
        if git(root, "config", "--local", "--get", key, check=False).returncode == 1:
            if (
                key == "core.untrackedCache"
                and git(root, "update-index", "--test-untracked-cache", check=False).returncode
                != 0
            ):
                continue
            git(root, "config", "--local", key, value, check=False)


@dataclass(frozen=True)
class MirrorCommit:
    commit: str | None
    parent: str | None

    @property
    def changed(self) -> bool:
        return self.commit is not None and self.commit != self.parent


class MirrorRepository:
    def __init__(self, paths: InstancePaths, branch: str) -> None:
        self.paths = paths
        self.root = paths.root
        self.branch = branch
        self.ref = f"refs/heads/{branch}"
        top = git(self.root, "rev-parse", "--show-toplevel").stdout.strip()
        if Path(top).resolve() != self.root.resolve():
            raise SnagenticError("snagentic must run from the git repository root")
        self.git_dir = Path(
            git(self.root, "rev-parse", "--absolute-git-dir").stdout.strip()
        )

    def tip(self) -> str | None:
        result = git(self.root, "rev-parse", "--verify", "--quiet", self.ref, check=False)
        return result.stdout.strip() or None

    def _env(self) -> dict[str, str]:
        return {
            "GIT_DIR": str(self.git_dir),
            "GIT_WORK_TREE": str(self.paths.mirror_tree),
            "GIT_INDEX_FILE": str(self.paths.mirror_index),
        }

    def restore(self) -> bool:
        """(Re)build the private mirror tree from the branch tip. Returns False if absent."""

        tree = self.paths.mirror_tree
        if tree.exists():
            shutil.rmtree(tree)
        self.paths.mirror_index.unlink(missing_ok=True)
        tree.mkdir(parents=True)
        tip = self.tip()
        if tip is None:
            return False
        env = self._env()
        git(tree, "read-tree", tip, env=env)
        git(tree, "checkout-index", "--all", "--force", env=env)
        self._mark_clean(tip)
        return True

    @property
    def _marker(self) -> Path:
        return self.paths.state / "mirror-tree.tip"

    def _mark_clean(self, tip: str | None) -> None:
        if tip is None:
            return
        self._marker.parent.mkdir(parents=True, exist_ok=True)
        self._marker.write_text(tip + "\n", encoding="utf-8")

    def begin_write(self) -> None:
        """Mark the private tree as being modified. If the write fails before
        ``commit()``, the next ``ensure()`` rebuilds the tree from the branch tip so a
        half-written tree can never be used as a plan base."""

        self._marker.unlink(missing_ok=True)

    def _workspace_matches_index(self) -> bool:
        relative = self.paths.relative_workspace.as_posix()
        result = git(
            self.paths.mirror_tree,
            "diff-files",
            "--quiet",
            "--",
            relative,
            env=self._env(),
            check=False,
        )
        return result.returncode == 0

    def ensure(self) -> bool:
        tip = self.tip()
        marker = self._marker.read_text(encoding="utf-8").strip() if self._marker.is_file() else ""
        if (
            self.paths.mirror_tree.is_dir()
            and self.paths.mirror_index.is_file()
            and tip is not None
            and marker == tip
            and self._workspace_matches_index()
        ):
            return True
        return self.restore()

    def commit(self, message: str) -> MirrorCommit:
        tree_root = self.paths.mirror_tree
        env = self._env()
        parent = self.tip()
        if not self.paths.mirror_index.is_file() and parent is not None:
            git(tree_root, "read-tree", parent, env=env)
        relative = self.paths.relative_workspace.as_posix()
        (tree_root / relative).mkdir(parents=True, exist_ok=True)
        git(tree_root, "add", "--all", "--force", "--", relative, env=env)
        tree = git(tree_root, "write-tree", env=env).stdout.strip()
        if parent is not None:
            parent_tree = git(self.root, "rev-parse", f"{parent}^{{tree}}").stdout.strip()
            if parent_tree == tree:
                self._mark_clean(parent)
                return MirrorCommit(commit=parent, parent=parent)
        args = ["commit-tree", tree]
        if parent is not None:
            args += ["-p", parent]
        commit = git(
            self.root, *args, env=MIRROR_AUTHOR, input_text=message
        ).stdout.strip()
        update = ["update-ref", "-m", "snagentic fetch", self.ref, commit]
        if parent is not None:
            update.append(parent)
        git(self.root, *update)
        self._mark_clean(commit)
        return MirrorCommit(commit=commit, parent=parent)

    def is_integrated(self) -> bool:
        tip = self.tip()
        if tip is None:
            return False
        result = git(self.root, "merge-base", "--is-ancestor", tip, "HEAD", check=False)
        return result.returncode == 0

    def workspace_dirty(self) -> list[str]:
        targets = [
            (self.paths.relative_workspace / name).as_posix()
            for name in MIRRORED_SUBDIRECTORIES
        ]
        return [path for _, path in worktree_status(self.root, tuple(targets))]

    def integrate(self, *, message: str) -> dict[str, object]:
        tip = self.tip()
        if tip is None:
            raise ConflictError(f"nothing to integrate: {self.branch} does not exist yet")
        if git(self.root, "rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False
               ).returncode == 0:
            raise ConflictError("a merge is already in progress; resolve or abort it first")
        dirty = self.workspace_dirty()
        if dirty:
            raise ConflictError(
                "commit or stash local changes before integrating remote changes: "
                + ", ".join(dirty[:10])
            )
        if self.is_integrated():
            return {"status": "up_to_date", "mirror": tip, "conflicts": []}
        args = ["merge", "--no-ff", "--no-edit", "-m", message]
        has_base = git(self.root, "merge-base", "HEAD", tip, check=False).returncode == 0
        if not has_base:
            args.append("--allow-unrelated-histories")
        result = git(self.root, *args, tip, check=False)
        if result.returncode == 0:
            return {"status": "merged", "mirror": tip, "conflicts": []}
        conflicts = git(
            self.root, "diff", "--name-only", "--diff-filter=U"
        ).stdout.split()
        if not conflicts:
            raise SnagenticError(
                "git merge failed: "
                + ((result.stderr or result.stdout).strip().splitlines() or ["unknown"])[-1]
            )
        return {"status": "conflicts", "mirror": tip, "conflicts": conflicts}

    def show(self, relative_path: str) -> str | None:
        tip = self.tip()
        if tip is None:
            return None
        result = git(self.root, "show", f"{tip}:{relative_path}", check=False)
        return result.stdout if result.returncode == 0 else None
