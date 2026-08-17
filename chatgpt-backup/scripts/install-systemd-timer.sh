#!/usr/bin/env bash
# Linux: 注册一个 systemd 用户级定时任务，每天自动备份一次。
#   ./scripts/install-systemd-timer.sh            # 默认每天 21:00
#   TIME=09:30 LIMIT=50 ./scripts/install-systemd-timer.sh
#   ./scripts/install-systemd-timer.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="$SCRIPT_DIR/backup.sh"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_NAME="chatgpt-backup"
TIME="${TIME:-21:00}"
LIMIT="${LIMIT:-20}"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "错误: 这台机器上没有 systemd。可以改用 cron:" >&2
    echo "  (crontab -l 2>/dev/null; echo \"0 21 * * * $BACKUP_SCRIPT\") | crontab -" >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now "$SERVICE_NAME.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE_NAME.service" "$UNIT_DIR/$SERVICE_NAME.timer"
    systemctl --user daemon-reload
    echo "已移除定时任务。"
    exit 0
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/$SERVICE_NAME.service" <<EOF
[Unit]
Description=备份 ChatGPT 最近对话为 Markdown
After=network-online.target

[Service]
Type=oneshot
Environment=LIMIT=$LIMIT
ExecStart=$BACKUP_SCRIPT
EOF

cat > "$UNIT_DIR/$SERVICE_NAME.timer" <<EOF
[Unit]
Description=每天备份一次 ChatGPT 对话

[Timer]
OnCalendar=*-*-* $TIME
Persistent=true
RandomizedDelaySec=5m

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME.timer"

echo "已安装定时任务，每天 $TIME 备份最近 $LIMIT 个对话。"
echo
echo "常用命令:"
echo "  systemctl --user list-timers $SERVICE_NAME.timer   查看下次运行时间"
echo "  systemctl --user start $SERVICE_NAME.service       立刻跑一次"
echo "  journalctl --user -u $SERVICE_NAME.service -n 50   查看运行日志"
echo
echo "提示: 若希望没登录桌面也能执行，请开启常驻会话: sudo loginctl enable-linger $USER"
