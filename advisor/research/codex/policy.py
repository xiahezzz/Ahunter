from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from advisor.research.contracts import ExecutionPolicy


CAPSULE_PERMISSION_PROFILE = "a_hunter_capsule_read"
CAPSULE_PERMISSION_FILESYSTEM = (
    'permissions.a_hunter_capsule_read.filesystem={'
    '":minimal"="read",":workspace_roots"={"."="read"}}'
)


@dataclass(frozen=True)
class CodexCommand:
    executable: Path
    arguments: tuple[str, ...]

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.executable), *self.arguments)


def build_command(
    executable: Path,
    policy: ExecutionPolicy,
    capsule_root: Path,
    *,
    prompt: str | None = None,
) -> CodexCommand:
    root = capsule_root.resolve()
    arguments = (
        "exec",
        "--ephemeral",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "-c",
        'web_search="disabled"',
        "-c",
        f'default_permissions="{CAPSULE_PERMISSION_PROFILE}"',
        "-c",
        CAPSULE_PERMISSION_FILESYSTEM,
        "-c",
        "allow_login_shell=false",
        "--skip-git-repo-check",
        "--model",
        policy.model,
        "-c",
        f'model_reasoning_effort="{policy.reasoning_effort}"',
        "-c",
        'approval_policy="never"',
        "-C",
        str(root),
        "--output-schema",
        str(root / "output-schema.json"),
        "--output-last-message",
        str(root / "output.json"),
        "--color",
        "never",
    )
    return CodexCommand(executable.resolve(), arguments)
