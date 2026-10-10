#!/usr/bin/env python3
import argparse
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("watcher")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


LOGGER = setup_logging()


class GitCommandError(RuntimeError):
    pass


class WatcherError(RuntimeError):
    pass


def run_git_command(args, cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(f"Git command timed out: {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        raise GitCommandError("git executable not found on PATH.") from exc

    if check and result.returncode != 0:
        raise GitCommandError(f"Git command failed ({' '.join(cmd)}): {result.stderr.strip()}")
    return result


def fetch_remote_updates(repo_path: Path) -> None:
    result = run_git_command(["fetch", "--all", "--prune", "--quiet"], cwd=str(repo_path), check=False)
    if result.returncode != 0:
        LOGGER.debug("git fetch skipped/failed: %s", result.stderr.strip())


def get_latest_commit_sha(repo_path: Path, branch: str) -> str:
    fetch_remote_updates(repo_path)
    for ref in (f"origin/{branch}", branch):
        result = run_git_command(["rev-parse", "--verify", ref], cwd=str(repo_path), check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    raise GitCommandError(f"Could not resolve branch '{branch}' in repository '{repo_path}'.")


def validate_git_repo(repo_path: Path) -> None:
    if not repo_path.exists():
        raise WatcherError(f"Repository path does not exist: {repo_path}")
    result = run_git_command(["rev-parse", "--is-inside-work-tree"], cwd=str(repo_path), check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise WatcherError(f"Path is not a valid git repository: {repo_path}")


def load_last_commit_sha(state_file: Path) -> Optional[str]:
    if not state_file.exists():
        return None
    return state_file.read_text(encoding="utf-8").strip() or None


def save_last_commit_sha(state_file: Path, sha: str) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp_file.write_text(sha + "\n", encoding="utf-8")
    tmp_file.replace(state_file)


def create_isolated_workspace(repo_path: Path, commit_sha: str, workspaces_dir: Path) -> Path:
    workspace_dir = workspaces_dir / commit_sha

    if workspace_dir.exists():
        LOGGER.warning("Workspace for commit %s already exists; removing stale copy.", commit_sha)
        shutil.rmtree(workspace_dir)

    workspace_dir.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Creating isolated workspace at %s", workspace_dir)
    
    run_git_command(["clone", "--quiet", "--no-hardlinks", str(repo_path), str(workspace_dir)], check=True)
    run_git_command(["fetch", "--quiet", "origin", commit_sha], cwd=str(workspace_dir), check=False)
    run_git_command(["checkout", "--quiet", "--force", commit_sha], cwd=str(workspace_dir), check=True)

    return workspace_dir


def cleanup_workspace(workspace_dir: Path) -> None:
    try:
        shutil.rmtree(workspace_dir, ignore_errors=True)
    except OSError as exc:
        LOGGER.warning("Failed to clean up workspace %s: %s", workspace_dir, exc)


def trigger_runner(runner_path: Path, commit_sha: str, workspace_dir: Path) -> bool:
    cmd = [sys.executable, str(runner_path), "--commit-sha", commit_sha]
    LOGGER.info("Triggering runner: %s (cwd=%s)", " ".join(cmd), workspace_dir)

    try:
        result = subprocess.run(cmd, cwd=str(workspace_dir))
    except OSError as exc:
        LOGGER.error("Failed to launch runner.py: %s", exc)
        return False

    if result.returncode != 0:
        LOGGER.error("Runner exited with status %d for commit %s", result.returncode, commit_sha)
        return False

    LOGGER.info("Runner completed successfully for commit %s", commit_sha)
    return True


def poll_loop(
    repo_path: Path,
    branch: str,
    interval: int,
    runner_path: Path,
    cicd_dir: Path,
    keep_workspaces: bool,
) -> None:
    state_file = cicd_dir / "last_commit.txt"
    workspaces_dir = cicd_dir / "workspaces"

    last_seen_sha = load_last_commit_sha(state_file)
    if last_seen_sha:
        LOGGER.info("Resuming watcher. Last known commit: %s", last_seen_sha)
    else:
        LOGGER.info("No previous state found. Starting fresh.")

    LOGGER.info("Watching repository '%s' on branch '%s' every %ds.", repo_path, branch, interval)

    while True:
        try:
            current_sha = get_latest_commit_sha(repo_path, branch)

            if current_sha != last_seen_sha:
                LOGGER.info("New commit detected: %s", current_sha)
                workspace_dir: Optional[Path] = None
                
                try:
                    workspace_dir = create_isolated_workspace(repo_path, current_sha, workspaces_dir)
                    success = trigger_runner(runner_path, current_sha, workspace_dir)

                    if success:
                        save_last_commit_sha(state_file, current_sha)
                        last_seen_sha = current_sha
                    else:
                        LOGGER.warning("Build failed for commit %s; will retry on next poll.", current_sha)
                except (GitCommandError, WatcherError) as exc:
                    LOGGER.error("Failed to prepare workspace for commit %s: %s", current_sha, exc)
                finally:
                    if workspace_dir is not None and not keep_workspaces:
                        cleanup_workspace(workspace_dir)
            
        except GitCommandError as exc:
            LOGGER.error("Git error while polling: %s", exc)
        except Exception as exc:
            LOGGER.error("Unexpected error during poll cycle: %s", exc)

        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll a Git repository and trigger CI/CD builds.")
    parser.add_argument("--repo-path", required=True, type=str, help="Path to the local Git repository.")
    parser.add_argument("--branch", default="main", type=str, help="Branch to watch (default: main).")
    parser.add_argument("--interval", default=10, type=int, help="Polling interval in seconds.")
    parser.add_argument("--runner-path", default=None, type=str, help="Path to runner.py.")
    parser.add_argument("--keep-workspaces", action="store_true", help="Keep workspace directories for debugging.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_path = Path(args.repo_path).expanduser().resolve()
    
    if args.interval <= 0:
        LOGGER.error("Interval must be a positive integer, got: %d", args.interval)
        return 1

    try:
        validate_git_repo(repo_path)
    except WatcherError as exc:
        LOGGER.error(str(exc))
        return 1

    runner_path = Path(args.runner_path).expanduser().resolve() if args.runner_path else (Path(__file__).parent / "runner.py").resolve()
    
    if not runner_path.exists():
        LOGGER.error("runner.py not found at: %s", runner_path)
        return 1

    cicd_dir = (repo_path / ".cicd").resolve()
    cicd_dir.mkdir(parents=True, exist_ok=True)

    try:
        poll_loop(
            repo_path,
            args.branch,
            args.interval,
            runner_path,
            cicd_dir,
            keep_workspaces=args.keep_workspaces,
        )
    except KeyboardInterrupt:
        LOGGER.info("Watcher stopped by user.")
        return 0
    except WatcherError as exc:
        LOGGER.error("Fatal watcher error: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())