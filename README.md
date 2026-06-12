# YouTube → Bilibili 自动转载流水线

一个用于个人自动化的 YouTube → B站转载工具。它可以下载 YouTube 视频和缩略图，翻译标题，生成 1920×1080 封面，并以转载类型上传到 Bilibili。也支持轮询 YouTube 订阅频道，自动处理新视频。

> 请遵守 YouTube、Bilibili 以及原作者的版权和转载要求。本项目只提供自动化工具，不授予任何内容转载权利。

## 功能

- 下载 YouTube 视频，默认最高 1080p，输出 MP4
- 获取 YouTube 标题、简介、视频 ID 和缩略图
- 使用 DeepSeek、OpenAI 兼容接口或 Google 翻译标题
- 自动移除标题末尾的 YouTube hashtag，并可保留指定术语
- 将缩略图校验并等比裁剪为 1920×1080 JPEG 封面
- 上传到 Bilibili，投稿类型为转载，简介保留原视频链接
- 批量处理时单条失败不影响后续，并写入 `runs/latest.json`
- 上传成功后可清理本地视频、缩略图和生成封面
- 可通过 `main.py --monitor` 定时检查 YouTube 订阅更新并自动上传
- 支持代理、YouTube Cookie、低速重启和超长视频排队

## Quick Start

### 1. 安装依赖

推荐使用 Python 3.12 或 Conda 环境：

```bash
conda create -n yt2bili python=3.12
conda activate yt2bili
pip install -r requirements.txt
```

还需要安装并配置可用的 `ffmpeg` / `ffprobe`。请确保它们能在命令行中直接运行：

```bash
ffmpeg -version
ffprobe -version
```

### 2. 创建配置文件

```bash
copy .env.example .env
```

Linux/macOS:

```bash
cp .env.example .env
```

`.env` 是本地私密配置文件，不要提交到 Git。

### 3. 配置翻译

默认使用 DeepSeek：

```env
TRANSLATE_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_THINKING=disabled
```

也可以使用 OpenAI 或兼容端点：

```env
TRANSLATE_PROVIDER=openai
OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_MODEL=gpt-4o-mini
```

### 4. 登录 Bilibili

首次运行会弹出 B站登录二维码，使用 Bilibili App 扫码即可。程序会把登录凭据写入 `.env`：

```bash
python main.py --login
```

### 5. 运行

单个视频：

```bash
python main.py "https://www.youtube.com/watch?v=xxxxx"
```

批量处理：

```bash
python main.py --file urls.txt
```

订阅轮询：

```bash
python main.py --monitor
```

调试订阅轮询，只检查一次且不下载上传：

```bash
python main.py --monitor --once --dry-run
```

## 配置说明

常用配置项：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `TRANSLATE_PROVIDER` | `deepseek`、`openai` 或 `google` | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 空 |
| `DEEPSEEK_MODEL` | DeepSeek 模型 | `deepseek-v4-flash` |
| `DEEPSEEK_THINKING` | DeepSeek thinking mode，标题翻译建议关闭 | `disabled` |
| `OPENAI_API_KEY` | OpenAI API Key，仅 `openai` 模式需要 | 空 |
| `TRANSLATION_PRESERVE_TERMS` | 翻译时原样保留的术语，逗号分隔 | 空 |
| `TRANSLATION_EXTRA_PROMPT` | 追加给 AI 翻译器的额外要求 | 空 |
| `TRANSLATION_PROXY` | 单独指定翻译代理，留空时复用 `YOUTUBE_PROXY` | 空 |
| `DEFAULT_TID` | B站分区 ID | `172` |
| `DEFAULT_TAGS` | B站标签，逗号分隔 | `转载,YouTube` |
| `DOWNLOAD_DIR` | 下载目录 | `./downloads` |
| `MAX_HEIGHT` | 下载视频最高画质 | `1080` |
| `CLEANUP_AFTER_UPLOAD` | 上传成功后清理本地文件 | `true` |
| `RUNS_DIR` | 批量结果记录目录 | `./runs` |
| `YOUTUBE_PROXY` | YouTube API 和默认下载代理 | 空 |
| `DOWNLOAD_PROXY` | 单独指定下载代理，留空时复用 `YOUTUBE_PROXY` | 空 |
| `YOUTUBE_HTTP_TIMEOUT` | YouTube API、元数据和缩略图请求超时秒数 | `60` |
| `YOUTUBE_COOKIES_FROM_BROWSER` | yt-dlp 读取 YouTube 登录 Cookie 的浏览器列表；可写 `chrome:Profile 1` 指定个人资料 | `chrome,edge,firefox` |
| `YOUTUBE_COOKIE_FILE` | 自动生成/使用的 Netscape `cookies.txt` 路径，优先于浏览器 Cookie | `cookies.txt` |
| `YTDLP_REMOTE_COMPONENTS` | yt-dlp 允许下载的 YouTube JS challenge 解算组件 | `ejs:github` |

