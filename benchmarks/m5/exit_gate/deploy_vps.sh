#!/usr/bin/env bash
# M5 exit-gate VPS deploy script — Phase C (production-scale).
#
# Provisions a fresh Ubuntu 22.04 / 24.04 VPS to run the four exit-gate
# apps + the mock upstream behind nginx. Idempotent.
#
# Usage (from the VPS, as a user with sudo):
#   curl -fsSL https://<repo>/benchmarks/m5/exit_gate/deploy_vps.sh | bash
# Or interactively:
#   ssh ubuntu@<vps>  bash < deploy_vps.sh
#
# Env knobs:
#   LANCELOT_GIT_URL  = git URL to clone (defaults to public GitHub mirror)
#   LANCELOT_GIT_REF  = branch/tag (default: main)
#   LANCELOT_DOMAIN   = optional domain for certbot; if unset, plain :80
#   MOCK_UPSTREAM_PORT = 8901 (default)
set -euo pipefail

LANCELOT_GIT_URL="${LANCELOT_GIT_URL:-https://github.com/anthropics/lancelot.git}"
LANCELOT_GIT_REF="${LANCELOT_GIT_REF:-main}"
LANCELOT_DOMAIN="${LANCELOT_DOMAIN:-}"
PROJECT_DIR="${PROJECT_DIR:-/opt/lancelot-m5}"

echo "==> M5 exit-gate VPS deploy ($(uname -sm))"
echo "    git=${LANCELOT_GIT_URL}#${LANCELOT_GIT_REF}"
echo "    dir=${PROJECT_DIR}  domain=${LANCELOT_DOMAIN:-<plain http>}"

# Guard: PROJECT_DIR must not contain spaces — the generated systemd
# ExecStart= line uses unquoted paths.
case "$PROJECT_DIR" in
  *\ *) echo "ERROR: PROJECT_DIR='$PROJECT_DIR' contains spaces; pick a path without spaces (default /opt/lancelot-m5 is safe)." >&2; exit 1;;
esac

# --- 1. System packages ---
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
    git curl build-essential pkg-config python3.13 python3.13-venv python3-pip \
    nginx ca-certificates jq htop \
    || sudo apt-get install -y --no-install-recommends \
        git curl build-essential pkg-config python3 python3-venv python3-pip \
        nginx ca-certificates jq htop

# Optional: certbot for TLS.
if [ -n "$LANCELOT_DOMAIN" ]; then
    sudo apt-get install -y --no-install-recommends certbot python3-certbot-nginx
fi

# --- 2. Rust toolchain (for maturin / Lancelot build) ---
if ! command -v cargo >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
fi
. "$HOME/.cargo/env" 2>/dev/null || true

# --- 3. Repo clone / pull ---
if [ ! -d "$PROJECT_DIR/.git" ]; then
    sudo mkdir -p "$(dirname "$PROJECT_DIR")"
    sudo chown -R "$USER":"$USER" "$(dirname "$PROJECT_DIR")"
    git clone "$LANCELOT_GIT_URL" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
git fetch --all
git checkout "$LANCELOT_GIT_REF"
git pull --ff-only

# --- 4. Python venv + deps ---
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
. .venv/bin/activate
pip install --upgrade pip maturin
pip install -r benchmarks/m5/exit_gate/requirements.txt
# Build + install Lancelot wheel.
maturin develop --release
# Install Blazeio from local source (PyPI release is the old API).
pip install -e researchfms/blazeio/Blazeio

# --- 5. systemd units ---
sudo mkdir -p /etc/systemd/system
APP_USER="${USER}"
for u in mock_upstream lancelot fastapi robyn blazeio; do
    case "$u" in
        mock_upstream)
            cmd="$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/benchmarks/m5/exit_gate/mock_upstream.py 8901"
            ;;
        *)
            cmd="$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/benchmarks/m5/exit_gate/app_${u}.py"
            ;;
    esac
    port_env=""
    case "$u" in
        lancelot) port_env="PORT=8910" ;;
        fastapi)  port_env="PORT=8911" ;;
        robyn)    port_env="PORT=8912" ;;
        blazeio)  port_env="PORT=8913" ;;
    esac

    sudo tee "/etc/systemd/system/lancelot-m5-${u}.service" >/dev/null <<EOF
[Unit]
Description=Lancelot M5 exit-gate ${u}
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${PROJECT_DIR}
Environment="LC_UPSTREAM_URL=http://127.0.0.1:8901" "LC_EMBED_SIZE=4096" "LC_BATCH_WINDOW_MS=50" "LC_BATCH_MAX=16" ${port_env}
ExecStart=${cmd}
# NB: paths in cmd above must not contain spaces — PROJECT_DIR defaults to
# /opt/lancelot-m5 (safe). If you override PROJECT_DIR to a path with
# spaces, wrap cmd in quotes here or pre-escape.
Restart=on-failure
RestartSec=2
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
done

sudo systemctl daemon-reload
for u in mock_upstream lancelot fastapi robyn blazeio; do
    sudo systemctl enable "lancelot-m5-${u}.service"
    sudo systemctl restart "lancelot-m5-${u}.service"
done
sleep 3
sudo systemctl --no-pager status 'lancelot-m5-*' | head -40 || true

# --- 6. nginx ---
sudo cp "${PROJECT_DIR}/benchmarks/m5/exit_gate/nginx.conf" /etc/nginx/sites-available/lancelot-m5
sudo ln -sf /etc/nginx/sites-available/lancelot-m5 /etc/nginx/sites-enabled/lancelot-m5
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

# --- 7. TLS (optional) ---
if [ -n "$LANCELOT_DOMAIN" ]; then
    sudo certbot --nginx -d "$LANCELOT_DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect || true
fi

echo ""
echo "==> deploy complete. Endpoints:"
echo "    http://${LANCELOT_DOMAIN:-<vps-ip>}/lancelot/healthz"
echo "    http://${LANCELOT_DOMAIN:-<vps-ip>}/fastapi/healthz"
echo "    http://${LANCELOT_DOMAIN:-<vps-ip>}/robyn/healthz"
echo "    http://${LANCELOT_DOMAIN:-<vps-ip>}/blazeio/healthz"
echo ""
echo "Status:"
echo "    sudo systemctl --no-pager status 'lancelot-m5-*'"
echo "Logs:"
echo "    sudo journalctl -u lancelot-m5-lancelot -f"
