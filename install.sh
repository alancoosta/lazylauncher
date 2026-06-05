#!/usr/bin/env bash
# ── LazyLauncher installer ──────────────────────────────────────────────────
set -e

INSTALL_DIR="$HOME/.local/share/lazylauncher"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_DIR="$HOME/.config/autostart"
CONFIG_DIR="$HOME/.config/lazylauncher"
REPO_URL="https://github.com/alancoosta/lazylauncher.git"

IS_UPDATE=false
if [ -d "$INSTALL_DIR" ] && [ -f "$BIN_DIR/lazylauncher" ]; then
    IS_UPDATE=true
    echo "==> Updating LazyLauncher..."
else
    echo "==> Installing LazyLauncher..."
fi

# If piped via curl (no local repo), clone to a temp directory first
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || true
if [ -z "$SCRIPT_DIR" ] || [ ! -f "$SCRIPT_DIR/tray.py" ]; then
    echo "==> Downloading LazyLauncher..."
    TMPDIR="$(mktemp -d)"
    git clone --depth 1 "$REPO_URL" "$TMPDIR"
    exec bash "$TMPDIR/install.sh"
fi

# ── 1. dependencies ───────────────────────────────────────────────────────────
echo "==> Checking dependencies..."

MISSING_PKGS=()

check_pkg() {
    python3 -c "import gi; gi.require_version('$1', '$2'); from gi.repository import $1" 2>/dev/null \
        || MISSING_PKGS+=("$3")
}

check_pkg "Gtk"  "3.0" "python3-gi"
check_pkg "Gdk"  "3.0" "python3-gi"

# Check for Pillow (PIL)
python3 -c "from PIL import Image" 2>/dev/null || MISSING_PKGS+=("python3-pil")

# Check which AppIndicator variant is available
INDICATOR_PKG=""
python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1'); from gi.repository import AyatanaAppIndicator3" 2>/dev/null \
    && INDICATOR_PKG="ayatana" \
    || true

if [ -z "$INDICATOR_PKG" ]; then
    python3 -c "import gi; gi.require_version('AppIndicator3','0.1'); from gi.repository import AppIndicator3" 2>/dev/null \
        && INDICATOR_PKG="legacy" \
        || true
fi

if [ -z "$INDICATOR_PKG" ]; then
    MISSING_PKGS+=("gir1.2-appindicator3-0.1 or gir1.2-ayatanaappindicator3-0.1")
fi

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo ""
    echo "==> Installing missing packages..."
    INSTALL_LIST=()
    for pkg in "${MISSING_PKGS[@]}"; do
        # Map to actual apt package names
        case "$pkg" in
            python3-gi) INSTALL_LIST+=("python3-gi" "python3-gi-cairo" "gir1.2-gtk-3.0") ;;
            python3-pil) INSTALL_LIST+=("python3-pil") ;;
            *appindicator*)
                # Prefer ayatana on Ubuntu 22.04+, fall back to legacy
                if apt-cache show gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1; then
                    INSTALL_LIST+=("gir1.2-ayatanaappindicator3-0.1")
                else
                    INSTALL_LIST+=("gir1.2-appindicator3-0.1")
                fi
                ;;
        esac
    done
    sudo apt-get install -y "${INSTALL_LIST[@]}" || {
        echo ""
        echo "ERROR: Could not install packages automatically."
        echo "Please run:"
        echo "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-appindicator3-0.1"
        echo "or on Ubuntu 22.04+:"
        echo "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1"
        exit 1
    }
fi

# ── 2. copy files ─────────────────────────────────────────────────────────────
echo "==> Copying files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$CONFIG_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/lazylauncher.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/common.py"      "$INSTALL_DIR/"
cp "$SCRIPT_DIR/tray.py"        "$INSTALL_DIR/"
cp "$SCRIPT_DIR/manager.py"     "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR/icons"
cp "$SCRIPT_DIR/icons/"*.png "$INSTALL_DIR/icons/" 2>/dev/null || true
cp "$SCRIPT_DIR/icons/"*.svg "$INSTALL_DIR/icons/" 2>/dev/null || true

# Install icons into hicolor theme for proper desktop rendering
HICOLOR="$HOME/.local/share/icons/hicolor"
for size in 48 64 128 256 512; do
    dest="$HICOLOR/${size}x${size}/apps"
    mkdir -p "$dest"
    cp "$SCRIPT_DIR/icons/logo-${size}.png" "$dest/lazylauncher.png"