下载稳定性配置：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `DOWNLOAD_MIN_SPEED_KIB` | 下载低速保护阈值，`0` 表示关闭 | `100` |
| `DOWNLOAD_SLOW_SECONDS` | 连续低于阈值多少秒后重启下载 | `60` |
| `DOWNLOAD_SLOW_GRACE_SECONDS` | 下载开始后的低速检测宽限秒数 | `30` |
| `DOWNLOAD_MAX_RESTARTS` | 单个视频因低速自动重启的最大次数 | `3` |
| `DOWNLOAD_STARTUP_STATUS_SECONDS` | 下载开始传输前，每隔多少秒提示解析/等待状态 | `30` |

封面配置：

```env
COVER_WIDTH=1920
COVER_HEIGHT=1080
COVER_FIT=crop
```

B站常用游戏分区 ID：

| 分区 | ID |
| --- | --- |
| 游戏-手机游戏 | `172` |
| 游戏-单机游戏 | `17` |
| 游戏-网络游戏 | `65` |
| 游戏-电子竞技 | `171` |
| 游戏-桌游棋牌 | `173` |

注意：`28` 是 `音乐-原创音乐`，不是游戏区。

## YouTube Cookie 与代理

YouTube 有时会要求登录或验证不是机器人。程序会优先使用 `YOUTUBE_COOKIE_FILE` 指向的 `cookies.txt`。如果文件不存在，会按 `YOUTUBE_COOKIES_FROM_BROWSER` 自动尝试从浏览器导出 YouTube/Google Cookie 并保存。

也可以手动刷新 Cookie：

```bash
python main.py --refresh-youtube-cookies
```

如果读取浏览器 Cookie 失败，请确认：

- 浏览器中已经登录 YouTube
- 正在使用的浏览器个人资料窗口已经关闭
- Python/PyCharm 和浏览器使用同一 Windows 用户和权限级别
- 如登录在非默认个人资料，设置例如 `YOUTUBE_COOKIES_FROM_BROWSER=chrome:Profile 1,firefox`

如需代理：

```env
YOUTUBE_PROXY=http://127.0.0.1:7897
DOWNLOAD_PROXY=http://127.0.0.1:7897
TRANSLATION_PROXY=http://127.0.0.1:7897
```

`YTDLP_REMOTE_COMPONENTS=ejs:github` 用于允许 yt-dlp 获取 YouTube JS challenge 解算组件，避免只拿到 storyboard 而拿不到视频流。首次使用可能需要能访问 GitHub。

## 订阅自动上传

`main.py --monitor` 会定时检查 YouTube 订阅更新，处理未上传的新视频：

```bash
python main.py --monitor
```

配置项：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `YOUTUBE_MONITOR_INTERVAL_SECONDS` | 订阅自动轮询间隔秒数 | `3600` |
| `YOUTUBE_MONITOR_SOURCE` | 订阅来源，`api` 或 `rss` | `api` |
| `YOUTUBE_MONITOR_LIMIT` | 每轮读取的订阅视频数量 | `50` |
| `YOUTUBE_MONITOR_STATE` | 已处理视频状态文件 | `state/processed_videos.json` |
| `YOUTUBE_DEFER_LONG_VIDEO_MINUTES` | 直播回放或超长视频排到队尾的时长阈值，`0` 表示关闭 | `60` |

自动轮询策略：

- 每轮处理全部未上传的新视频
- 上传或下载失败的视频会在下一轮继续重试
- 直播、预约直播和直播处理中内容会被永久跳过
- 直播回放或时长达到阈值的视频会排到队尾，先上传普通视频
- 处理历史写入 `state/processed_videos.json`，用 YouTube `video_id` 去重

