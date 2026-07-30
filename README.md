# Tinglan NcatBot 插件集

这是面向 QQ + NapCat 的 NcatBot 插件集。新用户请始终按以下顺序操作：**先初始化 NcatBot 与 NapCat，随后一次只安装一个插件，并在安装后立即验证。**

根 README 只维护安装顺序、插件矩阵、配置边界和排障入口。每个插件的完整安装与使用说明在自己的 README 中。

## 1. 项目说明与适用范围

- NcatBot 官方框架源码：[ncatbot/NcatBot](https://github.com/ncatbot/NcatBot)。
- 官方 README 的新用户路径是安装 PyPI 包 `ncatbot5`、执行 `ncatbot init`，再执行 `ncatbot run`；本指南遵循该路径，不要求克隆框架源码。
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
& .\.venv\Scripts\python.exe -m pip install ncatbot5
& .\.venv\Scripts\ncatbot.exe init --dir $env:NCATBOT_HOME
```

`ncatbot init` 可能在 `plugins/` 中生成 NcatBot 自带的示例插件；它是框架脚手架，不是第 5 节中的任一可选插件。如果没有插件开发的需求可无视。

初始化时，`ncatbot init` 会询问机器人 QQ、管理员 QQ 和适配器，请至少填写机器人的 QQ 号，其余可以留空。

初始化过程中请按照提示取消勾选 AI 适配器**并且勾选 NapCat (OneBot v11) 适配器**

### macOS / Linux 附录

```bash
export NCATBOT_HOME="$PWD"
python3.12 -m venv .venv
"$NCATBOT_HOME/.venv/bin/python" -m pip install ncatbot5
"$NCATBOT_HOME/.venv/bin/ncatbot" init --dir "$NCATBOT_HOME"
```

## 4. 按需安装插件

完成第 3 节并确认框架和 NapCat 连接成功后，才进入本节安装插件。

不需要一次安装所有插件但个别插件有其他依赖，请参考下方表格与插件的 readme.md 。

请在 release 中选择自己需要的插件下载并解压到 plugins 目录下。

| 插件 | 用途 | 必需配置 | 备注 |
| --- | --- | --- | --- |
| [message_archive](plugins/message_archive/README.md) | 归档群聊/私聊消息到 SQLite | 无 | 无特殊群权限要求 |
| [group_daily_report](plugins/group_daily_report/README.md) | 群消息日报 | 无 | 依赖 `message_archive`,意思就是你需要先安装`message_archive`再安装本插件；机器人需能读取群消息和成员信息 |
| [group_forbidden_words](plugins/group_forbidden_words/README.md) | 违禁词撤回和管理 | 词库文件 | 机器人须有群管理/撤回权限；管理命令仅群主或管理员可用 |
| [group_special_title](plugins/group_special_title/README.md) | 设置群成员头衔 | 无 | 机器人须为群主；申请可由群成员发送，发放仅管理员可发送 |
| [bilibili_url_parser](plugins/bilibili_url_parser/README.md) | 自动解析 Bilibili 视频链接 | 无 | 无特殊群权限要求；无需安装 B 站客户端，但需在框架虚拟环境安装 `bilibili-api-python==17.4.1` 和 `aiohttp==3.13.5` |
| [todo_reminder](plugins/todo_reminder/README.md) | LLM 自然语言待办和提醒 | LLM 地址、模型和环境变量 Key | 群开关仅群主/管理员可改；机器人需能私聊创建者以投递提醒 |



## 5. 启动、加载与最小验收

```powershell
Set-Location $env:NCATBOT_HOME
& .\.venv\Scripts\ncatbot.exe config check
& .\.venv\Scripts\ncatbot.exe plugin list
& .\.venv\Scripts\ncatbot.exe run
```

停止使用 `Ctrl+C`。重启是停止后再次执行 `& .\.venv\Scripts\ncatbot.exe run`

开发期可使用 `& .\.venv\Scripts\ncatbot.exe dev`查看日志：

```powershell
Get-ChildItem .\logs\*.log -ErrorAction SilentlyContinue | Get-Content -Tail 200 -Wait
```

每安装一个插件都要完成以下闭环：目录为预期路径；`& .\.venv\Scripts\ncatbot.exe plugin list` 能识别名称；启动日志显示插件已加载；执行插件 README 中的最小动作；日志没有依赖或配置错误。**目录存在不等于插件已成功加载。**

## 6. 常见故障排查

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
