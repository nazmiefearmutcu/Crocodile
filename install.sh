#!/usr/bin/env bash

# ANSI Color Codes
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Display ASCII banner
echo -ne "${GREEN}${BOLD}"
cat << 'EOF'
                    .-._   _ _ _ _ _ _ _ _
         .-''-.__.-'00  '-' ' ' ' ' ' ' ' '-.
         '.___ '    .   .--_'-' '-' '-' _'-' '._
          V: V 'vv-'   '_   '.       .'  _..' '.'.
            '=.____.=_.--'   :_.__.__:_   '.   : :
                    (((____.-'        '-.  /   : :
                                      (((-'\ .' /
                                    _____..'  .'
                                   '-._____.-'   
   ____                         _ _ _
  / ___|_ __ ___   ___ ___   __| (_) | ___
 | |   | '__/ _ \ / __/ _ \ / _` | | |/ _ \
 | |___| | | (_) | (_| (_) | (_| | | |  __/
  \____|_|  \___/ \___\___/ \__,_|_|_|\___|
EOF
echo -ne "${NC}"

echo -e "${GREEN}=================================================${NC}"
echo -e "${GREEN}      Crocodile CLI Framework Installer         ${NC}"
echo -e "${GREEN}=================================================${NC}"
echo

LOG_FILE="${TMPDIR:-/tmp}/crocodile_install.log"
rm -f "$LOG_FILE"
touch "$LOG_FILE"

# Helper function to run steps and handle failures
run_step() {
    local message="$1"
    local cmd="$2"
    printf "  ⟳  %-45s" "$message"
    
    # Run command and redirect all output to log file
    if eval "$cmd" >> "$LOG_FILE" 2>&1; then
        echo -e "${GREEN}✓ Done${NC}"
    else
        echo -e "${RED}✗ Failed${NC}"
        echo
        echo -e "${RED}Error: Installation failed at step: $message${NC}"
        echo -e "${RED}Error details (from $LOG_FILE):${NC}"
        echo -e "${YELLOW}------------------------------------------------------------------------${NC}"
        cat "$LOG_FILE"
        echo -e "${YELLOW}------------------------------------------------------------------------${NC}"
        exit 1
    fi
}

# 1. Verify python3 is installed and checks that the version is >= 3.12
printf "  ⟳  %-45s" "Verifying Python 3 version..."

PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

check_version() {
    local cmd="$1"
    "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >/dev/null 2>&1
}

PYTHON_OK=false
if [ -n "$PYTHON_CMD" ]; then
    if check_version "$PYTHON_CMD"; then
        PYTHON_OK=true
    fi
fi

if [ "$PYTHON_OK" = false ]; then
    echo -e "${RED}✗ Failed${NC}"
    echo
    echo -e "${RED}Error: Python 3.12+ was not found on your system.${NC}"
    if [ -n "$PYTHON_CMD" ]; then
        CURRENT_VER=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
        echo -e "Found Python version: ${YELLOW}${CURRENT_VER}${NC} (Minimum required: 3.12)"
    else
        echo -e "Python 3 is not installed."
    fi
    echo
    echo -e "${BOLD}How to install Python 3.12+ on your system:${NC}"
    echo -e "----------------------------------------"
    echo -e "${BOLD}macOS (using Homebrew):${NC}"
    echo -e "  brew install python@3.12"
    echo
    echo -e "${BOLD}Ubuntu/Debian:${NC}"
    echo -e "  sudo apt update"
    echo -e "  sudo apt install -y python3.12 python3.12-venv python3-pip"
    echo
    echo -e "${BOLD}Fedora/CentOS/RHEL:${NC}"
    echo -e "  sudo dnf install python3.12"
    echo
    echo -e "${BOLD}Arch Linux:${NC}"
    echo -e "  sudo pacman -S python"
    echo -e "----------------------------------------"
    exit 1
fi

CURRENT_VER=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
echo -e "${GREEN}✓ Verified${NC} ($CURRENT_VER)"

# 2. Check for git
printf "  ⟳  %-45s" "Verifying Git installation..."
if ! command -v git >/dev/null 2>&1; then
    echo -e "${RED}✗ Failed${NC}"
    echo
    echo -e "${RED}Error: 'git' is not installed, which is required to download the package from GitHub.${NC}"
    echo -e "Please install git first. For example:"
    echo -e "  macOS:  brew install git"
    echo -e "  Ubuntu: sudo apt install git"
    exit 1
fi
GIT_VER=$(git --version | head -n 1)
echo -e "${GREEN}✓ Verified${NC} ($GIT_VER)"

# 3. Creates the directory ~/.crocodile
run_step "Creating directory ~/.crocodile..." "mkdir -p \"\$HOME/.crocodile\""

# 4. Creates a virtual environment ~/.crocodile/venv
create_venv() {
    "$PYTHON_CMD" -m venv "$HOME/.crocodile/venv"
}
run_step "Creating virtual environment..." "create_venv"

# 5. Upgrades pip
run_step "Upgrading pip..." "\"\$HOME/.crocodile/venv/bin/pip\" install --upgrade pip"

# 6. Installs the CLI package
# The distribution is `crocodile` as of 0.3.0; pip rejects a requirement whose
# name does not match the project it resolves to. The repository rename has since
# landed, so the URL says Crocodile too — Crypcodile.git still redirects, but a
# redirect is not a thing to depend on.
run_step "Installing Crocodile..." "\"\$HOME/.crocodile/venv/bin/pip\" install \"crocodile[full] @ git+https://github.com/nazmiefearmutcu/Crocodile.git\""

# 7. Configures the wrapper scripts at ~/.local/bin
# `crocodile` is the command. `crypcodile` is kept because it was on people's
# PATH before the merge and removing it silently would break them; it is the
# deprecated console script, it prints a notice on stderr, and it goes away in
# 0.4. Until this step existed only the deprecated name was installed, so a
# fresh install handed you the one command the docs never mention.
configure_wrapper() {
    mkdir -p "$HOME/.local/bin" || return 1
    cat << 'EOF' > "$HOME/.local/bin/crocodile"
#!/bin/sh
exec "$HOME/.crocodile/venv/bin/crocodile" "$@"
EOF
    cat << 'EOF' > "$HOME/.local/bin/crypcodile"
#!/bin/sh
exec "$HOME/.crocodile/venv/bin/crypcodile" "$@"
EOF
    chmod +x "$HOME/.local/bin/crocodile" "$HOME/.local/bin/crypcodile"
}
run_step "Configuring wrapper script..." "configure_wrapper"

# 8. Safely updates ~/.bashrc and ~/.zshrc
UPDATED_PROFILES=()
update_path_config() {
    for profile in "$HOME/.bashrc" "$HOME/.zshrc"; do
        # Even if they don't exist, we can touch them
        touch "$profile"
        if ! grep -q '\.local/bin' "$profile"; then
            echo -e "\n# Crocodile CLI PATH configuration" >> "$profile"
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$profile"
            UPDATED_PROFILES+=("$profile")
        fi
    done
}
run_step "Updating PATH configuration..." "update_path_config"

# 9. Displays prominent notification message
echo
echo -e "${GREEN}${BOLD}========================================================================${NC}"
echo -e "${GREEN}${BOLD}Crocodile has successfully downloaded!${NC}"
echo -e "${BOLD}You must restart your terminal or run:${NC}"
for profile in "${UPDATED_PROFILES[@]}"; do
    echo -e "  source ~/${profile##*/}"
done
if [ ${#UPDATED_PROFILES[@]} -eq 0 ]; then
    echo -e "  source ~/.zshrc  # (or ~/.bashrc depending on your shell)"
fi
echo -e "${BOLD}before using it.${NC}"
echo -e "${GREEN}${BOLD}========================================================================${NC}"
echo
