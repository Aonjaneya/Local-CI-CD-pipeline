from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml


class PipelineConfigError(Exception): pass
class SchemaValidationError(PipelineConfigError): pass
class UnknownDependencyError(PipelineConfigError): pass
class CyclicDependencyError(PipelineConfigError): pass
class DuplicateStageError(PipelineConfigError): pass


@dataclass
class Stage:
    name: str
    commands: List[str]
    depends_on: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    allow_failure: bool = False


@dataclass
class PipelineConfig:
    version: str
    name: str
    env: Dict[str, str]
    stages: Dict[str, Stage]


class ConfigParser:
    REQUIRED_TOP_LEVEL = ("version", "name", "stages")
    REQUIRED_STAGE_FIELDS = ("name", "commands")

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.config: Optional[PipelineConfig] = None

    def load(self) -> PipelineConfig:
        if not self.path.exists():
            raise PipelineConfigError(f"Config file not found: {self.path}")

        with open(self.path, "r") as f:
            try:
                raw = yaml.safe_load(f)
            except yaml.YAMLError as e:
                raise PipelineConfigError(f"Invalid YAML syntax: {e}") from e

        if not isinstance(raw, dict):
            raise SchemaValidationError("Top level must be a mapping.")

        for key in self.REQUIRED_TOP_LEVEL:
            if key not in raw:
                raise SchemaValidationError(f"Missing required top-level key: '{key}'")

        global_env = raw.get("env", {}) or {}
        stages: Dict[str, Stage] = {}
        raw_stages = raw["stages"]

        if not isinstance(raw_stages, list) or len(raw_stages) == 0:
            raise SchemaValidationError("'stages' must be a non-empty list.")

        for i, raw_stage in enumerate(raw_stages):
            stage = self._parse_stage(raw_stage, index=i)
            if stage.name in stages:
                raise DuplicateStageError(f"Duplicate stage: '{stage.name}'")
            stages[stage.name] = stage

        self.config = PipelineConfig(
            version=str(raw["version"]),
            name=str(raw["name"]),
            env={str(k): str(v) for k, v in global_env.items()},
            stages=stages,
        )
        return self.config

    def _parse_stage(self, raw_stage: dict, index: int) -> Stage:
        if not isinstance(raw_stage, dict):
            raise SchemaValidationError(f"Stage at index {index} must be a dict.")

        for key in self.REQUIRED_STAGE_FIELDS:
            if key not in raw_stage:
                raise SchemaValidationError(f"Stage at index {index} missing '{key}'.")

        name = raw_stage["name"]
        commands = raw_stage["commands"]

        if not isinstance(name, str) or not name.strip():
            raise SchemaValidationError(f"Stage at index {index} has invalid 'name'.")
        if not isinstance(commands, list) or not commands:
            raise SchemaValidationError(f"Stage '{name}' needs a 'commands' list.")

        depends_on = raw_stage.get("depends_on", []) or []
        env = raw_stage.get("env", {}) or {}
        allow_failure = raw_stage.get("allow_failure", False)

        return Stage(
            name=name,
            commands=list(commands),
            depends_on=list(depends_on),
            env={str(k): str(v) for k, v in env.items()},
            allow_failure=allow_failure,
        )

    def validate(self) -> None:
        if self.config is None:
            raise PipelineConfigError("Call load() before validate().")

        stage_names = set(self.config.stages.keys())

        for stage in self.config.stages.values():
            for dep in stage.depends_on:
                if dep == stage.name:
                    raise UnknownDependencyError(f"Stage '{stage.name}' depends on itself.")
                if dep not in stage_names:
                    raise UnknownDependencyError(f"Stage '{stage.name}' missing dep '{dep}'.")

        self._topological_sort()

    def _topological_sort(self) -> List[str]:
        stages = self.config.stages
        in_degree = {name: 0 for name in stages}
        dependents: Dict[str, List[str]] = {name: [] for name in stages}

        for stage in stages.values():
            in_degree[stage.name] = len(stage.depends_on)
            for dep in stage.depends_on:
                dependents[dep].append(stage.name)

        ready = [name for name in stages if in_degree[name] == 0]
        order: List[str] = []

        while ready:
            ready.sort(key=lambda n: list(stages.keys()).index(n))
            current = ready.pop(0)
            order.append(current)
            for dependent in dependents[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    ready.append(dependent)

        if len(order) != len(stages):
            unresolved = set(stages) - set(order)
            raise CyclicDependencyError(f"Cycle detected: {sorted(unresolved)}")

        return order

    def get_execution_order(self) -> List[str]:
        if self.config is None:
            raise PipelineConfigError("Call load() and validate() first.")
        return self._topological_sort()

    def get_parallel_groups(self) -> List[List[str]]:
        if self.config is None:
            raise PipelineConfigError("Call load() and validate() first.")

        stages = self.config.stages
        remaining = dict(stages)
        completed: set = set()
        groups: List[List[str]] = []

        while remaining:
            wave = [
                name for name, stage in remaining.items()
                if all(dep in completed for dep in stage.depends_on)
            ]
            if not wave:
                raise CyclicDependencyError(f"Cycle detected: {sorted(remaining)}")
            groups.append(wave)
            for name in wave:
                completed.add(name)
                del remaining[name]

        return groups

    def get_stage(self, name: str) -> Stage:
        if self.config is None:
            raise PipelineConfigError("Call load() first.")
        return self.config.stages[name]

    def resolved_env(self, stage_name: str) -> Dict[str, str]:
        stage = self.get_stage(stage_name)
        merged = dict(self.config.env)
        merged.update(stage.env)
        return merged


if __name__ == "__main__":
    parser = ConfigParser("pipeline.yml")
    parser.load()
    parser.validate()

    print("Execution order:", parser.get_execution_order())
    print("Parallel groups:", parser.get_parallel_groups())