# chatgpt-backup

把 ChatGPT 桌面端**最近的聊天记录**（含图片、附件）备份成 Markdown，默认保存在 `~/文档/chat_bak`。

- 纯 Python 标准库实现，**不需要 pip 安装任何依赖**，Python 3.8+ 即可运行
- 图片和附件下载到本地，Markdown 里用相对路径引用，**离线也能看图**
- 增量备份：对话没更新就跳过，图片按内容去重，不会重复下载
- 三条获取数据的途径：自动读取桌面应用登录状态 / 手动粘贴凭证 / 官方导出包离线导入

## 目录长什么样

```
~/文档/chat_bak/
├── index.md                                  # 总目录，按更新时间倒序
├── conversations/
│   ├── 2026-08-17-退出账号记录保存-68a10000/
│   │   ├── index.md                          # 这段对话的 Markdown
│   │   └── assets/
│   │       ├── 截图-Upload1234.png            # 你上传的图片
│   │       ├── 示意图-Dalle09876.png          # ChatGPT 生成的图片
│   │       └── 账号说明-Report5555.pdf        # 其它附件
│   └── 2026-08-16-配置-Clash-Verge-多订阅合并-68a20000/
│       └── index.md
├── logs/                                     # 每天一个运行日志
└── .backup-state.json                        # 增量状态，别手动删
```

每个 `index.md` 带 YAML frontmatter（标题、对话 ID、模型、时间、消息数、原始链接），Obsidian / Typora / VS Code 都能直接打开：

```markdown
---
title: "退出账号记录保存"
conversation_id: "68a10000-1111-2222-3333-444455556666"
model: "gpt-5"
update_time: "2026-08-17T09:30:00+08:00"
message_count: 7
---

# 退出账号记录保存

> 创建时间: 2026-08-15 10:22:31 · 更新时间: 2026-08-17 09:30:00 · 消息数: 7 · 模型: gpt-5

---

## 我

<sub>2026-08-15 10:22:31</sub>

![截图.png](assets/截图-Upload1234.png)

请问 如果我退出了当前账号 这些记录还能看到吗

---

## ChatGPT · gpt-5

<sub>2026-08-15 10:22:45</sub>

可以。如果你只是“退出登录”，之后再登录回同一个 ChatGPT 账号，这些聊天记录还会在。

**参考来源**

1. [OpenAI Help Center](https://help.openai.com/en/articles/8265332)
```

## 快速开始

```bash
# 1. 读取本机 ChatGPT 桌面应用（或浏览器）的登录状态
./bin/chatgpt-backup login

# 2. 备份最近 20 个对话到 ~/文档/chat_bak
./bin/chatgpt-backup backup

# 3. 看看备份了什么
./bin/chatgpt-backup list
```

Windows 用 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1
```

也可以正式安装成命令（可选）：

```bash
pip install -e .
chatgpt-backup backup
```

## 三种获取数据的方式

### 方式一：自动读取桌面应用的登录状态（最省事）

```bash
./bin/chatgpt-backup login
```

脚本会去翻本机的 Cookie 存储，支持四种格式：Chromium/Electron 的 `Cookies`（加密值会用系统密钥解密）、
Tauri/WebKitGTK 的 `cookies.sqlite`、Firefox 的 `cookies.sqlite`、macOS 原生应用的 `*.binarycookies`。
桌面应用优先，找不到就退回浏览器。想只扫桌面应用加 `--no-browser`。

macOS 上读取原生应用的 Cookie 需要给终端「完全磁盘访问权限」
（系统设置 → 隐私与安全性 → 完全磁盘访问权限）。

### 方式二：手动粘贴凭证（自动读取失败时用）

在 ChatGPT 桌面应用里按 `Ctrl+Shift+I`（macOS 是 `Cmd+Option+I`）打开开发者工具，
把 [`scripts/grab-session-token.js`](scripts/grab-session-token.js) 整段粘进 Console 回车，然后：

```bash
./bin/chatgpt-backup login --paste     # 粘贴后按 Ctrl-D
```

`--paste` 能识别三种输入：`/api/auth/session` 返回的整段 JSON、裸 access token、或者一整行 Cookie。

想让定时任务长期免维护，建议复制**会话 Cookie**而不是 access token
（后者只有几小时有效期，前者能用几周，程序会自动用它续期）：

开发者工具 → Application → Cookies → `https://chatgpt.com` → 复制 `__Secure-next-auth.session-token` 的值

```bash
./bin/chatgpt-backup login --session-token '<粘贴这里>'
```

如果你的网络环境会被 Cloudflare 拦（表现为 HTTP 403），把 `cf_clearance` 和配套的 User-Agent 一起带上：

```bash
./bin/chatgpt-backup login --session-token '<...>' --cf-clearance '<...>' --user-agent '<浏览器的 UA>'
```

### 方式三：官方导出包（完全离线，最可靠）

ChatGPT →「设置 → 数据管理 → 导出数据」，邮件收到 zip 后：

```bash
./bin/chatgpt-backup import ~/下载/xxxxx.zip
./bin/chatgpt-backup import          # 省略路径则自动在下载目录里找最新的
```

导出包里已经包含所有图片，所以这条路不需要登录，也不受接口限流影响。
缺点是要等 OpenAI 发邮件，不适合每天跑。

## 定时自动备份

