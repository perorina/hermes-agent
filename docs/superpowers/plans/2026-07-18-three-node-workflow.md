# Hermes Three-Node Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an owner-only Telegram workflow that plans repository tasks, runs one Claude Code job at a time on a selected SSH node, pushes an isolated agent branch, and offers PR and recovery actions without modifying Hermes conversation memory.

**Architecture:** A focused `workflow` package owns configuration, `workflow.db`, queue state, node adapters, Git worktrees, Claude execution, and GitHub events. The existing Telegram adapter gets three small optional integration points for `/workflow`, workflow text, and `wf:*` callbacks; when `workflow.enabled` is false, its behavior is unchanged. Node operations remain ordinary SSH commands: PowerShell on Windows and POSIX shell on VPS/STB.

**Tech Stack:** Python 3.10+, stdlib SQLite/asyncio/subprocess/hmac, PyYAML already shipped by Hermes, python-telegram-bot already used by the gateway, Git/gh/Claude Code CLIs, OpenSSH, Tailscale.

## Global Constraints

- Store all workflow state in `${HERMES_HOME}/workflow.db`; never read or write `state.db`.
- Accept workflow commands and callbacks only from the configured Telegram owner.
- Never execute workspace-changing commands before explicit `Setujui` approval.
- Permit only one Claude Code process globally; queue tasks FIFO and retain the project lock while waiting for user input.
- Refuse a repository whose primary clone is dirty; never stash, reset, clean, or edit that clone.
- Create `agent/hermes/<task-id>-<slug>` from the latest remote default branch in a separate worktree.
- Commit and push agent branches automatically; create a PR only after `Buat PR`; never merge.
- Direct default-branch writes are limited to the exact context-file allowlist from the design specification.
- Keep handoffs permanently, raw logs for 30 days, and seven daily SQLite backups made through SQLite backup API.
- SSH and webhook surfaces are Tailscale-only or loopback; secrets never enter prompts, logs, Git, or Telegram.
- The production pilot is `perorina/yamansari-deployment`; deployment remains disabled until separately confirmed.

---

### Task 1: Persistent Workflow State

**Files:**
- Create: `workflow/__init__.py`
- Create: `workflow/models.py`
- Create: `workflow/store.py`
- Test: `tests/workflow/test_store.py`

**Interfaces:**
- Produces: `TaskState`, `WorkflowTask`, `Project`, and `WorkflowStore(path)`.
- Produces: `create_task`, `transition_task`, `approve_task`, `list_queue`, `acquire_project_lock`, `release_project_lock`, `recover_interrupted_tasks`, `record_event`, `record_handoff`, `backup_to`, and `prune_logs`.

- [ ] Write tests against a temporary real SQLite database for schema creation, valid task transitions, FIFO queue order, unique project locks, interrupted-task recovery, event audit records, safe backup, and isolation from a neighboring `state.db`.
- [ ] Run `python -m pytest tests/workflow/test_store.py -q` and confirm failures are caused by the missing `workflow` package.
- [ ] Implement the minimal dataclasses, state-transition table, WAL-enabled schema, transactional store methods, SQLite backup, and 30-day log pruning.
- [ ] Run `python -m pytest tests/workflow/test_store.py -q` and confirm all tests pass.
- [ ] Commit with `git commit -m "feat(workflow): add isolated workflow state store"`.

### Task 2: Configuration and Repository Planning

**Files:**
- Create: `workflow/config.py`
- Create: `workflow/planner.py`
- Test: `tests/workflow/test_config.py`
- Test: `tests/workflow/test_planner.py`

**Interfaces:**
- Consumes: `Project` and `WorkflowStore` from Task 1.
- Produces: `WorkflowConfig.load(config_yaml, hermes_home)`, `RepositorySummary`, `TaskProposal`, `GitHubClient.list_recent_repositories(limit, offset)`, and `TaskPlanner.propose(repo, instruction)`.

- [ ] Write failing tests for disabled-by-default configuration, owner ID validation, project/node parsing, secret redaction, ten-repo pagination, deterministic branch slugging, scope and acceptance-criteria proposal, and production-disabled defaults.
- [ ] Run `python -m pytest tests/workflow/test_config.py tests/workflow/test_planner.py -q` and confirm the expected failures.
- [ ] Implement config loading from the `workflow:` section of `${HERMES_HOME}/config.yaml`, subprocess-based `gh` repository discovery, read-only repository inspection, and deterministic proposal construction.
- [ ] Run the two test files and confirm they pass.
- [ ] Commit with `git commit -m "feat(workflow): add project configuration and planning"`.

