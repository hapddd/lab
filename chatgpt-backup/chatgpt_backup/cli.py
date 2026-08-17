"""Command line interface.

    chatgpt-backup login      # 保存登录凭证（可自动从桌面应用读取）
    chatgpt-backup backup     # 备份最近的对话（含图片）为 Markdown
    chatgpt-backup import     # 从官方导出包生成 Markdown（无需登录）
    chatgpt-backup list       # 查看已备份的对话
    chatgpt-backup doctor     # 自检：路径、凭证、网络
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .api import AuthenticationError, ChatGPTClient, resolve_base_url
from .auth import (
    Credentials,
    SESSION_COOKIE,
    make_client,
    refresh_access_token,
    token_account_email,
    token_expiry,
    token_is_valid,
)
from .backup import BackupOptions, BackupResult, backup_from_api, backup_from_export, state_report
from .config import (
    Settings,
    auth_file,
    config_dir,
    default_output_dir,
    documents_dir,
    log,
    setup_logging,
)
from .http import HttpError
from .sources import desktop
from .sources.official_export import find_latest_export
from .util import fmt_datetime

EPILOG = """\
常用示例:
  chatgpt-backup login                      从桌面应用/浏览器自动读取登录状态
  chatgpt-backup login --paste              手动粘贴 access token 或 Cookie
  chatgpt-backup backup                     备份最近 20 个对话到 ~/文档/chat_bak
  chatgpt-backup backup -n 50 --force       重新备份最近 50 个对话
  chatgpt-backup import ~/下载/export.zip   从官方导出包生成 Markdown（无需登录）
  chatgpt-backup list                       列出已备份的对话
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse_since(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    text = value.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([dwmh])", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        hours = {"h": 1, "d": 24, "w": 24 * 7, "m": 24 * 30}[unit] * amount
        return dt.datetime.now().astimezone() - dt.timedelta(hours=hours)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt).astimezone()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"无法解析时间: {value}（支持 7d / 3w / 2026-08-01）")


def _resolve_output_dir(args: argparse.Namespace, settings: Settings) -> Path:
    if getattr(args, "out", None):
        return Path(args.out).expanduser()
    if settings.output_dir:
        return Path(settings.output_dir).expanduser()
    return default_output_dir()


def _backup_options(args: argparse.Namespace, settings: Settings) -> BackupOptions:
    return BackupOptions(
        output_dir=_resolve_output_dir(args, settings),
        limit=getattr(args, "limit", None) or settings.limit,
        force=getattr(args, "force", False),
        include_system=getattr(args, "include_system", False) or settings.include_system,
        include_tools=getattr(args, "include_tools", False) or settings.include_tools,
        include_thoughts=not getattr(args, "no_thoughts", False) and settings.include_thoughts,
        all_branches=getattr(args, "all_branches", False) or settings.all_branches,
        download_assets=not getattr(args, "no_images", False) and settings.download_assets,
        include_archived=getattr(args, "archived", False),
        layout=getattr(args, "layout", None) or settings.layout,
        dry_run=getattr(args, "dry_run", False),
        since=_parse_since(getattr(args, "since", None)),
        save_raw=getattr(args, "save_raw", False),
    )


def _report(result: BackupResult) -> int:
    log.info("")
    log.info("完成: %s", result.summary())
    if result.output_dir:
        log.info("备份目录: %s", result.output_dir)
    if result.index_path:
        log.info("索引文件: %s", result.index_path)
    if result.errors:
        log.warning("有 %d 个问题:", len(result.errors))
        for message in result.errors[:10]:
            log.warning("  - %s", message)
    return 1 if (result.failed and not result.written) else 0


