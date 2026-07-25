# todo_reminder

版本：`0.1.0`。该插件用 OpenAI 兼容的 LLM 接口理解自然语言待办、生成待确认候选，并在到点后私聊提醒创建者。适合私聊待办；群待办默认关闭。

## 前置条件

- 已完成 NcatBot、NapCat 和 QQ 初始化；NcatBot `5.5.4`、Python `>=3.12`。
- 无前置插件和额外 Python 包；使用 Python 标准库访问 LLM。
- 需要一个支持 `chat/completions` 与工具调用的 OpenAI 兼容 API、可用模型名，以及通过环境变量提供的 API Key。
- 只有群主或管理员可执行 `#待办 开启` / `#待办 关闭`；群成员只能管理自己的待办。机器人必须能私聊创建者，否则无法投递提醒或候选确认。

## 独立安装与加载验证

```powershell
$env:NCATBOT_HOME = "<NCATBOT_HOME>"
Set-Location $env:NCATBOT_HOME
git clone https://github.com/GrainRR/ncatbot-plugin-todo_reminder.git ".\plugins\todo_reminder"
& .\.venv\Scripts\ncatbot.exe plugin info todo_reminder
```

成功信号：`plugin info` 显示 `todo_reminder` `0.1.0` 和入口 `todo_reminder.py`。配置 LLM 后重启框架：

```powershell
& .\.venv\Scripts\ncatbot.exe run
```

启动日志应显示待办数据库已就绪。目录存在或 `plugin info` 成功不代表 LLM 已配置。

## 配置与密钥

在框架根目录的 `config.yaml` 配置第三层覆盖；不要把真实 Key 写入 YAML：

```yaml
plugin:
  plugin_configs:
    todo_reminder:
      llm_api_base: "https://<LLM_PROVIDER_HOST>/v1"
      llm_api_key_env: "TODO_REMINDER_LLM_API_KEY"
      llm_model: "<LLM_MODEL_NAME>"
      llm_timeout_seconds: 30
      timezone: "Asia/Shanghai"
      reminder_check_interval: "60s"
      max_pending_todos_per_user: 100
      group_proposal_requires_mention: true
```

仅在启动机器人**同一个 PowerShell 会话**中设置密钥：

```powershell
$env:TODO_REMINDER_LLM_API_KEY = "<YOUR_LLM_API_KEY>"
& .\.venv\Scripts\ncatbot.exe run
```

也可用 `.env.example` 创建本地 `.env` 供你的进程管理器读取，但 NcatBot 本身不会自动加载 `.env`。`plugins/todo_reminder/config.yaml` 是低优先级源码默认值；应把个人选择写入 `plugin.plugin_configs.todo_reminder`，密钥始终只放环境变量。

## 操作与最小验收

私聊机器人后发送：

```text
明天上午十点提醒我提交周报
```

预期：插件给出待办候选和确认提示；回复 `确认`（或单候选时的“好/是”）后，插件创建待办并显示结果。随后发送：

```text
查看待办
```

预期输出含刚创建的待办编号。其他常用操作：

```text
完成第2条
取消第2条
把第2条推迟10分钟
猫娘模式
简洁模式
```

在群里，群主或管理员先发送 `#待办 开启`。默认需要 `@机器人` 才会进入可能需要追问的候选流程；已有候选的同一用户可继续回复。所有到点提醒均私聊创建者，不会在群内广播。

## 数据、更新、卸载与排障

- 数据库：`<NCATBOT_HOME>/data/todo_reminder/todos.sqlite`；待确认候选也保存在同一数据库。
- 更新前停止机器人并备份该 SQLite；在插件目录执行 `git pull --ff-only`，重启后检查迁移和日志。
- 卸载：先 `ncatbot plugin disable todo_reminder`，备份数据库后删除插件目录。删除目录会使旧数据不可用。
- “还没有配置 LLM”：检查 `llm_api_base`、`llm_model`、`llm_api_key_env`，并确认当前启动进程确实拥有该环境变量。
- 候选无法确认：确认在同一私聊/发起群会话中回复，并在候选过期前完成；群聊还需允许机器人私聊你。
- 提醒未发送：检查机器人能否私聊创建者、`timezone`、到期时间及 `logs/`；不要公开 API Key、数据库或完整聊天日志。
