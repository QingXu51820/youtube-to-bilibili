# 配置参考

本项目所有配置通过 `config/.env` 文件设置（复制自 `config/.env.example`）。

---

## 常用配置项

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
| `YOUTUBE_COOKIE_FILE` | 自动生成/使用的 Netscape `config/cookies.txt` 路径，优先于浏览器 Cookie | `config/cookies.txt` |
| `YTDLP_REMOTE_COMPONENTS` | yt-dlp 允许下载的 YouTube JS challenge 解算组件 | `ejs:github` |

## 下载稳定性配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `DOWNLOAD_MIN_SPEED_KIB` | 下载低速保护阈值，`0` 表示关闭 | `100` |
| `DOWNLOAD_SLOW_SECONDS` | 连续低于阈值多少秒后重启下载 | `60` |
| `DOWNLOAD_SLOW_GRACE_SECONDS` | 下载开始后的低速检测宽限秒数 | `30` |
| `DOWNLOAD_MAX_RESTARTS` | 单个视频因低速自动重启的最大次数 | `3` |
| `DOWNLOAD_STARTUP_STATUS_SECONDS` | 下载开始传输前，每隔多少秒提示解析/等待状态 | `30` |

## 网络重试配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `YOUTUBE_API_MAX_RETRIES` | YouTube API 单次请求的最大重试次数 | `3` |
| `YOUTUBE_API_RETRY_DELAY` | API 重试基础延迟秒数（指数退避：delay × 2^n） | `2` |
| `YOUTUBE_MONITOR_MAX_RETRIES` | 监控周期连续网络失败多少次后等待一个完整间隔 | `5` |
| `YOUTUBE_MONITOR_RETRY_DELAY` | 监控周期重试基础延迟秒数（上限 600s） | `30` |
| `YOUTUBE_VIDEO_RETRY_MAX` | 单视频处理失败后的额外重试次数（仅 download/split/upload 阶段） | `2` |
| `YOUTUBE_VIDEO_RETRY_DELAY` | 单视频重试基础延迟秒数 | `30` |

---

## 视频分割与分 P 上传

Bilibili 限制单个视频时长不超过 10 小时。超过此限制的视频会自动使用 ffmpeg 分割为多个片段，以分 P（多部分）形式上传：

- 使用 `ffmpeg -c copy` 在关键帧处无损分割，无需重新编码，速度快且不损失画质
- 分割后的片段按 `_P001`、`_P002`... 命名
- 上传时自动检测分 P 数量并在完成信息中标注
- 可通过 `MAX_VIDEO_DURATION_SECONDS` 调整分割阈值（默认 `36000` 秒）

---

## 封面配置

```env
COVER_WIDTH=1920
COVER_HEIGHT=1080
COVER_FIT=crop
```

---

## B站常用游戏分区 ID

| 分区 | ID |
| --- | --- |
| 游戏-手机游戏 | `172` |
| 游戏-单机游戏 | `17` |
| 游戏-网络游戏 | `65` |
| 游戏-电子竞技 | `171` |
| 游戏-桌游棋牌 | `173` |

---

## YouTube Cookie 与代理

YouTube 有时会要求登录或验证不是机器人。程序会优先使用 `YOUTUBE_COOKIE_FILE` 指向的 `config/cookies.txt`。如果文件不存在，会按 `YOUTUBE_COOKIES_FROM_BROWSER` 自动尝试从浏览器导出 YouTube/Google Cookie 并保存。

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

---

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

### 自动轮询策略

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

---

## Marvel SNAP 示例配置

如果转载 Marvel SNAP 相关内容，可以在 `config/.env` 中使用类似配置：

```env
DEFAULT_TID=172
DEFAULT_TAGS=SNAP,MARVEL SNAP
TRANSLATION_PRESERVE_TERMS=Marvel SNAP,SNAP
TRANSLATION_EXTRA_PROMPT=游戏名 SNAP 和 Marvel SNAP 保持英文；标题自然适合 B站观众
```
