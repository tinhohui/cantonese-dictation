#!/bin/bash
# Cantonese + English offline dictation for macOS — one-paste installer.
#
# Paste this ONE line into Terminal:
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/tinhohui/cantonese-dictation/main/install.sh)"
#
# Safe to run again: it updates the code and leaves your dictionary, hotkeys,
# history and recordings alone.
#
# What it installs (all local, nothing is uploaded):
#   ~/dictation                  the dictation server + speech models
#   ~/.hammerspoon/init.lua      the hotkey client (an existing one is backed up)
#   ~/Library/LaunchAgents/com.huitinho.dictation.plist   keeps the server running
#   ~/ops/fix_dictation_now.sh   the menu's "修復語音輸入" button
#   Homebrew, Python 3.12, Hammerspoon, Ollama + two qwen2.5 models
#
# DICTATION_INSTALL_NO_START=1 stages every file but starts nothing and
# installs no system software (used to validate the bundle without touching a
# dictation service that is already running on the machine).

set -euo pipefail

BUNDLE_REPO="tinhohui/cantonese-dictation"
LABEL="com.huitinho.dictation"

DICTATION_DIR="$HOME/dictation"
HAMMERSPOON_DIR="$HOME/.hammerspoon"
OPS_DIR="$HOME/ops"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
SOCKET_PATH="/tmp/dictation.sock"
NO_START="${DICTATION_INSTALL_NO_START:-0}"

PYTHON_FORMULA="python@3.12"
SENSE_VOICE_DIR="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSE_VOICE_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${SENSE_VOICE_DIR}.tar.bz2"
PUNCT_DIR="sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8"
PUNCT_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/${PUNCT_DIR}.tar.bz2"
OLLAMA_MODELS="qwen2.5:3b qwen2.5:7b"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '    \033[1;32m✓\033[0m %s\n' "$1"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$1"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. This Mac
# ---------------------------------------------------------------------------
log "檢查部機 / Checking this Mac"
[ "$(uname -s)" = "Darwin" ] || die "呢個系統只支援 macOS。"
[ "$(uname -m)" = "arm64" ] || die "呢個系統只支援 Apple Silicon（M1 或以上）嘅 Mac。"
ok "Apple Silicon Mac, macOS $(sw_vers -productVersion)"
RAM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
if [ "$RAM_GB" -lt 16 ]; then
  warn "部機得 ${RAM_GB}GB RAM。語音識別冇問題，但背景潤色（qwen2.5:7b）會慢，建議 16GB 或以上。"
fi

# ---------------------------------------------------------------------------
# 1. Locate the bundle: next to this script, else download it
# ---------------------------------------------------------------------------
BUNDLE_DIR=""
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$(cd "$(dirname "$SELF")" && pwd)/runtime/server.py" ]; then
  BUNDLE_DIR="$(cd "$(dirname "$SELF")" && pwd)"
  ok "用緊本機嘅 bundle：$BUNDLE_DIR"
else
  log "下載程式 / Downloading the bundle"
  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "$WORK_DIR"' EXIT
  # Download to a file and let tar judge it: github.com has been seen to
  # answer the archive URL with an HTML page and HTTP 200, which a
  # curl-into-tar pipe reports only as an unreadable archive.
  FETCHED=0
  for url in \
      "https://codeload.github.com/${BUNDLE_REPO}/tar.gz/refs/heads/main" \
      "https://github.com/${BUNDLE_REPO}/archive/refs/heads/main.tar.gz"; do
    if curl -fsSL -o "$WORK_DIR/bundle.tar.gz" "$url" \
        && tar -xzf "$WORK_DIR/bundle.tar.gz" -C "$WORK_DIR" --strip-components=1 2>/dev/null; then
      FETCHED=1; break
    fi
  done
  [ "$FETCHED" = "1" ] || die "下載唔到程式。檢查網絡之後重新貼一次安裝指令。"
  rm -f "$WORK_DIR/bundle.tar.gz"
  BUNDLE_DIR="$WORK_DIR"
  ok "下載完成"
fi
[ -f "$BUNDLE_DIR/runtime/server.py" ] || die "Bundle 唔完整（搵唔到 runtime/server.py）。"

# ---------------------------------------------------------------------------
# 2. System software: Homebrew, Python, Hammerspoon, Ollama
# ---------------------------------------------------------------------------
if [ "$NO_START" = "1" ]; then
  warn "DICTATION_INSTALL_NO_START=1 — 唔裝系統軟件，唔啟動任何服務"
  command -v brew >/dev/null 2>&1 || die "NO_START 模式需要已經裝好 Homebrew。"
