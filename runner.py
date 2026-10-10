
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from config_parser import ConfigParser, PipelineConfig, PipelineConfigError, Stage


class DockerUnavailableError(PipelineConfigError):
    pass


@dataclass
class StageResult:
    name: str
    status: str
    exit_code: Optional[int]
    started_at: str
    finished_at: str
    duration_seconds: float
    log_path: str
    skipped_reason: Optional[str] = None


@dataclass
class BuildRecord:
    build_id: str
    commit_sha: str
    started_at: str
    finished_at: str
    duration_seconds: float
    status: str
    stages: List[Dict]


class PipelineRunner:
    def __init__(self, config_path: str, build_id: Optional[str] = None, commit_sha: Optional[str] = None, cicd_dir: str = ".cicd"):
        self.config_path = config_path
        self.cicd_dir = Path(cicd_dir)
        self.history_path = self.cicd_dir / "history.json"
        self.build_id = build_id or self._generate_build_id()
        self.commit_sha = commit_sha  # Storing the passed SHA
        self.log_dir = self.cicd_dir / "logs" / self.build_id
        self.parser = ConfigParser(config_path)
        self.config: Optional[PipelineConfig] = None

        self._docker_checked = False
        self._docker_lock = threading.Lock()
        self._print_lock = threading.Lock()

    @staticmethod
    def _generate_build_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _get_commit_sha() -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "unknown"

    @staticmethod
    def _uses_docker(stage: Stage) -> bool:
        return any(cmd.strip().startswith("docker") for cmd in stage.commands)

    def _check_docker_available(self) -> None:
        with self._docker_lock:
            if self._docker_checked:
                return
            try:
                subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
            except FileNotFoundError:
                raise DockerUnavailableError("Docker CLI not found on PATH.")
            except subprocess.TimeoutExpired:
                raise DockerUnavailableError("Docker daemon did not respond in time.")
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.strip() if e.stderr else "unknown error"
                raise DockerUnavailableError(f"Docker daemon is not available: {stderr}")
            self._docker_checked = True

    def load(self) -> PipelineConfig:
        self.config = self.parser.load()
        self.parser.validate()
        return self.config

    def run(self) -> BuildRecord:
        if self.config is None:
            self.load()

        self.log_dir.mkdir(parents=True, exist_ok=True)
        groups = self.parser.get_parallel_groups()

        # Use the passed SHA from watcher, or detect it if run manually
        commit_sha = self.commit_sha or self._get_commit_sha()
        started_at = self._now_iso()
        start_clock = time.monotonic()

        stage_results: Dict[str, StageResult] = {}
        hard_failures: set = set()
        skipped: set = set()

        for group in groups:
            runnable: List[Stage] = []

            for stage_name in group:
                stage = self.parser.get_stage(stage_name)
                blockers = [d for d in stage.depends_on if d in hard_failures or d in skipped]

                if blockers:
                    skipped.add(stage_name)
                    now = self._now_iso()
                    reason = f"upstream failure: {', '.join(blockers)}"
                    stage_results[stage_name] = StageResult(
                        name=stage_name,
                        status="SKIPPED",
                        exit_code=None,
                        started_at=now,
                        finished_at=now,
                        duration_seconds=0.0,
                        log_path="",
                        skipped_reason=reason,
                    )
                    self._log(f"[{stage_name}] skipped ({reason})")
                else:
                    runnable.append(stage)

            if not runnable:
                continue

            with ThreadPoolExecutor(max_workers=len(runnable)) as executor:
                futures = {executor.submit(self._run_stage, stage): stage for stage in runnable}
                for future in as_completed(futures):
                    stage = futures[future]
                    result = future.result()
                    stage_results[stage.name] = result
                    if result.status == "FAILED" and not stage.allow_failure:
                        hard_failures.add(stage.name)

        finished_at = self._now_iso()
        total_duration = time.monotonic() - start_clock
        overall_status = "FAILED" if hard_failures else "SUCCESS"

        ordered_results = [stage_results[name] for name in self.config.stages if name in stage_results]

        record = BuildRecord(
            build_id=self.build_id,
            commit_sha=commit_sha,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=round(total_duration, 3),
            status=overall_status,
            stages=[asdict(r) for r in ordered_results],
        )

        self._persist_history(record)
        return record

    def _log(self, message: str) -> None:
        with self._print_lock:
            print(message)

    def _run_stage(self, stage: Stage) -> StageResult:
        log_path = self.log_dir / f"{stage.name}.log"

        if self._uses_docker(stage):
            try:
                self._check_docker_available()
            except DockerUnavailableError as e:
                return self._abort_stage(stage, log_path, str(e))

        env = os.environ.copy()
        env.update(self.parser.resolved_env(stage.name))

        started_at = self._now_iso()
        start_clock = time.monotonic()
        exit_code = 0

        with open(log_path, "w") as log_file:
            self._write(log_file, f"=== stage: {stage.name} | started: {started_at} ===")

            for command in stage.commands:
                self._write(log_file, f"$ {command}")
                self._log(f"[{stage.name}] $ {command}")

                process = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=Path.cwd(),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    line = line.rstrip("\n")
                    self._write(log_file, line)
                    self._log(f"[{stage.name}] {line}")
                process.wait()
                exit_code = process.returncode

                if exit_code != 0:
                    break

            finished_at = self._now_iso()
            duration = time.monotonic() - start_clock
            status = "SUCCESS" if exit_code == 0 else "FAILED"
            footer = (
                f"=== stage: {stage.name} | status: {status} | "
                f"exit_code: {exit_code} | duration: {duration:.2f}s ==="
            )
            self._write(log_file, footer)

        self._log(f"[{stage.name}] {status} in {duration:.2f}s")

        return StageResult(
            name=stage.name,
            status=status,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=round(duration, 3),
            log_path=str(log_path),
        )

    def _abort_stage(self, stage: Stage, log_path: Path, reason: str) -> StageResult:
        now = self._now_iso()
        with open(log_path, "w") as log_file:
            self._write(log_file, f"=== stage: {stage.name} | aborted: {reason} ===")
        self._log(f"[{stage.name}] aborted: {reason}")
        return StageResult(
            name=stage.name,
            status="FAILED",
            exit_code=None,
            started_at=now,
            finished_at=now,
            duration_seconds=0.0,
            log_path=str(log_path),
            skipped_reason=reason,
        )

    @staticmethod
    def _write(log_file, line: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        log_file.write(f"[{ts}] {line}\n")
        log_file.flush()

    def _persist_history(self, record: BuildRecord) -> None:
        self.cicd_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.history_path.with_suffix(".lock")

        with self._history_lock(lock_path):
            history = self._load_history()
            history = [r for r in history if r.get("build_id") != record.build_id]
            history.append(asdict(record))
            history.sort(key=lambda r: r["started_at"])

            fd, tmp_path = tempfile.mkstemp(
                dir=self.cicd_dir, prefix="history.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as tmp_file:
                    json.dump(history, tmp_file, indent=2)
                    tmp_file.write("\n")
                os.replace(tmp_path, self.history_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

    def _load_history(self) -> List[Dict]:
        if not self.history_path.exists():
            return []
        try:
            content = self.history_path.read_text().strip()
            return json.loads(content) if content else []
        except json.JSONDecodeError:
            return []

    @staticmethod
    @contextmanager
    def _history_lock(lock_path: Path) -> Iterator[None]:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "w")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock_file, fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()


def main() -> int:
    arg_parser = argparse.ArgumentParser(description="Run a CI/CD pipeline defined in a YAML config.")
    arg_parser.add_argument("config", nargs="?", default="pipeline.yml", help="Path to the pipeline YAML file.")
    arg_parser.add_argument("--build-id", default=None, help="Explicit build id, e.g. to retry a specific build.")
    arg_parser.add_argument("--commit-sha", default=None, help="The commit SHA triggering this build.")
    args = arg_parser.parse_args()

    try:
        runner = PipelineRunner(args.config, build_id=args.build_id, commit_sha=args.commit_sha)
        runner.load()
    except PipelineConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    record = runner.run()
    print(f"\nbuild {record.build_id}: {record.status} ({record.duration_seconds:.2f}s)")
    return 0 if record.status == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())