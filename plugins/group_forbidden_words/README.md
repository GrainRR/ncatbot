# group_forbidden_words

版本：`0.1.0`。该插件检测群消息中的词库条目，命中后提示并尝试撤回原消息；群主和管理员可维护词库。

## 前置条件

- 已完成 NcatBot、NapCat 和 QQ 初始化；NcatBot `5.5.4`、Python `>=3.12`。
- 无前置插件和额外 Python 依赖。
- 机器人必须有群管理/撤回消息权限；词库管理命令仅允许群主或管理员发送。

## 独立安装与加载验证

```powershell
$env:NCATBOT_HOME = "<NCATBOT_HOME>"
Set-Location $env:NCATBOT_HOME
git clone https://github.com/GrainRR/ncatbot-plugin-group-forbidden-words.git ".\plugins\group_forbidden_words"
& .\.venv\Scripts\ncatbot.exe plugin info group_forbidden_words
& .\.venv\Scripts\ncatbot.exe run
```

成功信号：`plugin info` 显示版本 `0.1.0`，启动日志显示插件已加载。依赖、权限或撤回失败均应在日志中处理，不能只依据目录存在判断成功。

## 配置

无需 `plugin.plugin_configs.group_forbidden_words` 和环境变量。词库文件为插件目录下的 `forbidden_words.txt`，每行一个词。该文件会被管理命令改写；将它作为本地运营配置备份，避免把真实群运营词库提交到公开仓库。

## 操作与最小验收

群主或管理员可使用：

```text
#添加违禁词 <词>
#删除违禁词 <词>
#违禁词列表
```

完整验收示例：管理员发送 `#添加违禁词 __FORBIDDEN_TEST__`，普通成员随后发送 `__FORBIDDEN_TEST__`。预期：机器人提示命中并撤回该消息；若未撤回，优先检查机器人群管理权限。普通成员发送管理命令没有权限。

## 数据、更新、卸载与排障

- 数据/词库：`<NCATBOT_HOME>/plugins/group_forbidden_words/forbidden_words.txt`。
- 更新前备份词库；更新时停止机器人并运行 `git pull --ff-only`。
- 卸载前执行 `ncatbot plugin disable group_forbidden_words`，备份词库后删除插件目录。
- 插件未撤回消息：确认机器人群管理权限与 NapCat 删除消息接口可用。
- 管理命令无效：确认发送者是群主或管理员，检查词库文件 UTF-8 编码和启动日志。
