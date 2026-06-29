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
- `config/subscriptions_cache.json`
- `config/urls.txt`
- `downloads/`
- `runs/`
- `state/`
- `.idea/`

如果这些文件已经被 Git 跟踪，请先从索引中移除：

```bash
git rm --cached config/.env config/cookies.txt config/client_secret.json config/youtube_token.json config/subscriptions_cache.json
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

## License

MIT License. See [LICENSE](LICENSE).
