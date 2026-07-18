#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as the normal VPS user with sudo access, not as root." >&2
  exit 1
fi

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

sudo systemctl enable --now tailscaled
sudo install -d -m 0750 -o "${USER}" -g "${USER}" \
  /data/repos /data/worktrees /data/home/.hermes/workflow-logs \
  /data/home/.hermes/backups/workflow

echo
echo "Tailscale is installed. Interactive action still required:"
echo "  sudo tailscale up"
echo
echo "Required manual identities on the VPS fallback node:"
echo "  gh auth login"
echo "  claude"
echo
echo "Verification:"
echo "  tailscale status"
echo "  gh auth status"
echo "  claude --version"
