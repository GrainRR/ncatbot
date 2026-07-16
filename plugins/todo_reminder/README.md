# todo_reminder

基于 LLM 解析自然语言的待办提醒插件。

## 功能

- 群待办默认关闭；群主或管理员可使用 `#待办 开启` / `#待办 关闭` 切换，开启后群成员可以管理自己的待办。
- 私聊直接发送自然语言待办，到点私聊提醒创建人。
- 一句话中明确包含多个独立活动时，可以一次创建多条待办。
- 待办可以不设置提醒时间，这类待办只会进入列表，不会定时提醒。
- 使用 SQLite 保存待办数据：`data/todo_reminder/todos.sqlite`。
- 私聊和已开启群聊中的待办按用户统一归属，共用同一列表、编号和管理范围；不同用户互相隔离。
- 删除采用软删除；完成、删除、提醒成功后的待办不会再提醒。无论待办来自哪里，到期提醒都只发送到创建人的私聊，私聊发送成功后才标记为已提醒。

## 用法

```text
明天十点提醒我开会
查看待办
完成第二条
取消第二条
把第二条推迟10分钟
猫娘模式
简洁模式
```

列表中会展示形如 `[3]` 的待办序号。这个序号按 `user_id` 分配，只由当前未完成待办占用；完成、删除或提醒后软删除的待办不会继续占用序号。完成、取消、修改、推迟等操作都使用当前用户的待办序号，因此可在私聊和已开启群聊之间继续管理同一条待办。

已完成和已取消记录会额外显示不可复用的历史 ID（`H-...`）。恢复和永久删除必须指定这个历史 ID，避免同一序号被复用后误操作历史记录。永久删除分两步：第一次只生成 5 分钟有效的确认令牌，第二次须携带同一历史 ID 与令牌；直接传入 `confirmed=true` 不会删除数据。

如果 LLM 没有识别到明确提醒时间，会创建普通待办并显示“提醒时间：未设置”。待办提醒成功发送后会自动软删除，从未完成列表中消失。

如果用户输入包含“先 A 后 B”“A，然后 B”这类明确的多个活动，LLM 会尽可能少地拆分为多条独立待办。没有说明多个活动间隔多久时，提示词要求 LLM 默认按 10 分钟间隔写入各条待办的提醒时间。

## LLM 配置

插件调用 OpenAI 兼容的 `chat/completions` 接口。建议在根 `config.yaml` 的 `plugin.plugin_configs` 中覆盖配置：

```yaml
plugin:
  plugin_configs:
    todo_reminder:
      llm_api_base: "https://api.openai.com/v1"
      llm_api_key: "你的 API Key"
      llm_model: "你的模型名"
```

也可以不把密钥写入配置文件，而是设置环境变量：

```yaml
plugin:
  plugin_configs:
    todo_reminder:
      llm_api_base: "https://api.openai.com/v1"
      llm_api_key_env: "TODO_REMINDER_LLM_API_KEY"
      max_pending_todos_per_user: 100
      llm_model: "你的模型名"
```

如果服务地址不是标准 `/chat/completions`，可以直接设置完整地址：

```yaml
llm_api_url: "https://example.com/v1/chat/completions"
```