```bash
# Linux (systemd 用户级定时器)，默认每天 21:00
./scripts/install-systemd-timer.sh
TIME=09:30 LIMIT=50 ./scripts/install-systemd-timer.sh
./scripts/install-systemd-timer.sh --uninstall

# macOS (launchd)
./scripts/install-launchd-agent.sh
HOUR=9 MINUTE=30 ./scripts/install-launchd-agent.sh

# Windows (计划任务)
powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1 -InstallTask -Limit 50
```

`scripts/backup.sh` 也可以直接交给 cron：

```bash
(crontab -l 2>/dev/null; echo "0 21 * * * $PWD/scripts/backup.sh") | crontab -
```

它自带文件锁（同一时间只跑一个实例）和按天切分的日志。

## 常用命令

```bash
chatgpt-backup backup                      # 备份最近 20 个对话
chatgpt-backup backup -n 100               # 最近 100 个
chatgpt-backup backup --since 7d           # 只要最近 7 天更新过的（也支持 3w / 2026-08-01）
chatgpt-backup backup --force              # 忽略增量状态，全部重写
chatgpt-backup backup --dry-run            # 只看会做什么，不写文件
chatgpt-backup backup --no-images          # 只要文字，不下载图片
chatgpt-backup backup --archived           # 连已归档的对话一起备份
chatgpt-backup backup --include-tools      # 保留代码解释器等工具调用与其输出图
chatgpt-backup backup --all-branches       # 连被“重新生成”覆盖掉的旧回答一起留下
chatgpt-backup backup --save-raw           # 顺手存一份原始 JSON，方便以后重新渲染
chatgpt-backup backup --layout flat        # 一个对话一个 .md 文件（图片集中放在 assets/）
chatgpt-backup backup -o /mnt/nas/chat_bak # 换个备份目录
chatgpt-backup doctor                      # 自检：路径、桌面应用、凭证、网络
chatgpt-backup config --set limit=50       # 把默认值写进配置文件
```

默认行为的几个约定：

| 项目 | 默认 | 说明 |
| --- | --- | --- |
| 备份目录 | `~/文档/chat_bak` | 可用 `-o` 或环境变量 `CHATGPT_BACKUP_DIR` 覆盖 |
| 数量 | 最近 20 个对话 | `-n` 调整 |
| 分支 | 只保留当前可见的那条 | 被重新生成覆盖的旧回答用 `--all-branches` 找回 |
| 系统消息 / 工具调用 | 不保留 | `--include-system` / `--include-tools` 打开 |
| 推理过程 | 保留（折叠显示） | `--no-thoughts` 关闭 |
| 已归档对话 | 不备份 | `--archived` 打开 |

配置文件和凭证放在 `~/.config/chatgpt-backup/`（macOS 在 `~/Library/Application Support/chatgpt-backup/`，
Windows 在 `%APPDATA%\chatgpt-backup\`）。`auth.json` 的权限会被设成 `600`。

## 常见问题

**提示「尚未登录」或 HTTP 401**
access token 过期了。如果当初保存的是会话 Cookie，程序会自动续期；否则重新跑一次 `login`。

**HTTP 403**
基本都是 Cloudflare。按上面「方式二」把 `cf_clearance` 和 User-Agent 一起存进去，或者直接改用方式三。

**图片位置显示「附件下载失败」**
Markdown 不会因此写不出来，正文照样保存。多半是签名下载地址过期或限流，重跑一次
`chatgpt-backup backup --force -n 5` 通常就好了。

**能不能直接从桌面应用的本地缓存里读聊天记录？**
不能可靠地读。桌面应用只是个 Web 容器，历史记录在服务器上，本地缓存既不完整也随时会变。
所以这里走的是「用你已有的登录状态调官方接口」和「解析官方导出包」这两条稳定路径。

**会不会改动我的 ChatGPT 账号？**
不会。只用到三个只读接口：会话列表、单个对话、附件下载地址。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `CHATGPT_BACKUP_DIR` | 备份目录 |
| `CHATGPT_BACKUP_CONFIG_DIR` | 配置/凭证目录 |
| `CHATGPT_ACCESS_TOKEN` | 直接提供 access token（优先级高于凭证文件） |
| `CHATGPT_SESSION_TOKEN` | 直接提供会话 Cookie |
| `CHATGPT_API_BASE` | 接口地址，默认 `https://chatgpt.com`（测试用） |

## 开发

```bash
python3 -m unittest discover -s tests -t .    # 149 个用例，约 1 秒
```

测试里带了一个模拟 `chatgpt.com` 后端接口的本地 HTTP 服务
（[`tests/fake_server.py`](tests/fake_server.py)），所以整条备份链路（列表 → 拉对话 → 下图片 →
渲染 Markdown → 增量跳过 → 凭证续期）都是真跑一遍验证的，不依赖网络和真实账号。

代码结构：

| 文件 | 职责 |
| --- | --- |
| `parse.py` | 把服务端的消息树摊平成线性对话，处理十几种 content_type |
| `render.py` | 生成 Markdown |
| `assets.py` | 图片/附件落盘、按内容去重、按魔术字节判后缀 |
| `store.py` | 目录布局、增量状态、总目录 |
| `api.py` / `auth.py` / `http.py` | 只读接口调用、凭证续期、重试与限流 |
| `sources/desktop.py` | 从桌面应用/浏览器的 Cookie 存储里找登录状态 |
| `sources/official_export.py` | 解析官方导出包 |