else
  log "Homebrew"
  if ! command -v brew >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/brew ]; then
    warn "未裝 Homebrew，而家裝（會問你部 Mac 嘅登入密碼，打字時唔會顯示，打完撳 Enter）"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
  command -v brew >/dev/null 2>&1 || die "Homebrew 裝唔到。請去 https://brew.sh 裝好再重新貼一次。"
  ok "Homebrew: $(command -v brew)"

  log "Python / Hammerspoon / Ollama"
  brew list "$PYTHON_FORMULA" >/dev/null 2>&1 || brew install "$PYTHON_FORMULA"
  ok "$PYTHON_FORMULA"
  if [ -d "/Applications/Hammerspoon.app" ] || brew list --cask hammerspoon >/dev/null 2>&1; then
    ok "Hammerspoon（已裝）"
  else
    brew install --cask hammerspoon
    ok "Hammerspoon"
  fi
  if command -v ollama >/dev/null 2>&1 || curl -s -o /dev/null "http://127.0.0.1:11434"; then
    ok "Ollama（已裝）"
  else
    brew install ollama
    ok "Ollama"
  fi
fi
PYTHON_BIN="$(brew --prefix "$PYTHON_FORMULA")/bin/python3.12"
[ -x "$PYTHON_BIN" ] || die "搵唔到 $PYTHON_BIN"

