# message_archive

版本：`0.1.0`。该插件把机器人收到的群聊和私聊消息写入 SQLite，为 `group_daily_report` 提供唯一的消息数据源。适合需要消息留存、统计或日报的群。

## 前置条件

- 已完成根 [README](../../README.md) 的 NcatBot、NapCat 与 QQ 初始化。
- NcatBot：本项目验证基线 `5.5.4`；Python：`>=3.12`。
- 无前置插件、额外 Python 依赖或群管理权限。

## 独立安装与加载验证

在 PowerShell 中设定并进入已初始化的框架目录：

```powershell
$env:NCATBOT_HOME = "<NCATBOT_HOME>"
Set-Location $env:NCATBOT_HOME
git clone https://github.com/GrainRR/ncatbot-plugin-message_archive.git ".\plugins\message_archive"
& .\.venv\Scripts\ncatbot.exe plugin info message_archive
```

成功信号：最后一条命令显示版本 `0.1.0` 与入口 `message_archive.py`。随后重启框架：

```powershell
& .\.venv\Scripts\ncatbot.exe run
```

启动日志应出现“消息归档数据库已就绪”。`plugin info` 只能证明目录和清单被识别；日志和下一节的消息写入才能证明插件实际加载。

## 配置

本插件没有 `plugin.plugin_configs.message_archive` 必填项，也不需要环境变量或密钥。不要为它创建 API Key。

## 使用与最小验收

插件自动工作，没有用户命令。向机器人已加入的测试群发送一条普通消息，例如：

```text
归档验证消息
```

预期：机器人可不回复；启动目录下出现 `data/message_archive/messages.sqlite`，日志没有写入异常。该 SQLite 文件包含消息内容和元数据，应按敏感数据处理。

## 数据、更新、卸载与排障

- 数据文件：`<NCATBOT_HOME>/data/message_archive/messages.sqlite`。
- 更新前先停止机器人，备份该文件，再在插件目录执行 `git pull --ff-only`。
- 卸载前备份数据库，执行 `ncatbot plugin disable message_archive`，然后删除 `<NCATBOT_HOME>/plugins/message_archive`。日报插件仍存在时不要卸载归档插件。
- 数据库未生成：确认插件启动日志、机器人已收到消息、`data/` 可写。
- 写入失败：查看 `logs/` 中的归档异常；不要上传数据库或原始消息日志到公开位置。
