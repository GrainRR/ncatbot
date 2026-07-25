# group_daily_report

版本：`0.1.0`。根据 `message_archive` 已归档的群消息生成当天、昨天或指定日期的发言统计和前十排行。

## 前置条件

- 已初始化 NcatBot、NapCat 和 QQ；基线为 NcatBot `5.5.4`、Python `>=3.12`。
- **必须先安装并加载 `message_archive` `0.1.0`**，且目标群已产生并归档至少一条消息。
- 无额外 Python 依赖或密钥。插件会查询群成员信息；机器人需能正常调用该 QQ 接口。

## 独立安装与加载验证

先完成 [message_archive 的安装和消息写入验证](../message_archive/README.md)。若尚未安装，可在框架目录执行：

```powershell
$env:NCATBOT_HOME = "<NCATBOT_HOME>"
Set-Location $env:NCATBOT_HOME
git clone https://github.com/GrainRR/ncatbot-plugin-message_archive.git ".\plugins\message_archive"
git clone https://github.com/GrainRR/ncatbot-plugin-group-daily-report.git ".\plugins\group_daily_report"
& .\.venv\Scripts\ncatbot.exe plugin info group_daily_report
```

成功信号：`plugin info` 显示 `message_archive >=0.1.0,<0.2.0` 依赖。重启框架后，日志应显示两个插件已加载；若归档插件不存在，日报插件不能形成有效数据闭环。

## 配置

本插件没有必填的 `plugin.plugin_configs.group_daily_report` 项，也不使用环境变量或密钥。

## 使用与最小验收

任意群成员可在已产生归档消息的群内发送：

```text
#每日报表 今日
```

其他形式：

```text
#每日报表
#每日报表 昨日
#每日报表 2026-07-25
```

预期输出包含统计日期、消息总数和发言排行。示例输出：

```text
2026-07-25 群聊日报
消息总数：12
1. 用户甲：5 条
```

## 数据、更新、卸载与排障

- 本插件读取 `data/message_archive/messages.sqlite`，不以日报插件目录中的空文件替代归档数据。
- 更新前停止机器人，并备份归档 SQLite；然后在本插件目录执行 `git pull --ff-only`。
- 卸载：先 `ncatbot plugin disable group_daily_report`，再删除插件目录；不会删除消息归档库。
- 日报无数据：检查归档插件已加载、数据库存在、统计日期和群号正确；先发送新消息后重试。
- 依赖错误：检查 `manifest.toml`、`ncatbot plugin info group_daily_report` 和启动日志，不能跳过 `message_archive`。
