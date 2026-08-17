#!/usr/bin/env bash
# macOS: 注册一个 launchd 定时任务，每天自动备份一次。
#   ./scripts/install-launchd-agent.sh              # 默认每天 21:00
#   HOUR=9 MINUTE=30 LIMIT=50 ./scripts/install-launchd-agent.sh
#   ./scripts/install-launchd-agent.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="$SCRIPT_DIR/backup.sh"
LABEL="com.chatgpt-backup.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HOUR="${HOUR:-21}"
MINUTE="${MINUTE:-0}"
LIMIT="${LIMIT:-20}"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "错误: 这个脚本只适用于 macOS。Linux 请用 install-systemd-timer.sh。" >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已移除定时任务。"
    exit 0
fi

mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$BACKUP_SCRIPT</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LIMIT</key>
        <string>$LIMIT</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/$LABEL.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/$LABEL.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

echo "已安装定时任务，每天 $HOUR:$(printf '%02d' "$MINUTE") 备份最近 $LIMIT 个对话。"
echo
echo "常用命令:"
echo "  launchctl kickstart -p gui/$(id -u)/$LABEL   立刻跑一次"
echo "  tail -f \"$HOME/Library/Logs/$LABEL.log\"     查看日志"
echo
echo "提示: 读取 ChatGPT for macOS 的 Cookie 需要「完全磁盘访问权限」，"
echo "      请在 系统设置 → 隐私与安全性 → 完全磁盘访问权限 里把「终端」勾上。"
