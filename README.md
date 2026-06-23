# YouTube → Bilibili 自动转载流水线

一个用于个人自动化的 YouTube → B站转载工具。它可以下载 YouTube 视频和缩略图，翻译标题，生成 1920×1080 封面，并以转载类型上传到 Bilibili。也支持轮询 YouTube 订阅频道，自动处理新视频。

> 请遵守 YouTube、Bilibili 以及原作者的版权和转载要求。本项目只提供自动化工具，不授予任何内容转载权利。

## 功能

- 下载 YouTube 视频，默认最高 1080p，输出 MP4
- 获取 YouTube 标题、简介、视频 ID 和缩略图
- 使用 DeepSeek、OpenAI 兼容接口或 Google 翻译标题
- **内容筛选**：可选在下前用 DeepSeek 判断视频是否与指定主题相关，过滤无关内容
- 自动移除标题末尾的 YouTube hashtag，并可保留指定术语
- 将缩略图校验并等比裁剪或填充为 1920×1080 JPEG 封面
- 上传到 Bilibili，投稿类型为转载，简介保留原视频链接
- 批量处理时单条失败不影响后续，并写入 `runs/latest.json`
- 上传成功后可清理本地视频、缩略图和生成封面
- 可通过 `main.py --monitor` 定时检查 YouTube 订阅更新并自动上传
- 支持代理、YouTube Cookie、低速重启和超长视频排队
- 多层次自动重试：API 调用、监控周期、单视频处理均支持指数退避重试
- 超长视频（>10h）自动使用 ffmpeg 无损分割为分 P 上传
- **竖屏过滤**：自动跳过 YouTube Shorts 等竖屏视频（可配置）
- **超长视频永久跳过**：超过指定时长的视频直接标记跳过，不再排队
- 支持 PyInstaller 打包为单个 Windows EXE，无需安装 Python
- 启动时自动检测 ffmpeg / ffprobe / Node.js 可用性并给出明确提示

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
| `SOURCE_LANG` | 源语言，`auto` 表示自动检测 | `auto` |
| `DEFAULT_TID` | B站分区 ID | `172` |
| `DEFAULT_TAGS` | B站标签，逗号分隔 | `转载,YouTube` |
| `DOWNLOAD_DIR` | 下载目录 | `./downloads` |
| `MAX_HEIGHT` | 下载视频最高画质 | `1080` |
| `MAX_VIDEO_DURATION_SECONDS` | 触发视频分割的时长阈值（秒），超过则分 P 上传 | `36000`（10 小时） |
| `COVER_FIT` | 封面适配模式：`crop`=居中裁剪，`contain`=等比缩放加黑边 | `crop` |
| `YOUTUBE_SKIP_LONG_VIDEO_MINUTES` | 永久跳过超过该时长的视频（分钟），`0`=禁用 | `0` |
| `YOUTUBE_SKIP_VERTICAL_VIDEOS` | 自动跳过竖屏视频（YouTube Shorts 等） | `true` |
| `CONTENT_FILTER_ENABLED` | 开启 DeepSeek 内容筛选 | `false` |
| `CONTENT_FILTER_KEYWORDS` | 内容筛选关键词，视频标题/简介需包含 | `Marvel SNAP` |
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

网络重试配置：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `YOUTUBE_API_MAX_RETRIES` | YouTube API 单次请求的最大重试次数 | `3` |
| `YOUTUBE_API_RETRY_DELAY` | API 重试基础延迟秒数（指数退避：delay × 2^n） | `2` |
| `YOUTUBE_MONITOR_MAX_RETRIES` | 监控周期连续网络失败多少次后等待一个完整间隔 | `5` |
| `YOUTUBE_MONITOR_RETRY_DELAY` | 监控周期重试基础延迟秒数（上限 600s） | `30` |
| `YOUTUBE_VIDEO_RETRY_MAX` | 单视频处理失败后的额外重试次数（仅 download/split/upload 阶段） | `2` |
| `YOUTUBE_VIDEO_RETRY_DELAY` | 单视频重试基础延迟秒数 | `30` |

## 视频分割与分 P 上传

Bilibili 限制单个视频时长不超过 10 小时。超过此限制的视频会自动使用 ffmpeg 分割为多个片段，以分 P（多部分）形式上传：

- 使用 `ffmpeg -c copy` 在关键帧处无损分割，无需重新编码，速度快且不损失画质
- 分割后的片段按 `_P001`、`_P002`... 命名
- 上传时自动检测分 P 数量并在完成信息中标注
- 可通过 `MAX_VIDEO_DURATION_SECONDS` 调整分割阈值（默认 `36000` 秒）

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

常用命令行选项：

```bash
# 每 30 分钟检查一次
python main.py --monitor --monitor-interval 1800

# 只检查一次（调试用）
python main.py --monitor --once

# 仅打印待处理视频，不下载不上传
python main.py --monitor --once --dry-run

# 使用 RSS 模式（不消耗 API 配额）
python main.py --monitor --monitor-source rss

# 禁用下载低速保护
python main.py --monitor --no-speed-protection
```