def _parse_pasted(text: str) -> Dict[str, str]:
    """Accept a bare token, the JSON from /api/auth/session, or a Cookie header."""
    found: Dict[str, str] = {}
    text = (text or "").strip()
    if not text:
        return found

    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            if isinstance(payload.get("accessToken"), str):
                found["access_token"] = payload["accessToken"]
            for key in ("access_token", "session_token", "cf_clearance", "user_agent"):
                if isinstance(payload.get(key), str):
                    found[key] = payload[key]
            cookies = payload.get("cookies")
            if isinstance(cookies, dict):
                if cookies.get(SESSION_COOKIE):
                    found["session_token"] = cookies[SESSION_COOKIE]
                if cookies.get("cf_clearance"):
                    found["cf_clearance"] = cookies["cf_clearance"]
            if found:
                return found

    if "=" in text and (";" in text or text.startswith("__Secure") or "cf_clearance=" in text):
        for chunk in text.split(";"):
            if "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name, value = name.strip(), value.strip()
            if name in (SESSION_COOKIE, "next-auth.session-token"):
                found["session_token"] = value
            elif name == "cf_clearance":
                found["cf_clearance"] = value
        if found:
            return found

    token = text.split()[-1] if text.lower().startswith("bearer") else text
    token = token.strip().strip('"').strip("'")
    if token.startswith("ey") and token.count(".") >= 2:
        found["access_token"] = token
    elif token:
        # Long opaque strings are session cookies in practice.
        found["session_token" if len(token) > 100 and "." not in token else "access_token"] = token
    return found


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_login(args: argparse.Namespace, settings: Settings) -> int:
    creds = Credentials.load(use_env=False)
    changed = False

    if args.token:
        creds.access_token = args.token.strip()
        creds.source = "手动输入"
        changed = True
    if args.session_token:
        creds.session_token = args.session_token.strip()
        creds.source = "手动输入"
        changed = True
    if args.cf_clearance:
        creds.cf_clearance = args.cf_clearance.strip()
        changed = True
    if args.user_agent:
        creds.user_agent = args.user_agent.strip()
        changed = True

    if args.paste:
        log.info("请粘贴 access token / Cookie / /api/auth/session 的 JSON，然后按 Ctrl-D 结束:")
        pasted = _parse_pasted(sys.stdin.read())
        if not pasted:
            log.error("没有识别出任何凭证。")
            return 2
        for key, value in pasted.items():
            setattr(creds, key, value)
        creds.source = "手动粘贴"
        changed = True

    if not changed and not args.no_auto:
        log.info("正在扫描本机的 ChatGPT 桌面应用登录状态…")
        candidates = desktop.discover_auth(include_browsers=not args.no_browser)
        usable = [item for item in candidates if item.usable]
        if usable:
            best = usable[0]
            creds.session_token = best.session_token
            if best.cf_clearance:
                creds.cf_clearance = best.cf_clearance
            creds.cookies = {
                name: value
                for name, value in best.cookies.items()
                if name not in (SESSION_COOKIE, "cf_clearance")
            }
            creds.source = f"{best.label} ({best.path})"
            changed = True
            log.info("已从 %s 读取登录状态。", creds.source)
        else:
            log.warning("没能自动读取到登录状态。")
            if candidates:
                log.warning("扫描到这些 Cookie 存储，但里面没有会话票据:")
                for item in candidates[:6]:
                    log.warning("  - %s: %s", item.label, item.path)
            log.warning("")
            log.warning("请改用手动方式（二选一）:")
            log.warning("  1) 在 ChatGPT 桌面应用/浏览器里打开 https://chatgpt.com/api/auth/session ，")
            log.warning("     复制整段 JSON，然后执行: chatgpt-backup login --paste")
            log.warning("  2) chatgpt-backup login --session-token '<__Secure-next-auth.session-token 的值>'")
            return 2

    if not creds.has_any:
        log.error("没有任何可用凭证。")
        return 2

    path = creds.save()
    log.info("凭证已保存: %s", path)

    if args.no_verify:
        log.info("凭证状态: %s", creds.describe())
        return 0

    client = make_client(creds)
    if not token_is_valid(creds.access_token) and creds.session_token:
        log.info("正在用会话票据换取 access_token…")
        refresh_access_token(client, creds, base_url=resolve_base_url())

    if not creds.access_token:
        log.error("换取 access_token 失败，请检查凭证是否完整（Cloudflare 环境可能还需要 cf_clearance）。")
        return 2

    api = ChatGPTClient(creds, client=client)
    try:
        refs = api.list_conversation_refs(limit=1)
    except (AuthenticationError, HttpError, OSError) as exc:
        log.error("验证失败: %s", exc)
        return 2

    email = token_account_email(creds.access_token)
    log.info("登录成功%s。", f"（账号: {email}）" if email else "")
    if refs:
        log.info("最近一个对话: %s（更新于 %s）", refs[0].title, fmt_datetime(refs[0].update_time))
    expiry = token_expiry(creds.access_token)
    if expiry:
        log.info("access_token 有效期至 %s，之后会用会话票据自动续期。", fmt_datetime(expiry))
    return 0


