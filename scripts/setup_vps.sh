#!/bin/bash
set -euo pipefail

# myClaw VPS Setup Script (Ubuntu/Debian)
echo "=== myClaw VPS Setup ==="

# Must run as root
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: Run this script as root (sudo bash setup_vps.sh)"
    exit 1
fi

# 1. Create myclaw user
if ! id -u myclaw &>/dev/null; then
    useradd -r -m -s /bin/bash myclaw
    echo "Created user: myclaw"
else
    echo "User myclaw already exists"
fi

# 2. Check Python 3.11+
PYTHON_VERSION=$(python3 --version 2>/dev/null | grep -oP '\d+\.\d+' | head -1)
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ -z "$PYTHON_VERSION" ] || [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
    echo "Python 3.11+ required. Installing..."
    apt-get update
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
    apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip
    echo "Python 3.11 installed"
else
    echo "Python $PYTHON_VERSION OK"
fi

# 3. Create /opt/myclaw directory
mkdir -p /opt/myclaw
echo "Created /opt/myclaw"

# 4. Copy project files (assumes script is run from project directory or files are available)
if [ -d "$(dirname "$0")/.." ]; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PROJECT_SRC="$(cd "$SCRIPT_DIR/.." && pwd)"
    echo "Copying project from $PROJECT_SRC..."
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
        "$PROJECT_SRC/" /opt/myclaw/
fi

# 5. Install Python dependencies
cd /opt/myclaw
pip install -e . 2>/dev/null || pip3 install -e . 2>/dev/null || echo "pip install skipped (run manually if needed)"

# 6. Create runtime directories
mkdir -p /opt/myclaw/{data,signals,state,logs}
echo "Created runtime directories"

# 7. Set ownership
chown -R myclaw:myclaw /opt/myclaw
echo "Set ownership to myclaw:myclaw"

# 8. Install systemd files
cp /opt/myclaw/deploy/myclaw.service /etc/systemd/system/
cp /opt/myclaw/deploy/myclaw.timer /etc/systemd/system/
systemctl daemon-reload
echo "Installed systemd units"

# 9. Enable timer
systemctl enable myclaw.timer
systemctl start myclaw.timer
echo "Enabled and started myclaw.timer"

# 10. Create runtime.env template if not exists
if [ ! -f /opt/myclaw/config/runtime.env ]; then
    cat > /opt/myclaw/config/runtime.env << 'ENVEOF'
# myClaw Runtime Environment
# Fill in these values before starting

HYPERLIQUID_PRIVATE_KEY=
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ENVEOF
    chown myclaw:myclaw /opt/myclaw/config/runtime.env
    chmod 600 /opt/myclaw/config/runtime.env
    echo "Created config/runtime.env template (fill in secrets)"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/myclaw/config/runtime.env with your API keys"
echo "  2. Edit /opt/myclaw/config/settings.yaml if needed"
echo "  3. Check timer: systemctl status myclaw.timer"
echo "  4. View logs: tail -f /opt/myclaw/logs/cycle.log"
echo ""
echo "Claude Code installation:"
echo "  curl -fsSL https://claude.ai/install.sh | sh"
echo "  See: https://docs.anthropic.com/en/docs/claude-code"
