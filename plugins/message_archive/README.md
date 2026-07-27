# message_archive

## 基本介绍

将机器人收到的群聊和私聊消息写入 SQLite。它也是 `group_daily_report` 的消息数据来源。

## 获取方式

本插件仅随本项目 Release 分发。请下载与项目版本匹配的发布包，并按发布包附带的部署说明安装；无需单独克隆插件仓库。

## 前置依赖

- 运行环境：已完成 NcatBot、NapCat 和 QQ 初始化。
- 软件包：无额外 Python 软件包依赖；消息存储使用 Python 自带的 SQLite。
- 其他插件：无。
- 配置与权限：无必填配置、密钥或额外群管理权限。

## 使用方式

插件自动归档，不需要发送命令。向测试群或私聊发送一条普通消息，例如：

```text
归档验证消息
```

运行目录中出现 `data/message_archive/messages.sqlite` 即表示消息已写入。
