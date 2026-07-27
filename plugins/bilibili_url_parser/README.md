# bilibili_url_parser

## 基本介绍

自动识别群聊和私聊中的 Bilibili 视频链接、BV 号、AV 号和 `b23.tv` 短链，并回复视频基本信息。

## 获取方式

本插件仅随本项目 Release 分发。请下载与项目版本匹配的发布包，并按发布包附带的部署说明安装；无需单独克隆插件仓库，也无需手动安装本插件的 Python 依赖。

## 前置依赖

- 运行环境：已完成 NcatBot、NapCat 和 QQ 初始化。
- 软件包：需要 `bilibili-api-python==17.4.1` 和 `aiohttp==3.13.5`；发布包的部署流程会根据 `manifest.toml` 处理，无需单独执行 `pip install`。
- 其他插件：无。
- 配置与权限：无需密钥、必填配置或群管理权限；机器人运行环境需要能够访问 Bilibili 服务。

## 使用方式

向群聊或私聊发送一个公开可访问的视频链接、BV/AV 号或 `b23.tv` 短链，例如：

```text
https://www.bilibili.com/video/BV1xx411c7mD
```

机器人会自动回复视频标题、UP 主、BV/AV 号和链接。