def cmd_whoami(args: argparse.Namespace, settings: Settings) -> int:
    creds = Credentials.load()
    log.info("凭证文件: %s", auth_file())
    log.info("状态: %s", creds.describe())
    if not creds.has_any:
        log.warning("尚未登录，请先执行 chatgpt-backup login。")
        return 2
    client = make_client(creds)
    api = ChatGPTClient(creds, client=client)
    try:
        refs = api.list_conversation_refs(limit=3)
    except (AuthenticationError, HttpError, OSError) as exc:
        log.error("接口访问失败: %s", exc)
        return 2
    email = api.account_email()
    log.info("账号: %s", email or "未知")
    log.info("最近的对话:")
    for ref in refs:
        log.info("  - %s（%s）", ref.title, fmt_datetime(ref.update_time))
    return 0


def cmd_backup(args: argparse.Namespace, settings: Settings) -> int:
    options = _backup_options(args, settings)
    creds = Credentials.load()
    if not creds.has_any:
        log.error("尚未登录。请先执行 `chatgpt-backup login`，或用 `chatgpt-backup import <导出包.zip>` 离线备份。")
        return 2

    log.info("备份目录: %s", options.output_dir)
    log.info("最多备份最近 %d 个对话%s。", options.limit, "（含图片）" if options.download_assets else "（不含图片）")

    client = make_client(creds, timeout=args.timeout, proxy=args.proxy)
    api = ChatGPTClient(creds, client=client, request_delay=args.delay, timeout=args.timeout, proxy=args.proxy)
    try:
        result = backup_from_api(api, options)
    except AuthenticationError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("已被用户中断。")
        return 130
    return _report(result)


