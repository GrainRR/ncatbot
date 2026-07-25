# bilibili_url_parser

版本：`1.0.1`。自动识别群聊和私聊中的 BV 号、AV 号、公开视频链接、`b23.tv` 短链及 QQ 分享消息，并回复视频基本信息。

## 前置条件

- 已完成 NcatBot、NapCat 与 QQ 初始化；NcatBot `5.5.4`、Python `>=3.12`。
- 无前置插件；机器人不需要群管理权限。
- Python 依赖：`bilibili-api-python==17.4.1`、`aiohttp==3.13.5`，必须安装到框架虚拟环境。
- 需要能够访问 Bilibili。公开视频解析不需要登录 Cookie；当前插件没有支持的 `SESSDATA` 配置项。

## 独立安装与加载验证

```powershell
$env:NCATBOT_HOME = "<NCATBOT_HOME>"
Set-Location $env:NCATBOT_HOME
git clone https://github.com/GrainRR/ncatbot-plugin-bilibili-url-parser.git ".\plugins\bilibili_url_parser"
& .\.venv\Scripts\python.exe -m pip install "bilibili-api-python==17.4.1" "aiohttp==3.13.5"
& .\.venv\Scripts\ncatbot.exe plugin info bilibili_url_parser
& .\.venv\Scripts\ncatbot.exe run
```

成功信号：`plugin info` 显示插件名、版本、入口类和两项 pip 依赖；启动日志显示解析器已启动。框架允许按 manifest 自动安装依赖，但上述显式命令可确保依赖由正确的 `.venv` 安装。

## 配置

本版本没有必填 `plugin.plugin_configs.bilibili_url_parser`、环境变量或密钥。不要向配置文件写入浏览器 Cookie、账号或 Token。

## 操作与最小验收

插件自动触发。向群聊或私聊发送一个可公开访问的视频链接，例如：

```text
https://www.bilibili.com/video/BV1xx411c7mD
```

也可只发送 `BV`/`av` 号或 `https://b23.tv/...` 短链。预期回复示例：

```text
标题: <视频标题>
UP主: <UP 主> (UID: <UID>)
BV号: <BV号>
av号: av<数字>
快捷链接: https://www.bilibili.com/video/<BV号>
```

请替换成真实、公开、可访问的视频；示例 BV 号仅说明格式。

## 数据、更新、卸载与排障

- 本插件不写入 SQLite 数据库；其运行信息在框架日志中。
- 更新前停止机器人，在插件目录执行 `git pull --ff-only`；然后再次执行虚拟环境 pip 安装命令以同步依赖。
- 卸载：`ncatbot plugin disable bilibili_url_parser` 后删除插件目录；可保留共享框架依赖供其他插件使用。
- 未加载：检查插件目录、`manifest.toml`、`ncatbot plugin info` 和 `python -m pip check`。
- 链接解析失败：检查网络、链接是否可公开访问、Bilibili API 限制和日志；不要在报错中提交 Cookie 或个人账号信息。