done
mkdir -p "$HICOLOR/scalable/apps"
cp "$SCRIPT_DIR/icons/logo.svg" "$HICOLOR/scalable/apps/lazylauncher.svg"
gtk-update-icon-cache -f -t "$HICOLOR" 2>/dev/null || true

# ── 3. launcher script ────────────────────────────────────────────────────────
echo "==> Creating launcher command..."

cat > "$BIN_DIR/lazylauncher" << 'EOF'
#!/usr/bin/env bash
exec python3 "$HOME/.local/share/lazylauncher/lazylauncher.py" "$@"
EOF
chmod +x "$BIN_DIR/lazylauncher"

# ── 4. .desktop files ─────────────────────────────────────────────────────────
echo "==> Creating .desktop entries..."

APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

cat > "$APPS_DIR/lazylauncher.desktop" << 'EOF'
[Desktop Entry]
Name=LazyLauncher
Comment=Manage and run scripts from the tray
Exec=$HOME/.local/bin/lazylauncher manage
Icon=lazylauncher
Terminal=false
Type=Application
Categories=Utility;Settings;
StartupNotify=true
StartupWMClass=lazylauncher
EOF
sed -i "s|\$HOME|$HOME|g" "$APPS_DIR/lazylauncher.desktop"
# Remove old .desktop if left over from previous install
rm -f "$APPS_DIR/io.github.lazylauncher.manager.desktop"


# ── 5. autostart ──────────────────────────────────────────────────────────────
echo "==> Setting up autostart..."
mkdir -p "$AUTOSTART_DIR"

cat > "$AUTOSTART_DIR/lazylauncher.desktop" << 'EOF'
[Desktop Entry]
Name=LazyLauncher
Comment=LazyLauncher tray daemon
Exec=$HOME/.local/bin/lazylauncher
Icon=lazylauncher
Terminal=false
Type=Application
X-GNOME-Autostart-enabled=true
EOF
sed -i "s|\$HOME|$HOME|g" "$AUTOSTART_DIR/lazylauncher.desktop"

# ── 6. seed config if empty ───────────────────────────────────────────────────
if [ ! -f "$CONFIG_DIR/config.json" ]; then
    echo "==> Creating starter config..."
    cat > "$CONFIG_DIR/config.json" << 'EOF'
{
  "scripts": [
    {
      "id": "example1",
      "name": "Example: List Files",
      "command": "ls -lah",
      "working_dir": "~",
      "icon": "",
      "pinned_icon": false,
      "enabled": true,
      "description": "Lists files in your home directory. Replace with your own script!"
    }
  ]
}
EOF
fi

# ── 7. restart running instances ──────────────────────────────────────────────
if [ "$IS_UPDATE" = true ]; then
    echo "==> Restarting running instances..."
    # Kill manager if running (it will be restarted below)
    MANAGER_PID=$(pgrep -f "lazylauncher.py manage" 2>/dev/null || true)
    if [ -n "$MANAGER_PID" ]; then
        kill $MANAGER_PID 2>/dev/null || true
        sleep 0.3
    fi
    # Restart tray daemon
    TRAY_PID=$(pgrep -f "lazylauncher.py$" 2>/dev/null || true)
    if [ -n "$TRAY_PID" ]; then
        kill $TRAY_PID 2>/dev/null || true
        sleep 0.3
        nohup "$BIN_DIR/lazylauncher" >/dev/null 2>&1 &
        echo "   Tray daemon restarted."
    fi
    # Reopen manager
    if [ -n "$MANAGER_PID" ]; then
        nohup "$BIN_DIR/lazylauncher" manage >/dev/null 2>&1 &
        echo "   Manager reopened."
    fi
fi

echo ""
if [ "$IS_UPDATE" = true ]; then
    echo "✅  LazyLauncher updated successfully!"
else
    echo "✅  LazyLauncher installed successfully!"
fi
echo ""
echo "   Start tray:    lazylauncher &"
echo "   Open manager:  lazylauncher manage"
echo ""
echo "   The tray will also start automatically on next login."
echo ""
echo "   TIP: On GNOME, if you don't see the tray icon, install:"
echo "   sudo apt install gnome-shell-extension-appindicator"
echo "   and enable it from extensions.gnome.org or GNOME Extensions app."
echo ""