## YouTube 订阅列表脚本

项目提供独立脚本 `youtube_subscriptions.py`，用于获取 YouTube 订阅频道的最新视频列表，输出标题、频道名、发布时间和视频链接。

默认使用 YouTube Data API + OAuth：

```bash
python youtube_subscriptions.py --source api --limit 50
```

首次使用前需要配置 Google OAuth：

1. 在 Google Cloud Console 创建或选择项目。
2. 启用 YouTube Data API v3。
3. 创建 OAuth Desktop App 凭据。
4. 下载客户端 JSON，保存为 `client_secret.json`。
5. 运行脚本，浏览器会打开授权页面；授权后会生成 `youtube_token.json`。

RSS 低配额模式：

```bash
python youtube_subscriptions.py --source rss --limit 50
python youtube_subscriptions.py --source rss --channels-file channels.txt --limit 50
```

配额说明：

- `subscriptions.list`、`channels.list`、`playlistItems.list` 官方配额成本都是 1 unit/请求
- YouTube Data API 默认项目配额是每天 10,000 units
- 脚本避免使用更贵的 `search.list`
- RSS 模式不消耗 YouTube Data API 配额，但不能自动读取订阅频道，需要 `subscriptions_cache.json` 或频道列表文件

## urls.txt 格式

实际运行文件名为 `urls.txt`，该文件会被 `.gitignore` 忽略。仓库中提供 `urls.example.txt` 作为示例：

```txt
# 空行和注释会被忽略
https://www.youtube.com/watch?v=xxxxx
https://youtu.be/yyyyy
```

## 运行结果

每次运行结束会生成：

- `runs/latest.json`
- `runs/YYYYMMDD-HHMMSS.json`

记录内容包括每个 URL 的成功状态、失败阶段、错误信息、BV 号、原标题、翻译标题、视频路径、缩略图路径和封面路径。

## 不要提交的本地文件

以下文件包含凭据、Cookie、本地历史或下载内容，已经在 `.gitignore` 中忽略：

- `.env`
- `cookies.txt`
- `client_secret.json`
- `youtube_token.json`
- `subscriptions_cache.json`
- `urls.txt`
- `channels.txt`
- `downloads/`
- `runs/`
- `state/`
- `.idea/`

如果这些文件已经被 Git 跟踪，请先从索引中移除：

```bash
git rm --cached .env cookies.txt client_secret.json youtube_token.json subscriptions_cache.json
git rm --cached -r downloads runs state .idea
```

## Marvel SNAP 示例配置

如果转载 Marvel SNAP 相关内容，可以在 `.env` 中使用类似配置：

```env
DEFAULT_TID=172
DEFAULT_TAGS=SNAP,MARVEL SNAP
TRANSLATION_PRESERVE_TERMS=Marvel SNAP,SNAP
TRANSLATION_EXTRA_PROMPT=游戏名 SNAP 和 Marvel SNAP 保持英文；标题自然适合 B站观众
```

## 工作流程

```text
YouTube URL
  -> yt-dlp 获取元数据和下载视频/缩略图
  -> DeepSeek/OpenAI/Google 翻译标题
  -> Pillow 校验并生成 1920x1080 封面
  -> bilibili-api-python 上传为转载
  -> 写入 runs 结果记录
```

## 常见问题

**DeepSeek 返回空翻译怎么办？**

确认 `.env` 中 `DEEPSEEK_THINKING=disabled`。DeepSeek V4 默认会开启 thinking mode，短标题翻译可能把输出 token 用在 reasoning 上，导致最终 `content` 为空或被截断。

**封面处理失败怎么办？**

程序会跳过 0 字节或损坏缩略图。如果 YouTube 没有可用缩略图，该条 URL 会在 `cover` 阶段失败，文件会保留在本地用于排查。

**批量中某个视频失败会停止吗？**

不会。失败会记录到 `runs/latest.json`，程序继续处理下一条链接。

**分区投错怎么办？**

检查 `.env` 中的 `DEFAULT_TID`。例如 `172` 是游戏-手机游戏，`28` 是音乐-原创音乐。

## License

MIT License. See [LICENSE](LICENSE).
