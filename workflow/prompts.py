from __future__ import annotations

import re
from collections.abc import Sequence

from workflow.models import WorkflowTask


_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY))"
    r"\s*=\s*([^\s,;]+)"
)
_KNOWN_TOKEN = re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{8,}|sk-[A-Za-z0-9_-]{8,})\b")


def redact_secrets(value: str) -> str:
    value = _ASSIGNMENT_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return _KNOWN_TOKEN.sub("[REDACTED]", value)


def build_task_prompt(task: WorkflowTask, checks: Sequence[str]) -> str:
    instruction = redact_secrets(task.instruction)
    proposal = redact_secrets(task.proposal)
    check_lines = "\n".join(f"- {redact_secrets(item)}" for item in checks) or "- Inspect the repository and use its documented checks."
    return f"""You are executing approved Hermes workflow task #{task.id}.

Repository task:
{instruction}

Approved proposal:
{proposal}

Branch: {task.branch}
Required checks:
{check_lines}

Work only inside the current Git worktree. Keep changes scoped, preserve existing user work, and do not expose credentials. Do not merge, deploy, restart services, or modify production. Run the required checks before finishing.

If you cannot continue without a user decision, make a safe checkpoint and end your final response with exactly:
HERMES_QUESTION: <one concise question>

Otherwise finish with a concise summary of changes and checks.
"""


def build_checkpoint_prompt() -> str:
    return """Stop new work and create a safe checkpoint. Commit only coherent work if appropriate; never push secrets. Then end with:
HERMES_HANDOFF: <current commit, changed files, completed work, checks, blockers, and remaining work>
"""
