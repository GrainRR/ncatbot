# Tinglan NcatBot 插件集

这是面向 QQ + NapCat 的 NcatBot 插件集。新用户请始终按以下顺序操作：**先初始化 NcatBot 与 NapCat，随后一次只安装一个插件，并在安装后立即验证。**

本仓库的来源、目录和版本由 [plugin-sources.toml](plugin-sources.toml) 统一维护；根 README 只维护安装顺序、来源矩阵、配置边界和排障入口。每个插件的完整安装与使用说明在自己的 README 中。

## 1. 项目说明与适用范围

- NcatBot 官方框架源码：[ncatbot/NcatBot](https://github.com/ncatbot/NcatBot)，主分支为 `main`。
- 官方 README 的新用户路径是安装 PyPI 包 `ncatbot5`、执行 `ncatbot init`，再执行 `ncatbot run`；本指南遵循该路径，不要求克隆框架源码。
- 官方仓库在 2026-07-25 显示最新发布为 `v5.5.6`。本插件集当前以本地验证过的 `ncatbot5==5.5.4` 为基线；升级前请先在干净环境验收。
- 本插件集仅覆盖 QQ/NapCat 场景。请确保你有权运行 QQ 机器人并处理群消息数据。

## 2. 环境要求

- Windows 10/11（主路径）；Git；Python **3.12 或更高**；可正常访问 PyPI、GitHub 与 QQ/NapCat 所需服务。
- NapCat 和 QQ 客户端由 NcatBot CLI 安装。不要从不明镜像或聊天记录下载安装包。
- 以框架虚拟环境运行所有 `pip` 命令；不要使用全局 `pip`。

PowerShell 预检：

```powershell
git --version
python --version
```

成功信号：两条命令均返回版本号。失败时：安装 Git，或安装/修复 64 位 Python 3.12+ 后重新打开 PowerShell。

## 3. 从零初始化 NcatBot 框架

请先在准备存放框架的**专用目录**中打开 PowerShell；该目录将成为 NcatBot 的运行目录。以下命令以 `$env:NCATBOT_HOME` 表示这个目录。它只在当前 PowerShell 窗口有效，关闭窗口后需重新设置。本节**只安装并初始化 NcatBot 框架**，不会下载任何本插件集或插件源码。

```powershell
$env:NCATBOT_HOME = (Get-Location).Path
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install "ncatbot5==5.5.4"
& .\.venv\Scripts\ncatbot.exe init --dir $env:NCATBOT_HOME
```

`ncatbot init` 可能在 `plugins/` 中生成 NcatBot 自带的示例插件；它是框架脚手架，不是第 5 节中的任一可选插件。

初始化时，`ncatbot init` 会询问机器人 QQ、管理员 QQ 和适配器；可先留空/使用默认值，但必须在配置 NapCat 前编辑本机 `config.yaml`，填写自己的机器人 QQ、管理员 QQ、WebSocket Token 和 WebUI Token。

如果初始化界面显示“AI 适配器配置”（OpenAI、DeepSeek、Qwen、Kimi 等），请跳过或留空。本项目的待办 LLM 只在安装 `todo_reminder` 后，按其独立 README 配置；该项不是 NapCat 连接配置。

| 步骤 | 成功信号 | 失败时查看 |
| --- | --- | --- |
| 选择运行目录 | `$env:NCATBOT_HOME` 显示当前目录 | 确认在专用目录中打开了 PowerShell；不要与其他项目混用 |
| 创建虚拟环境 | 出现 `$env:NCATBOT_HOME\.venv` | Python 版本与执行策略 |
| 安装框架 | `& .\.venv\Scripts\ncatbot.exe --version` 显示 `5.5.4` | `& .\.venv\Scripts\python.exe -m pip show ncatbot5` |
| 初始化项目 | 出现 `$env:NCATBOT_HOME\plugins` | `& .\.venv\Scripts\ncatbot.exe init --help` 与当前目录 |
| 生成框架配置 | 出现本地 `config.yaml` | 根据交互提示检查机器人 QQ、管理员 QQ 和适配器配置 |

不要提交 `config.yaml`。如果仓库历史中曾提交过真实 Token 或 LLM Key，请在对应服务端撤销并重新生成；仅删除文件不足以轮换密钥。

### macOS / Linux 附录

```bash
export NCATBOT_HOME="$PWD"
python3.12 -m venv .venv
"$NCATBOT_HOME/.venv/bin/python" -m pip install "ncatbot5==5.5.4"
"$NCATBOT_HOME/.venv/bin/ncatbot" init --dir "$NCATBOT_HOME"
```

## 4. 配置 NapCat 与 QQ 连接

`ncatbot init` 只初始化 NcatBot 项目和适配器配置；它**不会**下载、安装或登录 NapCat/QQ。全新机器仍必须执行下面的安装命令。若你已自行安装并登录 NapCat，可跳过该命令，直接核对 WebUI 和 WebSocket 配置。

在 `$env:NCATBOT_HOME` 中执行：

```powershell
& .\.venv\Scripts\ncatbot.exe napcat install
```

按安装程序提示安装/启动 QQ 与 NapCat，并登录**你自己的机器人 QQ**。在 NapCat WebUI 中启用本地 WebSocket 服务，令地址、端口和 Token 与 `config.yaml` 的 `adapters[0].config` 完全一致。默认模板使用 `ws://127.0.0.1:3001` 和 `http://127.0.0.1:6099`；Token 必须由你生成，不能使用示例文本。

连接诊断：

```powershell
& .\.venv\Scripts\ncatbot.exe napcat diagnose webui
& .\.venv\Scripts\ncatbot.exe napcat diagnose ws
```

成功信号：两个诊断均报告可连接/认证成功。失败时：核对 WebUI、WebSocket 的 URI 与 Token；不要把 Token 粘贴到 Issue、截图或群聊。

## 5. 按需逐个安装插件

完成第 3、4 节并确认框架和 NapCat 连接成功后，才进入本节安装插件。不要一次安装全部插件。选择一个插件后，打开其独立 README，执行其中的克隆、虚拟环境依赖安装、加载验证与最小功能验证。日报的唯一前置链是：

```text
message_archive → group_daily_report
```

| 插件 | 用途 | 唯一来源 / 推荐版本 | 目标目录 | 前置插件 | Python 依赖 | 必填配置 | 权限 | 最小验收 | 详细文档 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `message_archive` | 归档群聊/私聊消息到 SQLite | `ncatbot-plugin-message_archive` / `0.1.0` | `<NCATBOT_HOME>/plugins/message_archive` | 无 | 无 | 无 | 无 | 发一条群消息后生成数据库 | [README](plugins/message_archive/README.md) |
| `group_daily_report` | 群消息日报 | `ncatbot-plugin-group-daily-report` / `0.1.0` | `<NCATBOT_HOME>/plugins/group_daily_report` | `message_archive` | 无 | 无 | 读取群成员信息 | `#每日报表 今日` | [README](plugins/group_daily_report/README.md) |
| `group_forbidden_words` | 违禁词撤回和管理 | `ncatbot-plugin-group-forbidden-words` / `0.1.0` | `<NCATBOT_HOME>/plugins/group_forbidden_words` | 无 | 无 | 词库文件 | 机器人群管理权限；管理命令由群主/管理员发出 | 命中测试词后撤回 | [README](plugins/group_forbidden_words/README.md) |
| `group_special_title` | 设置群成员头衔 | `ncatbot-plugin-group-special-title` / `0.1.0` | `<NCATBOT_HOME>/plugins/group_special_title` | 无 | 无 | 无 | 机器人具备设置头衔权限；命令发送者必须是群主 | 群主设置一个短头衔 | [README](plugins/group_special_title/README.md) |
| `bilibili_url_parser` | 自动解析 Bilibili 视频链接 | `ncatbot-plugin-bilibili-url-parser` / `1.0.1` | `<NCATBOT_HOME>/plugins/bilibili_url_parser` | 无 | `bilibili-api-python==17.4.1`、`aiohttp==3.13.5` | 无 | 无 | 发送公开视频链接 | [README](plugins/bilibili_url_parser/README.md) |
| `todo_reminder` | LLM 自然语言待办和提醒 | `ncatbot-plugin-todo_reminder` / `0.1.0` | `<NCATBOT_HOME>/plugins/todo_reminder` | 无 | 无 | LLM 地址、模型、环境变量 Key | 群开关仅群主/管理员 | 私聊创建并确认一条待办 | [README](plugins/todo_reminder/README.md) |

所有实际 Git URL、目录名、版本在 [plugin-sources.toml](plugin-sources.toml) 中维护；不要在各 README 中另写来源。

## 6. 插件配置与密钥管理

配置分三层，优先级由低到高：

1. 框架本地 `config.yaml`：QQ、NapCat、日志和全局插件开关；
2. `plugin.plugin_configs.<plugin_name>`：插件的用户覆盖配置；
3. 进程环境变量：仅存放插件 API Key 等密钥。

第 3 节的 `ncatbot init` 已创建本机的 `config.yaml`。本仓库的 `config.example.yaml` 只作为受版本控制的脱敏结构参考；首次安装框架时不需要下载它，也不要用它覆盖已经填好的本机配置。

插件专属配置、环境变量名称和密钥导入方式只在对应插件 README 中维护；例如待办插件的 LLM 配置在 [todo_reminder README](plugins/todo_reminder/README.md)。`.env` 仅是本机保存变量的可选方式，NcatBot 不会自动加载它。

下列文件只在本机保存，绝不提交：`config.yaml`、`.env`、`data/`、`logs/`、`napcat/`。

## 7. 启动、加载与最小验收

```powershell
Set-Location $env:NCATBOT_HOME
& .\.venv\Scripts\ncatbot.exe config check
& .\.venv\Scripts\ncatbot.exe plugin list
& .\.venv\Scripts\ncatbot.exe run
```

停止使用 `Ctrl+C`。重启是停止后再次执行 `& .\.venv\Scripts\ncatbot.exe run`；开发期可使用 `& .\.venv\Scripts\ncatbot.exe dev`。查看日志：

```powershell
Get-ChildItem .\logs\*.log -ErrorAction SilentlyContinue | Get-Content -Tail 200 -Wait
```

每安装一个插件都要完成以下闭环：目录为预期路径；`& .\.venv\Scripts\ncatbot.exe plugin list` 能识别名称；启动日志显示插件已加载；执行插件 README 中的最小动作；日志没有依赖或配置错误。**目录存在不等于插件已成功加载。**

## 8. 常见故障排查

| 症状 | 检查位置/命令 | 修复动作 |
| --- | --- | --- |
| 框架无法启动 | `& .\.venv\Scripts\ncatbot.exe --version`、`& .\.venv\Scripts\ncatbot.exe config check`、`logs/` | 确认 `.venv` 存在、Python 3.12+，修正配置占位符 |
| NapCat 未连接 | `& .\.venv\Scripts\ncatbot.exe napcat diagnose webui`、`& .\.venv\Scripts\ncatbot.exe napcat diagnose ws` | 让 URI、端口、Token 与 NapCat WebUI 一致并重新登录 QQ |
| 插件未加载 | `& .\.venv\Scripts\ncatbot.exe plugin list`、启动日志、`manifest.toml` | 检查目录、`main`、插件黑名单和依赖 |
| 缺少 Python 依赖 | 启动日志和插件 README | 用 `$env:NCATBOT_HOME\.venv\Scripts\python.exe -m pip install ...` 安装 |
| 群权限不足 | 群角色与 NapCat/API 错误日志 | 授予机器人所需群管理权限，再由正确的群主/管理员重试 |
| 日报无数据 | `data/message_archive/messages.sqlite`、归档插件日志 | 先加载归档插件并产生群消息后再执行日报 |
| 待办未配置 LLM | [todo_reminder README](plugins/todo_reminder/README.md)、启动日志 | 按插件 README 配置后重启并验证 |
| WebUI/WebSocket 错误 | 两个 `napcat diagnose` 命令 | 不在日志中暴露 Token，改正 URI/Token 后重试 |

## 9. 更新、卸载与数据备份

先停止机器人。更新单个插件前，备份其数据和配置，然后在插件目录执行：

```powershell
git fetch --tags
git pull --ff-only
```

回退前记录可工作的 commit/tag：`git log --oneline --decorate -20`，再执行 `git checkout <KNOWN_GOOD_TAG_OR_COMMIT>`。卸载前先执行 `& .\.venv\Scripts\ncatbot.exe plugin disable <plugin_name>`，备份数据后才删除对应插件目录。

SQLite 备份应在机器人停止后进行，例如：

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Copy-Item ".\data\message_archive\messages.sqlite" ".\backups\messages-$stamp.sqlite"
Copy-Item ".\data\todo_reminder\todos.sqlite" ".\backups\todos-$stamp.sqlite"
```

`backups/` 也应只保存在本机或受控备份系统中。

## 10. 各插件详细文档链接

- [消息归档](plugins/message_archive/README.md)
- [群日报](plugins/group_daily_report/README.md)
- [违禁词管理](plugins/group_forbidden_words/README.md)
- [群专属头衔](plugins/group_special_title/README.md)
- [Bilibili 链接解析](plugins/bilibili_url_parser/README.md)
- [LLM 待办提醒](plugins/todo_reminder/README.md)

## 发布前新手验收

每次修改来源、依赖、命令或配置键后，必须由未参与开发者在干净 Windows 环境重跑：框架初始化、NapCat 连接、一个无依赖插件、`message_archive → group_daily_report`、待办 LLM、升级/卸载/排障。将阻塞点回写到对应 README；不要用账号、Token、密钥或完整日志作为验收材料。
