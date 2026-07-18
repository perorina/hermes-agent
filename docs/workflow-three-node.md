# Hermes Three-Node Workflow

This deployment keeps Hermes on the Azure VPS, runs normal coding work on the Windows PC, uses the VPS as an explicit fallback, and asks the STB to wake the PC. Existing Hermes memory remains in `state.db`; workflow state is isolated in `workflow.db`.

## Current Inventory

- Hermes VPS: `newvme-next`, public management IP `70.153.25.11`, container data mounted at `/opt/data`.
- Windows PC: Tailscale name `desktop-8haj6sl`, Tailscale IP `100.119.142.107`.
- Pilot primary clone: `F:\masx\repos\yamansari-deployment`, clean `main` tracking `origin/main`.
- Pilot checks: frontend `npm test` and `npm run build`; Go API `go test ./...`.
- Pre-workflow Hermes backup: `/opt/data/backups/pre-workflow/20260718T085222Z` inside the container, host path `/data/home/.hermes/backups/pre-workflow/20260718T085222Z`.
- STB Tailscale node and Wake-on-LAN command are not yet identified. Wake actions remain unavailable until both are configured.

## Node Preparation

Generate the workflow key inside the Hermes data volume. Do not reuse the PC user's private key:

```bash
sudo docker exec hermes sh -lc 'umask 077; mkdir -p /opt/data/ssh; test -f /opt/data/ssh/workflow_pc_ed25519 || ssh-keygen -q -t ed25519 -N "" -f /opt/data/ssh/workflow_pc_ed25519 -C hermes-workflow-pc'
sudo docker exec hermes cat /opt/data/ssh/workflow_pc_ed25519.pub
```

On the Windows PC, open an elevated PowerShell and pass that public key to:

```powershell
F:\masx\tropis-lab\hermes-three-node-workflow\scripts\install-workflow-node.ps1 -PublicKey 'ssh-ed25519 AAAA... hermes-workflow-pc'
```

The script installs Windows OpenSSH Server, enables key auth, disables SSH password auth, and allows TCP 22 only from Tailscale's `100.64.0.0/10` range. Existing Azure/RDP rules are not changed.

Install Tailscale prerequisites on the VPS with `scripts/install-workflow-vps.sh`, then complete `sudo tailscale up` in the browser. Tailscale documents that `tailscale up` is the interactive step which adds the machine to the tailnet. GitHub CLI and Claude Code logins are also deliberately manual.

After both machines are online, pin the Windows host key from inside Hermes:

```bash
sudo docker exec hermes sh -lc 'ssh-keyscan -H desktop-8haj6sl >> /opt/data/ssh/workflow_known_hosts && chmod 600 /opt/data/ssh/workflow_known_hosts'
sudo docker exec hermes ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/opt/data/ssh/workflow_known_hosts -i /opt/data/ssh/workflow_pc_ed25519 karen@desktop-8haj6sl -- powershell.exe -NoProfile -Command '$env:COMPUTERNAME'
```

## Hermes Configuration

Merge this section into `/opt/data/config.yaml`. Replace the Telegram owner ID with the existing allowed Telegram user ID. Do not place bot tokens, SSH private keys, or webhook secrets in YAML.

```yaml
workflow:
  enabled: true
  owner_telegram_id: 123456789
  database: workflow.db
  logs_dir: workflow-logs
  worktree_roots:
    windows-pc: 'F:\masx\agent-worktrees'
    vps: /data/worktrees
  nodes:
    windows-pc:
      kind: windows
      ssh_target: karen@desktop-8haj6sl
      identity_file: /opt/data/ssh/workflow_pc_ed25519
      known_hosts_file: /opt/data/ssh/workflow_known_hosts
    vps:
      kind: linux
      ssh_target: hermes-agent@127.0.0.1
      identity_file: /opt/data/ssh/hermes_agent_ed25519
      known_hosts_file: /opt/data/ssh/known_hosts
  projects:
    yamansari:
      repo: perorina/yamansari-deployment
      default_branch: main
      preferred_node: windows-pc
      fallback_node: vps
      primary_clones:
        windows-pc: 'F:\masx\repos\yamansari-deployment'
        vps: /data/repos/yamansari-deployment
      checks:
        - 'cmd.exe /d /s /c "cd frontend && npm ci && npm test && npm run build"'
        - 'cmd.exe /d /s /c "cd services\yamansari-api && go test ./..."'
      retry_limit: 3
      checkpoint_timeout_seconds: 60
      minimum_free_disk_gb: 10
      production: false
```

Keep `production: false` until the Yamansari deployment boundary is reviewed separately. The workflow implementation has no merge operation and no production deploy operation.

## Build and Rollback

Build from the pinned production baseline plus the workflow branch. Tag the old image before replacement. Never delete the existing image or backup during the first rollout.

```bash
docker build -t hermes-agent:three-node-workflow .
python -m pytest tests/workflow tests/gateway/test_telegram_approval_buttons.py -q
```

Before restart, run SQLite integrity checks and online backups. The workflow scheduler keeps seven `workflow-YYYYMMDD.db` files. `state.db` is not opened by the workflow package.

Rollback is: stop the new container, restore the previous image reference, and start the old container with the same `/opt/data` mount. Do not restore `state.db` unless its integrity check fails or a verified memory regression is observed. `workflow.enabled: false` disables all new Telegram routing without removing `workflow.db`.

## Acceptance Run

1. Confirm `git -C F:\masx\repos\yamansari-deployment status --porcelain` is empty.
2. Send `/workflow` from the owner Telegram account.
3. Choose `perorina/yamansari-deployment`, request a documentation-only pilot change, and inspect the proposal.
4. Press `Setujui`; verify the PC readiness check, isolated worktree, and `agent/hermes/...` branch.
5. Verify Telegram reports changed files, checks, and commit after push.
6. Press `Buat PR`; verify no merge occurs and the task worktree is removed while the remote branch remains.
7. Send `/workflow` from any other Telegram ID and verify it cannot create or approve a task.

Official setup references: [Tailscale Linux installation](https://tailscale.com/docs/install/linux), [GitHub CLI authentication](https://cli.github.com/manual/), and [Claude Code setup](https://docs.anthropic.com/en/docs/claude-code/getting-started).