### Task 3: Telegram Owner Workflow

**Files:**
- Create: `workflow/telegram.py`
- Modify: `plugins/platforms/telegram/adapter.py`
- Test: `tests/workflow/test_telegram.py`
- Modify: `tests/gateway/test_telegram_approval_buttons.py`

**Interfaces:**
- Consumes: `WorkflowConfig`, `WorkflowStore`, `GitHubClient`, and `TaskPlanner`.
- Produces: `TelegramWorkflow.handle_command(update) -> bool`, `handle_text(update) -> bool`, `handle_callback(update) -> bool`, and `notify_task(task_id, text, actions)`.

- [ ] Write failing tests proving unauthorized users are rejected, `/workflow` lists numbered repositories, a selected repository asks for free-form work, a proposal shows `Setujui/Ubah/Batalkan`, approval queues the task, and non-workflow messages fall through unchanged.
- [ ] Run `python -m pytest tests/workflow/test_telegram.py tests/gateway/test_telegram_approval_buttons.py -q` and confirm the new tests fail for missing integration.
- [ ] Implement the controller and add only optional initialization plus the three adapter dispatch points. Use `wf:` callback data and the adapter's existing authorization gate.
- [ ] Run both test files and confirm they pass.
- [ ] Commit with `git commit -m "feat(workflow): add owner-only Telegram task flow"`.

### Task 4: SSH Nodes, Readiness, and Global Lock

**Files:**
- Create: `workflow/nodes.py`
- Create: `workflow/runner.py`
- Test: `tests/workflow/test_nodes.py`
- Test: `tests/workflow/test_runner.py`

**Interfaces:**
- Consumes: node configuration and workflow task/store records.
- Produces: `SSHNode.run(argv, timeout)`, `WindowsNode.readiness()`, `LinuxNode.readiness()`, `GlobalClaudeLock`, `WorkflowRunner.enqueue(task_id)`, `start()`, and `stop()`.

- [ ] Write failing tests for argv-safe SSH invocation, Windows PowerShell encoding, Tailscale/SSH readiness, disk threshold, manual Claude/IDE detection, one global execution slot, FIFO order, lock retention while waiting, and restart recovery.
- [ ] Run the node and runner tests and confirm missing-module failures.
- [ ] Implement subprocess execution without shell interpolation, node-specific readiness commands, and one background FIFO runner backed by transactional database locks.
- [ ] Run the node and runner tests and confirm they pass.
- [ ] Commit with `git commit -m "feat(workflow): add SSH nodes and global task runner"`.

### Task 5: Worktree and Claude Code Execution

**Files:**
- Create: `workflow/executor.py`
- Create: `workflow/prompts.py`
- Test: `tests/workflow/test_executor.py`
- Test: `tests/workflow/test_prompts.py`

**Interfaces:**
- Consumes: `SSHNode`, `WorkflowTask`, `Project`, `WorkflowStore`.
- Produces: `TaskExecutor.prepare`, `run_claude`, `answer_claude`, `checkpoint`, `run_checks`, `commit_and_push`, `cancel`, and `ExecutionResult`.

- [ ] Write failing tests for dirty-clone refusal, latest-remote-default worktree creation, branch naming, secret-free task prompt, stream-json question capture, checkpoint timeout, configured checks, changed-file summary, commit/push, and destructive cancellation confirmation.
- [ ] Run `python -m pytest tests/workflow/test_executor.py tests/workflow/test_prompts.py -q` and confirm expected failures.
- [ ] Implement the minimal remote command sequence and streamed Claude parser. Preserve complete raw output in the configured log directory and store only summaries/handoffs in SQLite.
- [ ] Run both test files and confirm they pass.
- [ ] Commit with `git commit -m "feat(workflow): execute Claude tasks in isolated worktrees"`.

### Task 6: Pull Requests and Signed GitHub Webhooks

**Files:**
- Create: `workflow/github.py`
- Create: `workflow/webhook.py`
- Test: `tests/workflow/test_github.py`
- Test: `tests/workflow/test_webhook.py`