配置项：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `YOUTUBE_MONITOR_INTERVAL_SECONDS` | 订阅自动轮询间隔秒数 | `3600` |
| `YOUTUBE_MONITOR_SOURCE` | 订阅来源，`api` 或 `rss` | `api` |
| `YOUTUBE_MONITOR_LIMIT` | 每轮读取的订阅视频数量 | `50` |
| `YOUTUBE_MONITOR_STATE` | 已处理视频状态文件 | `state/processed_videos.json` |
| `YOUTUBE_DEFER_LONG_VIDEO_MINUTES` | 直播回放或超长视频排到队尾的时长阈值，`0` 表示关闭 | `60` |
| `YOUTUBE_SKIP_LONG_VIDEO_MINUTES` | 永久跳过超过该时长的视频（分钟），`0`=禁用（仅 API 模式） | `0` |
| `YOUTUBE_SKIP_VERTICAL_VIDEOS` | 自动跳过竖屏/Shorts 视频 | `true` |
| `CONTENT_FILTER_ENABLED` | 开启 DeepSeek 内容筛选，下载前过滤无关视频 | `false` |
| `CONTENT_FILTER_KEYWORDS` | 内容筛选关键词 | `Marvel SNAP` |
| `YOUTUBE_MAX_VIDEOS_PER_CHANNEL` | 每个订阅频道抓取最近多少条视频 | `5` |

自动轮询策略：

- 每轮处理全部未上传的新视频
- 上传或下载失败的视频会在 **同一周期内** 自动重试（最多 `YOUTUBE_VIDEO_RETRY_MAX` 次，仅 download/split/upload 阶段）
- 重试采用指数退避：30s → 60s → 120s...，避免频繁请求
- 若整个周期的 API/网络请求连续失败，监控循环也会自动重试，不会直接退出
- **永久跳过规则（不重试、不排队）**：
  - 直播、预约直播和直播处理中内容
  - 竖屏视频（`YOUTUBE_SKIP_VERTICAL_VIDEOS=true`）
  - 超长视频（`YOUTUBE_SKIP_LONG_VIDEO_MINUTES > 0`）
  - 内容筛选不相关（`CONTENT_FILTER_ENABLED=true`）
- 直播回放或时长达到 `YOUTUBE_DEFER_LONG_VIDEO_MINUTES` 的视频会排到队尾，先上传普通视频
- 内容筛选（可选）：下载前用 DeepSeek 判断标题+简介是否与 `CONTENT_FILTER_KEYWORDS` 相关，无关则永久跳过
- 处理历史写入 `state/processed_videos.json`，用 YouTube `video_id` 去重

## YouTube 订阅列表脚本

项目提供独立脚本 `youtube_subscriptions.py`，用于获取 YouTube 订阅频道的最新视频列表，输出标题、频道名、发布时间和视频链接。

默认使用 YouTube Data API + OAuth：

```bash
python youtube_subscriptions.py --source api --limit 50
python -m yt2bili.youtube.subscriptions --source api --limit 50
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

仓库中提供 `examples/channels.example.txt` 作为频道列表格式示例。

配额说明：

- `subscriptions.list`、`channels.list`、`playlistItems.list` 官方配额成本都是 1 unit/请求
- YouTube Data API 默认项目配额是每天 10,000 units
- 脚本避免使用更贵的 `search.list`
- RSS 模式不消耗 YouTube Data API 配额，但不能自动读取订阅频道，需要 `subscriptions_cache.json` 或频道列表文件

## urls.txt 格式

实际运行文件名为 `urls.txt`，该文件会被 `.gitignore` 忽略。仓库中提供 `examples/urls.example.txt` 作为示例：

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

## 启动工具检查

每次启动时，程序会自动检测以下外部工具的可用性：

```
[工具] ffmpeg: OK
[工具] ffprobe: OK
[工具] node: OK
```

缺少工具时会显示 `未找到` 及影响说明，但**不会阻止程序运行**——相关功能会优雅降级：

| 工具 | 缺失影响 |
| --- | --- |
| ffmpeg | 视频分割不可用 |
| ffprobe | 分辨率/时长探测不可用 |
| Node.js | yt-dlp 将使用内置 JS 引擎（可能较慢） |

## EXE 打包（Windows）

项目支持通过 PyInstaller 打包为单个 `.exe` 文件，无需安装 Python 即可运行：

```batch
tools\build_exe.bat
pyinstaller packaging\yt2bili.spec
```

输出：`dist\yt2bili.exe`（约 150–200 MB）

**打包前准备：**

1. 安装 PyInstaller：`pip install pyinstaller`
2. （可选）安装 [UPX](https://upx.github.io/) 并加入 PATH，可压缩约 40% 体积
3. 确保 `ffmpeg` / `ffprobe` 已安装并加入系统 PATH（EXE 运行时仍需它们）

**打包后的目录结构：**

```
D:\yt2bili\
  yt2bili.exe          ← 主程序
  .env                 ← 配置文件（从 .env.example 复制并填写）
  client_secret.json   ← YouTube OAuth（使用 API 监控模式时需要）
  downloads/           ← 下载目录（自动创建）
  runs/                ← 运行记录（自动创建）
  state/               ← 监控状态（自动创建）
```

**与开发模式的区别：**

- 所有用户数据（`.env`、下载、记录）保存在 EXE 所在目录，而非解压临时目录
- 通过 `yt2bili/frozen_paths.py` 自动检测运行环境，无需手动配置路径
- 命令行用法与 `python main.py` 完全一致：`yt2bili.exe --monitor`

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
