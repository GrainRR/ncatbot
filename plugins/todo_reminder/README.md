# todo_reminder

## 基本介绍

通过 OpenAI 兼容的 LLM 理解自然语言待办，生成待确认候选，并在到点后私聊提醒创建者。默认仅在私聊中使用待办。

## 获取方式

本插件仅随本项目 Release 分发。请下载与项目版本匹配的发布包，并按发布包附带的部署说明安装；无需单独克隆插件仓库。

## 前置依赖

- 运行环境：已完成 NcatBot、NapCat 和 QQ 初始化，且机器人能够私聊待办创建者以发送提醒。
- 软件包：无额外 Python 软件包依赖；待办数据使用 Python 自带的 SQLite。
- 其他插件：无。
- 外部服务与配置：必须具备支持 `chat/completions` 和工具调用的 OpenAI 兼容 LLM 服务、对应的 API Key 和模型名称；请在框架根目录的 `config.yaml` 添加以下插件配置。不要把真实 Key 写入 YAML：

```yaml
plugin:
  plugin_configs:
    todo_reminder:
      llm_api_base: "https://<LLM_PROVIDER_HOST>/v1"
      llm_api_key_env: "TODO_REMINDER_LLM_API_KEY"
      llm_model: "<LLM_MODEL_NAME>"
```

在启动机器人的同一个 PowerShell 会话中设置密钥并启动：

```powershell
$env:TODO_REMINDER_LLM_API_KEY = "<YOUR_LLM_API_KEY>"
& .\.venv\Scripts\ncatbot.exe run
```

启动日志没有 LLM 或数据库错误即表示配置完成。

## 使用方式

私聊机器人发送：

```text
明天上午十点提醒我提交周报
```

机器人给出候选后回复 `确认`（或单候选时回复“好”或“是”）来创建待办；发送 `查看待办` 查看结果。

群待办默认关闭。群主或管理员可发送 `#待办 开启` 或 `#待办 关闭`；群成员只能管理自己的待办。机器人还必须能私聊创建者，才能发送提醒。
