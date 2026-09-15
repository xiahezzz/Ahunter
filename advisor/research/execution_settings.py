"""Local model selection, immutable policies, and per-request policy pinning."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

import yaml

from advisor.config import AdvisorConfig, resolve_research_catalog
from advisor.research.catalog import load_catalog_from_directory
from advisor.research.contracts import ExecutionPolicy, VersionRef
from advisor.research.daily_teams import DailyTeamSetService, _atomic_replace_yaml, _sync_directory, _yaml_mapping


class ExecutionSettingsConflict(ValueError):
    pass


def _model_options(current: ExecutionPolicy) -> list[dict]:
    """Read only public selection metadata from the local Codex model cache."""
    cache = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
    options = {}
    try:
        with cache.open(encoding="utf-8") as handle:
            payload = json.loads(handle.read(2_000_001))
        models = payload.get("models", []) if isinstance(payload, dict) else []
        for item in models if isinstance(models, list) else []:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            model = item.get("slug")
            levels = item.get("supported_reasoning_levels", [])
            if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}", model) or not isinstance(levels, list):
                continue
            efforts = list(dict.fromkeys(level["effort"] for level in levels
                if isinstance(level, dict) and isinstance(level.get("effort"), str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", level["effort"])))
            if efforts:
                default = item.get("default_reasoning_level")
                options[model] = {"model": model, "reasoning_efforts": efforts,
                                  "default_reasoning_effort": default if default in efforts else efforts[0]}
    except (OSError, ValueError):
        pass
    # Keep the configured pair visible even when offline or absent from the cache.
    if current.model not in options:
        options[current.model] = {"model": current.model, "reasoning_efforts": [current.reasoning_effort],
                                  "default_reasoning_effort": current.reasoning_effort}
    elif current.reasoning_effort not in options[current.model]["reasoning_efforts"]:
        options[current.model]["reasoning_efforts"].append(current.reasoning_effort)
    return list(options.values())


class ExecutionSettingsService:
    def __init__(self, *, root: Path, config_path: Path):
        self.root = root.resolve()
        self.config_path = config_path.resolve()
        if not self.config_path.is_relative_to(self.root):
            raise ValueError("模型配置必须位于项目目录内")

    def _load(self):
        payload = _yaml_mapping(self.config_path)
        config = AdvisorConfig.model_validate(payload)
        catalog_dir = resolve_research_catalog(config, self.root)
        if not catalog_dir.is_relative_to(self.root):
            raise ValueError("研究目录必须位于项目目录内")
        catalog = load_catalog_from_directory(catalog_dir)
        return payload, catalog_dir, catalog, catalog.execution_policy(config.research.execution_policy)

    def read(self) -> dict:
        _, _, _, current = self._load()
        return self._view(current)

    @staticmethod
    def _view(policy: ExecutionPolicy) -> dict:
        # Never expose executable paths, proxy endpoints or environment values.
        options = _model_options(policy)
        return {"policy_ref": str(policy.policy), "model": policy.model,
                "reasoning_effort": policy.reasoning_effort,
                "known_models": [item["model"] for item in options], "model_options": options}

    def update(self, *, expected_policy_ref: str, model: str, reasoning_effort: str) -> dict:
        if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}", model):
            raise ValueError("请输入有效的模型名称")
        expected = str(VersionRef.parse(expected_policy_ref))
        # Share the existing advisor.yaml writer lock with daily-Team edits.
        with DailyTeamSetService(root=self.root, config_path=self.config_path)._configuration_lock():
            payload, directory, catalog, current = self._load()
            if str(current.policy) != expected:
                raise ExecutionSettingsConflict("模型配置已更新，请刷新后重试")
            if not any(item["model"] == model and reasoning_effort in item["reasoning_efforts"]
                       for item in _model_options(current)):
                raise ValueError("请选择有效的模型及推理强度组合")
            if current.model == model and current.reasoning_effort == reasoning_effort:
                return self._view(current)
            version = 1 + max(item.policy.version for item in catalog.execution_policies.values()
                              if item.policy.id == current.policy.id)
            policy = ExecutionPolicy.model_validate({
                **current.model_dump(mode="json"),
                "policy": {"id": current.policy.id, "version": version}, "model": model,
                "reasoning_effort": reasoning_effort,
            })
            target_dir = directory / "execution"
            target = target_dir / f"{current.policy.id}-v{version}.yaml"
            descriptor, temporary = tempfile.mkstemp(prefix=".policy-", suffix=".tmp", dir=target_dir)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    yaml.safe_dump(policy.model_dump(mode="json"), handle, allow_unicode=True, sort_keys=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.link(temporary, target)  # Never overwrite a published policy.
                _sync_directory(target_dir)
            finally:
                os.unlink(temporary)
            payload.setdefault("research", {})["execution_policy"] = str(policy.policy)
            AdvisorConfig.model_validate(payload)
            _atomic_replace_yaml(self.config_path, payload)
            return self.read()


def request_execution_policy(runtime, request_id: str) -> ExecutionPolicy:
    """Reload defaults at request start, then keep them across process recovery."""
    config_path = getattr(runtime, "config_path", None)
    if config_path is None:
        return runtime.policy
    repository = runtime.repository
    with repository.transaction():
        pinned = repository.connection.execute(
            "SELECT policy_json FROM research_request_policies WHERE request_id=?", (request_id,),
        ).fetchone()
        if pinned:
            return ExecutionPolicy.model_validate_json(pinned[0])
        _, _, catalog, current = ExecutionSettingsService(root=runtime.root, config_path=config_path)._load()
        # Old requests predating policy pinning can already have durable invocations.
        cycle_id = f"cycle-{request_id.removeprefix('request-')}"
        refs = repository.connection.execute(
            "SELECT policy_ref FROM research_invocations WHERE cycle_id=? UNION "
            "SELECT policy_ref FROM research_scope_invocations WHERE cycle_id=?", (cycle_id, cycle_id),
        ).fetchall()
        if len(refs) > 1:
            raise ValueError("研究任务存在不一致的模型策略")
        if refs:
            current = catalog.execution_policy(refs[0][0])
        repository.connection.execute(
            "INSERT INTO research_request_policies(request_id, policy_json) VALUES (?, ?)",
            (request_id, current.model_dump_json()),
        )
        return current
