# group_special_title

版本：`0.1.0`。该插件允许群主为自己或被 @ 的群成员设置专属头衔，长度最多 6 个汉字或 12 个半角字符。

## 前置条件

- 已初始化 NcatBot、NapCat、QQ；NcatBot `5.5.4`、Python `>=3.12`。
- 无前置插件、额外 Python 依赖、环境变量或密钥。
- 机器人必须具备 QQ 设置群专属头衔所需的群管理权限；命令发送者必须是**群主**。当前实现不会把管理员当作头衔发放命令的授权者。

## 独立安装与加载验证

```powershell
$env:NCATBOT_HOME = "<NCATBOT_HOME>"
Set-Location $env:NCATBOT_HOME
git clone https://github.com/GrainRR/ncatbot-plugin-group-special-title.git ".\plugins\group_special_title"
& .\.venv\Scripts\ncatbot.exe plugin info group_special_title
& .\.venv\Scripts\ncatbot.exe run
```

成功信号：`plugin info` 显示 `group_special_title` `0.1.0`，启动日志没有导入或权限初始化错误。

## 配置

无需 `plugin.plugin_configs.group_special_title`。不要把 QQ 号、Token 或管理员名单写入插件源码。

## 操作与最小验收

只有群主可发送：

```text
#申请头衔 测试头衔
#发放头衔 @成员 测试头衔
```

完整验收：由群主先发送 `#申请头衔 测试`。预期机器人回复设置成功，QQ 群资料中显示新头衔。若需要为他人发放，必须使用 QQ 的真实 `@成员` 消息段，不能手工输入昵称。群管理员或普通成员发送相同命令应被拒绝。

## 数据、更新、卸载与排障

- 本插件不创建自己的数据库；头衔状态由 QQ 侧保存。
- 更新前停止机器人并在插件目录执行 `git pull --ff-only`；回退前记录可用 commit/tag。
- 卸载：先 `ncatbot plugin disable group_special_title`，再删除插件目录；不会自动清除已设置的 QQ 头衔。
- 设置失败：确认发送者是群主、机器人具备群管理权限、NapCat 连接正常；再查看 `logs/`。不要公开群号、成员 QQ 或 Token。