# ---------------------------------------------------------------------------
# 3. Code into ~/dictation (never overwrites your own data)
# ---------------------------------------------------------------------------
log "安裝程式去 $DICTATION_DIR"
mkdir -p "$DICTATION_DIR"
for src in "$BUNDLE_DIR"/runtime/*; do
  f="$(basename "$src")"
  case "$f" in
    dictionary.json|hotkey.json)
      if [ -f "$DICTATION_DIR/$f" ]; then
        warn "$f 已經存在，保留你原本嗰份"
      else
        cp "$src" "$DICTATION_DIR/$f"; ok "$f"
      fi ;;
    *) cp "$src" "$DICTATION_DIR/$f" ;;
  esac
done
ok "程式檔案已複製"

log "Python 環境"
if [ ! -x "$DICTATION_DIR/venv/bin/python3" ]; then
  "$PYTHON_BIN" -m venv "$DICTATION_DIR/venv"
fi
"$DICTATION_DIR/venv/bin/pip" install --quiet --upgrade pip
"$DICTATION_DIR/venv/bin/pip" install --quiet -r "$DICTATION_DIR/requirements.txt"
ok "Python 套件裝好"

# ---------------------------------------------------------------------------
# 4. Speech models (about 1.1 GB, downloaded once)
# ---------------------------------------------------------------------------
fetch_model() {  # <dir name> <url> <a file that must exist once extracted>
  if [ -f "$DICTATION_DIR/$1/$3" ]; then
    ok "$1（已有）"
    return
  fi
  rm -rf "${DICTATION_DIR:?}/$1"
  curl -L --fail --progress-bar -o "$DICTATION_DIR/$1.tar.bz2" "$2"
  tar -xjf "$DICTATION_DIR/$1.tar.bz2" -C "$DICTATION_DIR"
  rm -f "$DICTATION_DIR/$1.tar.bz2"
  [ -f "$DICTATION_DIR/$1/$3" ] || die "$1 解壓之後搵唔到 $3"
  ok "$1"
}
log "下載語音模型（約 1.1GB）"
fetch_model "$SENSE_VOICE_DIR" "$SENSE_VOICE_URL" "tokens.txt"
fetch_model "$PUNCT_DIR" "$PUNCT_URL" "model.int8.onnx"
# The archive ships a full-precision copy next to the int8 model the server
# actually loads; it is never read, so do not keep ~900MB of it on disk.
rm -f "$DICTATION_DIR/$SENSE_VOICE_DIR/model.onnx"

# ---------------------------------------------------------------------------
# 5. Hotkey client, repair button, launchd job (files only — nothing starts yet)
# ---------------------------------------------------------------------------
log "安裝 Hammerspoon 設定"
mkdir -p "$HAMMERSPOON_DIR"
if [ -f "$HAMMERSPOON_DIR/init.lua" ] && ! cmp -s "$BUNDLE_DIR/hammerspoon/init.lua" "$HAMMERSPOON_DIR/init.lua"; then
  BACKUP="$HAMMERSPOON_DIR/init.lua.bak.$(date +%Y%m%d%H%M%S)"
  cp "$HAMMERSPOON_DIR/init.lua" "$BACKUP"
  warn "你原本嘅 init.lua 已備份去 $BACKUP"
fi
cp "$BUNDLE_DIR/hammerspoon/init.lua" "$HAMMERSPOON_DIR/init.lua"
ok "$HAMMERSPOON_DIR/init.lua"

mkdir -p "$OPS_DIR"
cp "$BUNDLE_DIR/ops/fix_dictation_now.sh" "$OPS_DIR/fix_dictation_now.sh"
chmod +x "$OPS_DIR/fix_dictation_now.sh"
ok "$OPS_DIR/fix_dictation_now.sh"

mkdir -p "$(dirname "$PLIST_PATH")"
sed "s|__HOME__|$HOME|g" "$BUNDLE_DIR/launchd/$LABEL.plist.template" > "$PLIST_PATH"
plutil -lint "$PLIST_PATH" >/dev/null || die "launchd plist 格式有問題：$PLIST_PATH"
ok "$PLIST_PATH"

if [ "$NO_START" = "1" ]; then
  log "已完成（NO_START）：檔案全部就位，冇啟動任何嘢"
  exit 0
fi

# ---------------------------------------------------------------------------
# 6. Ollama + polish models (about 6.6 GB, downloaded once)
# ---------------------------------------------------------------------------
log "啟動 Ollama"
if ! curl -s -o /dev/null "http://127.0.0.1:11434"; then
  brew services start ollama >/dev/null 2>&1 || open -a Ollama 2>/dev/null || true
fi
for i in $(seq 1 60); do
  curl -s -o /dev/null "http://127.0.0.1:11434" && break
  [ "$i" -eq 60 ] && die "Ollama 60 秒內未起到。開一次 Ollama app 之後重新貼一次安裝指令。"
  sleep 1
done
ok "Ollama 已經運行"
for m in $OLLAMA_MODELS; do
  log "下載潤色模型 $m（第一次要幾分鐘）"
  ollama pull "$m"
done

# ---------------------------------------------------------------------------
# 7. Start the server, then the hotkey client
# ---------------------------------------------------------------------------
log "啟動語音輸入 server"
GUI="gui/$(id -u)"
if launchctl print "$GUI/$LABEL" >/dev/null 2>&1; then
  # Updating an existing install: bootout returns before the old job is fully
  # gone, and bootstrapping over it fails, so wait for it to disappear.
  launchctl bootout "$GUI/$LABEL" >/dev/null 2>&1 || true
  for i in $(seq 1 20); do
    launchctl print "$GUI/$LABEL" >/dev/null 2>&1 || break
    sleep 1
  done
fi
launchctl bootstrap "$GUI" "$PLIST_PATH"
echo "    macOS 可能會彈出「Python 想使用麥克風」— 請撳「允許」。"
READY=0
for i in $(seq 1 180); do
  if [ "$(printf 'PING' | nc -U -w 2 "$SOCKET_PATH" 2>/dev/null | tr -d '\n')" = "pong" ]; then
    READY=1; break
  fi
  sleep 1
done
if [ "$READY" = "1" ]; then
  ok "Server 已就緒"
else
  warn "Server 3 分鐘內未回應 — 睇 /tmp/dictation.log。Hammerspoon 開咗之後會再試。"
fi

log "啟動 Hammerspoon"
pkill -x Hammerspoon >/dev/null 2>&1 || true
sleep 1
open -a Hammerspoon
echo "    如果彈出「Hammerspoon 係由互聯網下載嘅 App」— 請撳「打開」。"
HS_CLI="/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs"
AUTOLAUNCH=0
for i in $(seq 1 20); do
  if [ -x "$HS_CLI" ] && "$HS_CLI" -c "hs.autoLaunch(true)" >/dev/null 2>&1; then
    AUTOLAUNCH=1; break
  fi
  sleep 1
done
[ "$AUTOLAUNCH" = "1" ] && ok "Hammerspoon 已設定開機自動啟動"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" >/dev/null 2>&1 || true

cat <<EOF

$(printf '\033[1;32m')安裝完成。仲有兩個權限要你親手批（macOS 規定，冇得自動）：$(printf '\033[0m')

  1. 「系統設定 → 私隱與保安 → 輔助使用」（已經幫你開咗嗰版）
     將 Hammerspoon 嗰個掣打開。冇呢個權限，快捷鍵完全冇反應。
     開完之後撳選單列 🎙️ → 「重載設定」。
  2. 如果彈出「Python 想使用麥克風」，撳「允許」。
     （冇彈出又錄唔到聲：系統設定 → 私隱與保安 → 麥克風 → 開啟 Python）

之後就用得：
  • 撳住 右⌘ 講嘢，鬆手就出字
  • 輕撳一下 右⌘ = 長段落模式（再撳一下完成）
  • 選單列嘅 🎙️ 圖示可以轉語言、開關潤色、睇返最近轉寫
  • 字典同記錄：http://127.0.0.1:8765
EOF
if [ "$AUTOLAUNCH" != "1" ]; then
  echo "  • 想開機自動啟動：撳選單列 Hammerspoon 圖示 → Preferences → Launch Hammerspoon at login"
fi
echo
