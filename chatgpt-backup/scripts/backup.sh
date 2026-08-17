#!/usr/bin/env bash
# 一键备份：把最近的 ChatGPT 对话（含图片）写进 ~/文档/chat_bak，并留一份运行日志。
# 适合手动双击运行，也适合交给 cron / systemd timer / launchd 定时执行。
#
# 可用环境变量:
#   CHATGPT_BACKUP_DIR   备份目录，默认 ~/文档/chat_bak
#   LIMIT                备份最近多少个对话，默认 20
#   EXTRA_ARGS           额外参数，例如 "--include-tools --save-raw"
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$(dirname "$SCRIPT_DIR")/bin/chatgpt-backup"
LIMIT="${LIMIT:-20}"

if [ ! -x "$LAUNCHER" ]; then
    echo "错误: 找不到启动器 $LAUNCHER" >&2
    exit 1
fi

# 解析备份目录（与 Python 端同一套默认规则）。
OUT_DIR="${CHATGPT_BACKUP_DIR:-}"
if [ -z "$OUT_DIR" ]; then
    DOC_DIR="$(xdg-user-dir DOCUMENTS 2>/dev/null || true)"
    if [ -z "$DOC_DIR" ] || [ "$DOC_DIR" = "$HOME" ]; then
        if [ -d "$HOME/文档" ]; then
            DOC_DIR="$HOME/文档"
        else
            DOC_DIR="$HOME/Documents"
        fi
    fi
    OUT_DIR="$DOC_DIR/chat_bak"
fi

LOG_DIR="$OUT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/backup-$(date +%Y%m%d).log"

run_backup() {
    "$LAUNCHER" backup \
        --out "$OUT_DIR" \
        --limit "$LIMIT" \
        --log-file "$LOG_FILE" \
        ${EXTRA_ARGS:-}
}

# 同一时间只允许跑一个实例，避免定时任务和手动运行撞在一起。
LOCK_FILE="$OUT_DIR/.backup.lock"
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        echo "已有另一个备份任务在运行，本次跳过。" >&2
        exit 0
    fi
fi

echo "==== $(date '+%Y-%m-%d %H:%M:%S') 开始备份 ===="
run_backup
STATUS=$?
echo "==== $(date '+%Y-%m-%d %H:%M:%S') 结束，退出码 $STATUS ===="

if [ "$STATUS" -eq 2 ]; then
    cat >&2 <<'HINT'

登录状态不可用。请任选一种方式处理:
  1) 重新读取桌面应用的登录状态:  ./bin/chatgpt-backup login
  2) 手动粘贴凭证:                ./bin/chatgpt-backup login --paste
  3) 完全离线的方案: 在 ChatGPT 里「设置 → 数据管理 → 导出数据」，下载后执行
     ./bin/chatgpt-backup import ~/下载/xxxx.zip
HINT
fi

exit "$STATUS"