def cmd_import(args: argparse.Namespace, settings: Settings) -> int:
    options = _backup_options(args, settings)
    path = Path(args.path).expanduser() if args.path else find_latest_export()
    if path is None:
        log.error("没找到导出包。请先在 ChatGPT 里「设置 → 数据管理 → 导出数据」，")
        log.error("下载到本地后执行: chatgpt-backup import <导出包.zip>")
        return 2
    if not path.exists():
        log.error("路径不存在: %s", path)
        return 2

    log.info("导出包: %s", path)
    log.info("备份目录: %s", options.output_dir)
    try:
        result = backup_from_export(path, options)
    except (OSError, ValueError, FileNotFoundError) as exc:
        log.error("读取导出包失败: %s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("已被用户中断。")
        return 130
    return _report(result)


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    output_dir = _resolve_output_dir(args, settings)
    entries = state_report(output_dir)
    if not entries:
        log.info("%s 里还没有备份记录。", output_dir)
        return 0
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0
    log.info("备份目录: %s（共 %d 个对话）", output_dir, len(entries))
    log.info("")
    for entry in entries[: args.limit or len(entries)]:
        updated = str(entry.get("update_time") or "")[:16].replace("T", " ")
        log.info(
            "%s  %-40s  消息 %-4s 附件 %-3s  %s",
            updated or "未知时间",
            (entry.get("title") or "")[:40],
            entry.get("message_count", 0),
            entry.get("asset_count", 0),
            entry.get("path", ""),
        )
    return 0


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    output_dir = _resolve_output_dir(args, settings)
    log.info("== 基本信息 ==")
    log.info("版本: %s  Python: %s", __version__, sys.version.split()[0])
    log.info("平台: %s", sys.platform)
    log.info("配置目录: %s", config_dir())
    log.info("文档目录: %s", documents_dir())
    log.info("备份目录: %s（%s）", output_dir, "已存在" if output_dir.exists() else "尚未创建")

    log.info("")
    log.info("== 桌面应用 ==")
    app_dirs = desktop.app_data_dirs()
    if app_dirs:
        for path in app_dirs:
            log.info("  应用数据: %s", path)
    else:
        log.info("  没有发现 ChatGPT 桌面应用的数据目录。")
    sources = desktop.desktop_cookie_sources()
    if sources:
        for source in sources[:10]:
            log.info("  Cookie 存储[%s]: %s", source.kind, source.path)
    else:
        log.info("  没有发现可读的 Cookie 存储。")

    log.info("")
    log.info("== 凭证 ==")
    creds = Credentials.load()
    log.info("  %s", creds.describe())

    log.info("")
    log.info("== 网络 ==")
    base_url = resolve_base_url()
    client = make_client(creds, timeout=args.timeout, proxy=args.proxy)
    try:
        response = client.request("GET", f"{base_url}/robots.txt", retry_status=())
        log.info("  %s 可达 (HTTP %s)", base_url, response.status)
    except HttpError as exc:
        log.info("  %s 返回 HTTP %s（403 通常是 Cloudflare 拦截）", base_url, exc.status)
    except OSError as exc:
        log.warning("  无法连接 %s: %s", base_url, exc)

    if creds.has_any:
        api = ChatGPTClient(creds, base_url=base_url, client=client, timeout=args.timeout, proxy=args.proxy)
        try:
            refs = api.list_conversation_refs(limit=1)
            log.info("  接口调用正常，最近对话: %s", refs[0].title if refs else "（无）")
        except (AuthenticationError, HttpError, OSError) as exc:
            log.warning("  接口调用失败: %s", exc)

    log.info("")
    log.info("== 导出包 ==")
    latest = find_latest_export()
    log.info("  %s", f"发现候选导出包: {latest}" if latest else "下载目录里没有发现 ChatGPT 导出包。")
    return 0


def cmd_config(args: argparse.Namespace, settings: Settings) -> int:
    if args.set:
        for item in args.set:
            if "=" not in item:
                log.error("格式应为 key=value: %s", item)
                return 2
            key, value = item.split("=", 1)
            key = key.strip()
            if key not in Settings.__dataclass_fields__ or key == "extras":
                log.error("未知配置项: %s（可用: %s）", key, ", ".join(
                    name for name in Settings.__dataclass_fields__ if name != "extras"))
                return 2
            current = getattr(settings, key)
            if isinstance(current, bool):
                parsed = value.strip().lower() in ("1", "true", "yes", "on", "是")
            elif isinstance(current, int) and not isinstance(current, bool):
                parsed = int(value)
            else:
                parsed = value
            setattr(settings, key, parsed)
        path = settings.save()
        log.info("配置已保存: %s", path)

    payload = {
        key: getattr(settings, key) for key in Settings.__dataclass_fields__ if key != "extras"
    }
    payload["output_dir"] = payload["output_dir"] or str(default_output_dir())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--out", metavar="目录", help="备份输出目录（默认 ~/文档/chat_bak）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出警告和错误")
    parser.add_argument("--log-file", metavar="文件", help="把详细日志追加写入文件")


def _add_backup_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-n", "--limit", type=int, metavar="N", help="备份最近 N 个对话（默认 20）")
    parser.add_argument("--since", metavar="时间", help="只备份此时间之后更新的对话，如 7d / 3w / 2026-08-01")
    parser.add_argument("--force", action="store_true", help="忽略增量状态，全部重写")
    parser.add_argument("--no-images", action="store_true", help="不下载图片和附件")
    parser.add_argument("--include-system", action="store_true", help="保留系统消息和隐藏消息")
    parser.add_argument("--include-tools", action="store_true", help="保留工具调用与工具返回")
    parser.add_argument("--no-thoughts", action="store_true", help="不保留推理过程")
    parser.add_argument("--all-branches", action="store_true", help="包含被重新生成/编辑覆盖的旧分支")
    parser.add_argument("--archived", action="store_true", help="同时备份已归档的对话")
    parser.add_argument("--layout", choices=("folder", "flat"), help="folder=每个对话一个目录（默认），flat=单文件")
    parser.add_argument("--save-raw", action="store_true", help="同时保存原始 JSON，便于将来重新渲染")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要做什么，不写文件")


def _add_network_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=45.0, metavar="秒", help="单个请求超时（默认 45）")
    parser.add_argument("--delay", type=float, default=0.5, metavar="秒", help="请求间隔，避免触发限流（默认 0.5）")
    parser.add_argument("--proxy", metavar="URL", help="HTTP(S) 代理，如 http://127.0.0.1:7890")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chatgpt-backup",
        description="把 ChatGPT 桌面端的最近对话（含图片）备份成 Markdown。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"chatgpt-backup {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    login = subparsers.add_parser("login", help="保存登录凭证（默认自动从桌面应用读取）")
    _add_common(login)
    login.add_argument("--token", metavar="JWT", help="直接提供 access token")
    login.add_argument("--session-token", metavar="COOKIE", help="提供 __Secure-next-auth.session-token")
    login.add_argument("--cf-clearance", metavar="COOKIE", help="提供 cf_clearance（Cloudflare 拦截时需要）")
    login.add_argument("--user-agent", metavar="UA", help="与 cf_clearance 配套的 User-Agent")
    login.add_argument("--paste", action="store_true", help="从标准输入粘贴凭证")
    login.add_argument("--no-auto", action="store_true", help="不扫描本机 Cookie 存储")
    login.add_argument("--no-browser", action="store_true", help="只扫描桌面应用，跳过浏览器")
    login.add_argument("--no-verify", action="store_true", help="保存后不做联网验证")
    login.set_defaults(func=cmd_login)

    whoami = subparsers.add_parser("whoami", help="查看当前登录状态")
    _add_common(whoami)
    whoami.set_defaults(func=cmd_whoami)

    backup = subparsers.add_parser("backup", help="备份最近的对话（含图片）为 Markdown")
    _add_common(backup)
    _add_backup_flags(backup)
    _add_network_flags(backup)
    backup.set_defaults(func=cmd_backup)

    importer = subparsers.add_parser(
        "import", help="从官方导出包（zip/目录）生成 Markdown，无需登录", aliases=["import-export"]
    )
    _add_common(importer)
    _add_backup_flags(importer)
    importer.add_argument("path", nargs="?", help="导出包路径；省略则自动在下载目录里找最新的")
    importer.set_defaults(func=cmd_import)

    listing = subparsers.add_parser("list", help="列出已备份的对话")
    _add_common(listing)
    listing.add_argument("-n", "--limit", type=int, help="只显示前 N 条")
    listing.add_argument("--json", action="store_true", help="以 JSON 输出")
    listing.set_defaults(func=cmd_list)

    doctor = subparsers.add_parser("doctor", help="自检：路径、桌面应用、凭证、网络")
    _add_common(doctor)
    _add_network_flags(doctor)
    doctor.set_defaults(func=cmd_doctor)

    config_cmd = subparsers.add_parser("config", help="查看/修改默认配置")
    _add_common(config_cmd)
    config_cmd.add_argument("--set", action="append", metavar="KEY=VALUE", help="设置配置项，可重复")
    config_cmd.set_defaults(func=cmd_config)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        argv = ["backup"]
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    logfile = Path(args.log_file).expanduser() if getattr(args, "log_file", None) else None
    setup_logging(verbose=args.verbose, quiet=args.quiet, logfile=logfile)

    settings = Settings.load()
    try:
        return int(args.func(args, settings) or 0)
    except KeyboardInterrupt:
        log.warning("已被用户中断。")
        return 130
    except AuthenticationError as exc:
        log.error("%s", exc)
        return 2
    except Exception as exc:  # keep tracebacks behind -v
        if args.verbose:
            raise
        log.error("出错了: %s", exc)
        log.error("加上 -v 可以看到完整堆栈。")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
