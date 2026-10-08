#!/usr/bin/env bash
# ==============================================================================
# Quake MCS Listener - Zero-Install One-Line Bridge (macOS / Linux)
# Runs the lightweight earthquake listener in a temporary session without git clone
# Built with Google Antigravity (AGY) & Gemini 3.8 Flash (Thinking High)
# ==============================================================================

set -e

SCRIPT_URL="https://raw.githubusercontent.com/Juanipis/quake-alert-listener/main/quake_listener.py"
TEMP_DIR="${TMPDIR:-/tmp}/quake-listener-$$"
mkdir -p "$TEMP_DIR"
TRAP_CLEANUP() {
    rm -rf "$TEMP_DIR"
}
trap TRAP_CLEANUP EXIT INT TERM

echo ""
echo "==================================================================="
echo "  🌍 Quake MCS Listener • Real-Time Earthquake Bridge"
echo "  Early warnings, quake feeds and your own sensor -> Home Assistant"
echo "==================================================================="
echo ""

# 1. Detect Python 3
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "❌ Python 3.8+ was not found on your system."
    echo ""
    if [ "$(uname)" = "Darwin" ]; then
        echo "On macOS, install Xcode command line tools by running:"
        echo "  xcode-select --install"
    else
        echo "On Linux, install python3 using your package manager:"
        echo "  Debian/Ubuntu: sudo apt update && sudo apt install -y python3"
        echo "  Fedora/RHEL:   sudo dnf install -y python3"
        echo "  Arch Linux:    sudo pacman -S python"
    fi
    exit 1
fi

# 2. Check for location arguments or auto-detect
LAT=""
LON=""
NAME=""
EXTRA_ARGS=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --lat)
            LAT="$2"
            shift 2
            ;;
        --lon)
            LON="$2"
            shift 2
            ;;
        --name)
            NAME="$2"
            shift 2
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

if [ -z "$LAT" ] || [ -z "$LON" ]; then
    echo "🔍 Auto-detecting your approximate location via IP..."
    IP_JSON=$(curl -sSL --max-time 5 https://ipapi.co/json/ 2>/dev/null || echo "{}")
    DETECTED_LAT=$(echo "$IP_JSON" | grep -o '"latitude": [^,]*' | cut -d: -f2 | tr -d ' ' || true)
    DETECTED_LON=$(echo "$IP_JSON" | grep -o '"longitude": [^,]*' | cut -d: -f2 | tr -d ' ' || true)
    DETECTED_CITY=$(echo "$IP_JSON" | grep -o '"city": "[^"]*' | cut -d\" -f4 || true)
    DETECTED_COUNTRY=$(echo "$IP_JSON" | grep -o '"country_name": "[^"]*' | cut -d\" -f4 || true)

    if [ -n "$DETECTED_LAT" ] && [ -n "$DETECTED_LON" ] && [ "$DETECTED_LAT" != "null" ]; then
        LAT="$DETECTED_LAT"
        LON="$DETECTED_LON"
        NAME="${DETECTED_CITY}, ${DETECTED_COUNTRY}"
        echo "📍 Detected location: $NAME ($LAT, $LON)"
    else
        LAT="0.0"
        LON="0.0"
        NAME="Base Station"
        echo "📍 Using default location: $NAME ($LAT, $LON)"
    fi
else
    echo "📍 User specified location: ${NAME:-Custom Station} ($LAT, $LON)"
fi

# 3. Download the single-file listener into temp directory
LISTENER_FILE="$TEMP_DIR/quake_listener.py"
echo "⬇️  Fetching latest quake_listener.py..."
curl -sSL "$SCRIPT_URL" -o "$LISTENER_FILE"

echo ""
echo "🚀 Starting Quake MCS Bridge on http://127.0.0.1:8990 ..."
echo "🌐 Open the live web app: https://juanipis.github.io/quake-alert-listener/"
echo "   (The web app will automatically detect this bridge and connect in real-time)"
echo "⌨️  Press Ctrl+C at any time to stop."
echo ""

# 4. Launch the listener (not `exec`, so the EXIT trap still removes the temp folder)
"$PYTHON_BIN" "$LISTENER_FILE" \
    --lat "$LAT" \
    --lon "$LON" \
    --name "${NAME:-Local Station}" \
    "${EXTRA_ARGS[@]}"
