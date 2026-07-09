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
- **多账号管理**：支持多个 Bilibili 账号轮询不同频道列表，独立记录状态
- **字幕下载与翻译**：自动下载 YouTube 字幕，通过 DeepSeek 批量翻译为中文，上传为 B站软字幕
- **持久化去重**：`state/upload_log.json` 永久记录上传历史，状态丢失后可恢复
- **并行处理**：字幕翻译多线程并行，字幕处理与视频上传同时进行
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
copy config\.env.example config\.env
```

Linux/macOS:

```bash
cp config/.env.example config/.env
```

`config/.env` 是本地私密配置文件，不要提交到 Git。

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

首次运行会弹出 B站登录二维码，使用 Bilibili App 扫码即可。程序会把登录凭据写入 `config/.env`：

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

## 多账号管理

创建 `config/profiles.json`（参考 `config/profiles.json.example`）配置多个 Bilibili 账号，每个账号绑定独立的 YouTube 频道列表和设置：

```bash
# 列出所有配置的账号
python main.py --list-profiles

# 为指定账号执行扫码登录
python main.py --login --profile snap
python main.py --login --profile deadlock

# 用指定账号处理视频
python main.py --profile snap "https://youtube.com/watch?v=xxx"

# 监控指定账号的频道
python main.py --monitor --profile snap

# 轮询所有配置账号（无间隔等待）
python main.py --monitor --all-profiles --once

# 持续轮询所有账号
python main.py --monitor --all-profiles
```

多账号模式下，每个账号的状态文件独立存储在 `state/<profile_name>/processed_videos.json`。

## 字幕功能

自动下载 YouTube 视频的英文字幕，通过 DeepSeek 批量翻译为中文，上传到 Bilibili 作为软字幕。

字幕处理与视频上传**并行进行**，不会增加总耗时。翻译支持多线程（默认 3 线程，可通过 `SUBTITLE_TRANSLATE_WORKERS` 调整）。

相关配置：

```env
SUBTITLE_ENABLED=true              # 启用字幕功能
SUBTITLE_SOURCE_LANGS=en.*,ja,ko   # 优先匹配的字幕语言
SUBTITLE_TARGET_LANG=zh-CN         # 目标翻译语言
SUBTITLE_TRANSLATE_BATCH_SIZE=80   # 每次 API 调用翻译条数
SUBTITLE_TRANSLATE_WORKERS=3       # 并行翻译线程数
SUBTITLE_UPLOAD_TO_BILIBILI=true   # 翻译后自动上传到 B站
```

## 内容筛选

可配置关键词过滤或 AI 分类器，筛选只与指定主题相关的视频：

```env
CONTENT_FILTER_ENABLED=true
CONTENT_FILTER_KEYWORDS=Marvel SNAP,SNAP,marvelsnap
```

工作流程：
1. 先用关键词匹配标题和简介 — 命中即放行
2. 未命中时调用 DeepSeek AI 分类 — 传入频道名作为上下文辅助判断
3. 被筛选掉的视频记录为 `skipped_content`，永久跳过

## 凭据状态检查

```bash
python main.py --check-auth
```

检查 Bilibili 登录凭据、YouTube OAuth Token、YouTube Cookie 的状态和过期时间，支持多账号。

## 配置

所有配置通过 `config/.env` 设置，详见 **[配置参考 →](docs/CONFIG.md)**

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
4. 下载客户端 JSON，保存为 `config/client_secret.json`。
5. 运行脚本，浏览器会打开授权页面；授权后会生成 `config/youtube_token.json`。

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
- RSS 模式不消耗 YouTube Data API 配额，但不能自动读取订阅频道，需要 `config/subscriptions_cache.json` 或频道列表文件

## urls.txt 格式

实际运行文件名为 `urls.txt`（位于 `config/` 目录下），该文件会被 `.gitignore` 忽略。仓库中提供 `examples/urls.example.txt` 作为示例：

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

- `config/.env`
- `config/cookies.txt`
- `config/client_secret.json`
- `config/youtube_token.json`
- `config/profiles.json`
- `config/subscriptions_cache.json`
- `config/urls.txt`
- `downloads/`
- `runs/`
- `state/`
- `.idea/`

如果这些文件已经被 Git 跟踪，请先从索引中移除：

```bash
git rm --cached config/.env config/cookies.txt config/client_secret.json config/youtube_token.json config/profiles.json config/subscriptions_cache.json
git rm --cached -r downloads runs state .idea
```

## 工作流程

```text
YouTube URL
  -> yt-dlp 获取元数据和下载视频/缩略图
  -> DeepSeek/OpenAI/Google 翻译标题
  -> Pillow 校验并生成 1920x1080 封面
  -> bilibili-api-python 上传为转载
  -> 写入 runs 结果记录
  -> [并行] 下载字幕 -> DeepSeek 批量翻译 -> B站软字幕上传
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

## 常见问题

**DeepSeek 返回空翻译怎么办？**

确认 `config/.env` 中 `DEEPSEEK_THINKING=disabled`。DeepSeek V4 默认会开启 thinking mode，短标题翻译可能把输出 token 用在 reasoning 上，导致最终 `content` 为空或被截断。

**封面处理失败怎么办？**

程序会跳过 0 字节或损坏缩略图。如果 YouTube 没有可用缩略图，该条 URL 会在 `cover` 阶段失败，文件会保留在本地用于排查。

**批量中某个视频失败会停止吗？**

不会。失败会记录到 `runs/latest.json`，程序继续处理下一条链接。

**分区投错怎么办？**

检查 `config/.env` 中的 `DEFAULT_TID`。例如 `172` 是游戏-手机游戏，`28` 是音乐-原创音乐。

**RSS 模式下超长视频不会跳过？**

RSS 模式无法提前获取视频时长，但下载器会在获取元数据后检查 `YOUTUBE_SKIP_LONG_VIDEO_MINUTES` 并拦截——设置后 RSS 和 API 模式均生效。

## License

MIT License. See [LICENSE](LICENSE).