**Interfaces:**
- Consumes: pushed task records and project retry policy.
- Produces: `GitHubClient.create_pr(task)`, `WebhookApp.handle(headers, body)`, signature validation, delivery deduplication, CI/review repair enqueueing, and three-attempt blocking.

- [ ] Write failing tests for button-gated PR creation, no merge command, HMAC signature validation, duplicate delivery rejection, actionable CI/review extraction, repair queueing, worktree recreation, and retry exhaustion.
- [ ] Run the GitHub and webhook tests and confirm expected failures.
- [ ] Implement `gh pr create` only from the approved callback and an authenticated loopback HTTP webhook handler that records delivery IDs before processing.
- [ ] Run both test files and confirm they pass.
- [ ] Commit with `git commit -m "feat(workflow): add PR and webhook repair lifecycle"`.

### Task 7: Wake, Power, Fallback, and Backup Operations

**Files:**
- Create: `workflow/operations.py`
- Test: `tests/workflow/test_operations.py`

**Interfaces:**
- Consumes: nodes, runner, store, and task handoffs.
- Produces: `wake_pc`, `wait_for_pc`, `power_pc`, `move_to_fallback`, `daily_backup`, and `cleanup_expired_logs`.

- [ ] Write failing tests for wake only after approval, three-minute readiness result, explicit fallback choice, Windows sleep/shutdown approval, cross-node fresh session with handoff, seven-version backups, and preservation of active artifacts below disk threshold.
- [ ] Run the operations test and confirm expected failures.
- [ ] Implement command allowlists for STB/PC operations, explicit approval records, handoff-based fallback, SQLite online backup, and retention cleanup.
- [ ] Run the operations test and confirm it passes.
- [ ] Commit with `git commit -m "feat(workflow): add recovery and node operations"`.

### Task 8: Deployment, Migration, and Pilot Verification

**Files:**
- Create: `docs/workflow-three-node.md`
- Create: `scripts/install-workflow-node.ps1`
- Create: `scripts/install-workflow-vps.sh`
- Modify: `docker-compose.yml` only if the production deployment uses this tracked compose file.
- Test: `tests/workflow/test_integration.py`

**Interfaces:**
- Consumes: all prior workflow components.
- Produces: repeatable node setup, production configuration example, service activation, backup/restore procedure, and acceptance evidence for `perorina/yamansari-deployment`.

- [ ] Write a failing integration test for `/workflow -> proposal -> approval -> queued -> prepared -> pushed -> PR option` using real SQLite and fake process transports at only the SSH/GitHub/Telegram boundaries.
- [ ] Run `python -m pytest tests/workflow/test_integration.py -q` and confirm the workflow cannot yet complete.
- [ ] Add installation scripts and documentation with exact OpenSSH/Tailscale/Git/gh/Claude prerequisites, owner config, pilot project paths, backup location, rollback, and service health checks.
- [ ] Make an online SQLite backup of `/opt/data/workflow.db` when present and a separate verified backup of `/opt/data/state.db` before deploying any image or restarting Hermes.
- [ ] Build a custom image from the pinned production commit plus this branch, start it beside the current service for a smoke check, then replace the service only after health checks pass.
- [ ] Configure Windows OpenSSH and Tailscale, authenticate VPS Tailscale/gh/Claude manually where interactive login is required, and register the pilot's primary clone and worktree root.
- [ ] Run focused tests, then `python -m pytest tests/workflow tests/gateway/test_telegram_approval_buttons.py -q`; run the pilot read-only readiness check before the first approved task.
- [ ] Execute the pilot acceptance path, record branch/commit/test evidence, and verify the primary clone remains clean.
- [ ] Commit with `git commit -m "docs(workflow): add deployment and pilot runbook"`.

## Completion Gate

- [ ] All focused workflow and Telegram tests pass from a clean checkout.
- [ ] `state.db` checksum and SQLite integrity check match before and after deployment.
- [ ] Existing Telegram chat still works when `workflow.enabled` is false and when enabled outside `/workflow` state.
- [ ] Unauthorized Telegram IDs cannot plan, approve, answer, cancel, create PRs, or trigger power/deployment actions.
- [ ] No merge or production deploy command exists in the workflow implementation.
- [ ] Pilot evidence includes selected node, clean primary clone, isolated worktree, branch, commit, checks, push, and Telegram result.
