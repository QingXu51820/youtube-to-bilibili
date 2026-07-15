#!/usr/bin/env python3
"""
YouTube → Bilibili 自动转载流水线 / Discord → Bilibili 动态搬运

三种运行模式：
  单视频/批量模式      python main.py <url> [url ...]
  订阅监控模式          python main.py --monitor
  Discord 搬运模式      python main.py --discord

────────────────────────────────────────────────────────────────
  基础参数
────────────────────────────────────────────────────────────────

  url                        YouTube 视频链接，支持多个，空格分隔
  --file PATH                从文件批量读取 URL（每行一个，#开头为注释）
  --login                    重新扫码登录 B站，刷新凭据
  --refresh-youtube-cookies  从浏览器导出 YouTube Cookie（用于绕过反爬）

────────────────────────────────────────────────────────────────
  视频专用参数
────────────────────────────────────────────────────────────────

  --no-speed-protection      禁用下载低速保护（网络差时有用）

────────────────────────────────────────────────────────────────
  YouTube 订阅监控 (--monitor)
────────────────────────────────────────────────────────────────

  --monitor                  启动订阅监控，定时轮询 YouTube 订阅更新并自动搬运
  --once                     与 --monitor 配合，仅执行一轮检查后退出
  --dry-run                  与 --monitor 配合，只打印待处理视频，不下载上传
  --monitor-source {api|rss} 订阅数据源（默认读取 .env 配置）
  --monitor-limit N          每轮最多处理多少条视频（默认 50）
  --monitor-interval SECONDS 轮询间隔秒数（默认 3600 = 1小时）
  --monitor-state PATH       处理状态文件路径（默认 state/processed_videos.json）
  --max-videos-per-channel N 每个频道抓取最近 N 条视频（默认 5）

────────────────────────────────────────────────────────────────
  Discord 消息搬运 (--discord)
────────────────────────────────────────────────────────────────

  --discord                  启动 Discord 实时监听，搬运消息到 B站动态
                             需要 .env 中配置 DISCORD_BOT_TOKEN 和 DISCORD_CHANNEL_IDS

  Discord 所有配置项均在 .env 中设置（无命令行参数）：
    DISCORD_BOT_TOKEN         Bot 令牌（必填）
    DISCORD_CHANNEL_IDS       监听频道 ID，逗号分隔（必填）
    DISCORD_SKIP_BOTS         跳过其他 Bot 的消息（默认 true）
    DISCORD_SKIP_EMPTY        跳过无文本无附件的空消息（默认 true）
    DISCORD_MAX_IMAGES        每条动态最多几张图（默认 9）
    DISCORD_TRANSLATE         是否翻译为中文（默认 true）
    DISCORD_STATE_FILE        已处理消息记录（默认 state/discord_messages.json）
    DISCORD_FALLBACK_LIMIT    启动时回溯最近 N 条消息（默认 20）

────────────────────────────────────────────────────────────────
  示例
────────────────────────────────────────────────────────────────

  # 单个视频
  python main.py https://www.youtube.com/watch?v=xxxxx

  # 批量处理
  python main.py --file urls.txt

  # YouTube 订阅监控（持续运行）
  python main.py --monitor

  # 只检查一次有什么新视频
  python main.py --monitor --once --dry-run

  # 订阅监控 + 无速度限制
  python main.py --monitor --no-speed-protection

  # Discord 实时搬运
  python main.py --discord

  # 重新登录 B站
  python main.py --login

  # 刷新 YouTube Cookie
  python main.py --refresh-youtube-cookies

────────────────────────────────────────────────────────────────
  流水线步骤
────────────────────────────────────────────────────────────────

  视频: 下载 → 分割(>10h) → 翻译标题 → 生成封面 → 上传B站 → 清理
  Discord: 监听消息 → 英文卡牌名替换 → 翻译文本 → 发布B站动态

  Marvel SNAP 术语表（自动保护卡牌/地形官方中文名）：
    SNAP_GLOSSARY_ENABLED=true  启用术语表（默认）
    SNAP_GLOSSARY_CACHE         缓存文件（默认 data/snap_glossary.json）
    SNAP_GLOSSARY_TTL           刷新间隔秒数（默认 86400 = 1天）
"""

import argparse
import gc
import sys
import os
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yt2bili import config
from yt2bili import profile as profile_mod
from yt2bili.config import validate
from yt2bili.media.cover import image_size, prepare_cover
from yt2bili.youtube.downloader import download_video
from yt2bili.translation.translator import translate
from yt2bili.bilibili.uploader import upload_video
from yt2bili.media.video_splitter import split_video
from yt2bili.bilibili import auth
from yt2bili.subtitles.downloader import download_subtitles
from yt2bili.subtitles.parser import parse_subtitle
from yt2bili.subtitles.translator import translate_cues
from yt2bili.subtitles.writer import write_srt
from yt2bili.bilibili.subtitle import save_pending_subtitle


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class ProcessResult:
    """Per-URL pipeline result for batch reports."""
    url: str
    success: bool = False
    stage: str = "pending"
    error: str = ""
    bvid: str = ""
    aid: int = 0
    original_title: str = ""
    translated_title: str = ""
    video_path: str = ""
    thumbnail_path: str = ""
    cover_path: str = ""
    video_resolution: str = ""
    # Subtitle fields
    subtitle_source_path: str = ""
    subtitle_translated_path: str = ""
    subtitle_status: str = ""       # success | skipped_multi_part | skipped_disabled | cid_timeout | upload_failed | failed | no_source
    subtitle_error: str = ""
    subtitle_cid: int = 0


def _remove_file(path: str, label: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
        print(f"[清理] 已删除{label}: {Path(path).name}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[清理] ⚠️ 删除{label}失败: {e}")


def _cleanup_after_success(video, cover_path: str, extra_video_paths: list[str] | None = None) -> None:
    if not config.CLEANUP_AFTER_UPLOAD:
        return

    _remove_file(video.file_path, "原始视频")
    if extra_video_paths:
        for vp in extra_video_paths:
            if vp != video.file_path:
                _remove_file(vp, "分P视频")
        # Remove empty split directory
        split_dir = Path(video.file_path).parent / "splits" / Path(video.file_path).stem
        if split_dir.exists():
            try:
                split_dir.rmdir()  # only removes if empty
            except OSError:
                pass
    _remove_file(video.thumbnail_path, "缩略图")
    if cover_path and cover_path != video.thumbnail_path:
        _remove_file(cover_path, "封面")


def process_video(url: str, credential=None) -> ProcessResult:
    """
    Process a single YouTube URL through the full pipeline.

    Args:
        url: YouTube video URL.
        credential: Optional pre-built bilibili_api Credential.
                    When omitted the active profile is used.

    Returns:
        ProcessResult with success/failure details
    """
    record = ProcessResult(url=url)

    print("=" * 60)
    print(f"🚀 处理视频: {url}")
    print("=" * 60)

    # ── Step 1: Download ──────────────────────────────────────
    record.stage = "download"
    try:
        video = download_video(url)
    except Exception as e:
        record.error = str(e)
        print(f"\n❌ 下载失败: {e}")
        return record

    record.video_path = video.file_path
    record.thumbnail_path = video.thumbnail_path
    record.original_title = video.title
    if video.width and video.height:
        record.video_resolution = f"{video.width}x{video.height}"

    # ── Step 1.5: Split if video exceeds Bilibili's 10h limit ──
    record.stage = "split"
    video_files_for_upload = [video.file_path]
    if video.duration > 0 and video.duration > config.MAX_VIDEO_DURATION_SECONDS:
        print(f"\n[分割] 视频时长 {video.duration:.0f}s ({video.duration/3600:.2f}h)"
              f"，超过 {config.MAX_VIDEO_DURATION_SECONDS/3600:.0f}h 限制")
        try:
            segments = split_video(video.file_path)
            if len(segments) > 1:
                video_files_for_upload = segments
            elif len(segments) == 1:
                print(f"[分割] 分割后仅 1 个文件，按单分P处理")
            else:
                print(f"[分割] ⚠️ 分割失败，将上传原始文件")
        except Exception as e:
            print(f"[分割] ⚠️ 分割异常: {e}，将上传原始文件")

    # ── Step 2: Translate title ───────────────────────────────
    record.stage = "translate"
    print(f"\n[翻译] 原标题: {video.title}")
    try:
        translated_title = translate(video.title, source_lang=config.SOURCE_LANG)
        if not translated_title:
            raise RuntimeError("翻译结果为空")
        print(f"[翻译] 中文标题: {translated_title}")
    except Exception as e:
        record.error = str(e)
        print(f"\n❌ 翻译失败: {e}")
        return record

    record.translated_title = translated_title

    # ── Step 3: Prepare cover ─────────────────────────────────
    record.stage = "cover"
    try:
        cover_path = prepare_cover(video.thumbnail_path, video.video_id)
        if not cover_path:
            raise RuntimeError("没有可用的视频缩略图，无法生成 1920x1080 封面")
        cover_size = image_size(cover_path)
        print(f"[封面] 已生成: {cover_path}")
        if cover_size:
            print(f"[封面] 尺寸: {cover_size[0]}x{cover_size[1]}")
    except Exception as e:
        record.error = str(e)
        print(f"\n❌ 封面处理失败: {e}")
        return record

    record.cover_path = cover_path

    # ── Step 4: Upload to Bilibili ────────────────────────────
    record.stage = "upload"
    is_multi_part = len(video_files_for_upload) > 1

    print()
    try:
        result = upload_video(
            file_paths=video_files_for_upload,
            title=translated_title,
            original_url=video.original_url,
            original_description=video.description,
            original_title=video.title,
            cover_path=cover_path,
            credential=credential,
        )
        if not result.success:
            raise RuntimeError(result.message)
    except RuntimeError as e:
        msg = str(e)
        record.error = msg
        if "请重新扫码登录" in msg:
            print(f"\n🔐 {msg}")
        else:
            print(f"\n❌ 上传失败: {e}")
        return record
    except Exception as e:
        record.error = str(e)
        print(f"\n❌ 上传失败: {e}")
        return record

    record.success = True
    record.stage = "complete"
    record.bvid = result.bvid
    record.aid = result.aid

    # ── Step 4.5: Subtitle processing (synchronous, after upload) ──
    record.stage = "subtitle"

    if is_multi_part:
        print(f"\n[字幕] 多分P视频，跳过自动字幕上传")
        record.subtitle_status = "skipped_multi_part"
    elif not config.SUBTITLE_ENABLED:
        print(f"\n[字幕] 字幕功能已禁用")
        record.subtitle_status = "skipped_disabled"
    else:
        try:
            # Download source subtitles
            record.stage = "subtitle_download"
            subtitle_path = download_subtitles(video.original_url, video.video_id)
            if not subtitle_path:
                raise RuntimeError("YouTube 上未找到匹配的字幕语言")
            record.subtitle_source_path = str(subtitle_path)
            print(f"[字幕] 源字幕: {Path(subtitle_path).name}")

            # Parse SRT cues
            record.stage = "subtitle_parse"
            cues = parse_subtitle(subtitle_path)
            if not cues:
                raise RuntimeError("字幕文件解析为空")
            print(f"[字幕] 解析: {len(cues)} 条字幕")

            # Translate via DeepSeek batch
            record.stage = "subtitle_translate"
            translated = translate_cues(cues, batch_size=config.SUBTITLE_TRANSLATE_BATCH_SIZE)
            if not translated:
                raise RuntimeError("翻译后字幕为空")
            print(f"[字幕] 翻译完成: {len(translated)} 条字幕")

            # Write translated SRT file
            record.stage = "subtitle_write"
            subtitle_dir = Path(config.SUBTITLE_DIR)
            subtitle_dir.mkdir(parents=True, exist_ok=True)
            translated_filename = f"{video.video_id}.{config.SUBTITLE_TARGET_LANG}.srt"
            translated_path = str(subtitle_dir / translated_filename)
            write_srt(translated, translated_path)
            record.subtitle_translated_path = translated_path
            print(f"[字幕] 已保存: {translated_filename}")

            # Defer subtitle upload (Bilibili CID may not be ready for hours)
            if config.SUBTITLE_UPLOAD_TO_BILIBILI and record.bvid and record.aid:
                save_pending_subtitle(
                    bvid=record.bvid,
                    aid=record.aid,
                    translated_path=translated_path,
                )
                record.subtitle_status = "pending_upload"
                print(f"[字幕] 已加入延迟上传队列，等待 B站 CID 就绪后自动上传")
            else:
                record.subtitle_status = (
                    "success" if not config.SUBTITLE_UPLOAD_TO_BILIBILI
                    else "skipped_upload_disabled"
                )
                print(f"[字幕] 已生成翻译字幕，未上传到 B站")

        except Exception as e:
            record.subtitle_error = str(e)
            record.subtitle_status = "failed"
            if config.SUBTITLE_REQUIRED:
                record.error = str(e)
                record.stage = "subtitle"
                record.success = False
                print(f"\n[FAIL] 字幕处理失败（必需）: {e}")
                return record
            print(f"[字幕] [WARN] {e}（非致命，继续）")

    # ── Step 5: Report result ─────────────────────────────────
    print()
    print("=" * 60)
    part_note = f" ({len(video_files_for_upload)}分P)" if len(video_files_for_upload) > 1 else ""
    print(f"🎉 全流程完成!{part_note}")
    print(f"   B站 BV号: {result.bvid}")
    print(f"   B站 AV号: {result.aid}")
    if result.bvid:
        print(f"   视频链接: https://www.bilibili.com/video/{result.bvid}")
    print(f"   中文标题: {translated_title}")
    print(f"   原视频: {url}")
    print("=" * 60)

    _cleanup_after_success(
        video, cover_path,
        extra_video_paths=video_files_for_upload if len(video_files_for_upload) > 1 else None,
    )
    # Force release of yt-dlp connection pools to prevent fd exhaustion
    gc.collect()
    return record


def _cleanup_old_runs(runs_dir: Path, *, keep_days: int | None = None) -> int:
    """Delete run reports older than `keep_days`. Returns count of deleted files."""
    if not runs_dir.exists():
        return 0
    if keep_days is None:
        keep_days = config.RUNS_RETENTION_DAYS
    cutoff = datetime.now() - timedelta(days=keep_days)
    deleted = 0
    for f in runs_dir.glob("*.json"):
        if f.name == "latest.json":
            continue
        try:
            file_time = datetime.strptime(f.stem, "%Y%m%d-%H%M%S")
            if file_time < cutoff:
                f.unlink()
                deleted += 1
        except (ValueError, OSError):
            pass
    return deleted


def _write_run_report(results: list[ProcessResult]) -> Path:
    """Write a batch report to runs/latest.json and a timestamped JSON file."""
    runs_dir = Path(config.RUNS_DIR)
    runs_dir.mkdir(parents=True, exist_ok=True)

    success_count = sum(1 for r in results if r.success)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(results),
        "success": success_count,
        "failed": len(results) - success_count,
        "results": [asdict(r) for r in results],
    }

    report_path = runs_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    latest_path = runs_dir / "latest.json"
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    report_path.write_text(content + "\n", encoding="utf-8")
    latest_path.write_text(content + "\n", encoding="utf-8")

    _cleanup_old_runs(runs_dir)

    return report_path


def _ensure_credentials():
    """Check B站 credentials, trigger QR login if missing. Returns True if OK."""
    profile_name = profile_mod.get_active_profile_name()

    if profile_name != "default" or profile_mod.is_multi_profile():
        # ── Profile-aware check ────────────────────────────────
        prof = profile_mod.resolve_profile(profile_name)
        if prof is None:
            print(f"❌ 账号 '{profile_name}' 不存在。")
            sys.exit(1)
        if not prof.bilibili.sessdata or not prof.bilibili.bili_jct:
            print(f"⚠️  账号 '{profile_name}' 未检测到 B站 登录凭据，需要先扫码登录。")
            try:
                auth.get_credential(profile_name=profile_name)
            except KeyboardInterrupt:
                print("\n用户取消登录，退出。")
                sys.exit(1)
            print("✅ 登录成功！\n")
        return

    # ── Legacy .env path ───────────────────────────────────────
    all_issues = validate()
    cred_issues = [i for i in all_issues if "SESSDATA" in i.upper() or "BILI_JCT" in i.upper()]
    other_issues = [i for i in all_issues if i not in cred_issues]

    # Stop on non-credential config errors
    if other_issues:
        print("❌ 配置错误:")
        for issue in other_issues:
            print(f"   - {issue}")
        print("\n请检查 .env 配置文件。")
        sys.exit(1)

    if cred_issues:
        print("⚠️  未检测到 B站 登录凭据，需要先扫码登录。")
        from yt2bili import config as cfg
        cfg.BILI_SESSDATA = ""
        cfg.BILI_BILI_JCT = ""
        try:
            auth.get_credential()
        except KeyboardInterrupt:
            print("\n用户取消登录，退出。")
            sys.exit(1)
        print("✅ 登录成功！\n")


def _login_interactive() -> None:
    """Refresh B站 credentials through the QR login flow."""
    profile_name = profile_mod.get_active_profile_name()

    if profile_name != "default" or profile_mod.is_multi_profile():
        auth.login_interactive(profile_name=profile_name)
    else:
        from yt2bili import config as cfg
        cfg.BILI_SESSDATA = ""
        cfg.BILI_BILI_JCT = ""
        auth.login_interactive()
    print("\n凭据已更新。下次运行将使用新的凭据。\n")


def _list_profiles() -> None:
    """Print all configured profiles and exit."""
    if not profile_mod.is_multi_profile():
        print("📋 未检测到多账号配置（config/profiles.json 不存在）。")
        print("   当前使用默认账号（.env 中的 BILI_* 凭据）。")
        print()
        print("   创建多账号配置: 复制 config/profiles.json.example → config/profiles.json")
        return

    profiles = profile_mod.load_profiles()
    if not profiles:
        print("📋 没有配置任何账号。")
        return

    print(f"📋 共 {len(profiles)} 个账号配置:\n")
    for name, p in profiles.items():
        channel_count = len(p.youtube.channels)
        has_creds = bool(p.bilibili.sessdata and p.bilibili.bili_jct)
        status = "✅ 已登录" if has_creds else "⚠️  未登录"
        tags = p.settings.default_tags or config.DEFAULT_TAGS
        tid = p.settings.default_tid or config.DEFAULT_TID
        print(f"  [{name}]  {status}")
        print(f"         分区: {tid}, 标签: {tags}")
        print(f"         YouTube 频道: {channel_count} 个")
        if p.youtube.channels:
            for c in p.youtube.channels:
                print(f"           - {c.channel_title} ({c.channel_id})")
        if p.youtube.monitor_source:
            print(f"         源: {p.youtube.monitor_source}")
        print()
    print("用法: python main.py --profile <账号名> [其他参数]")


def setup_profile(args) -> None:
    """Initialize the profile system from CLI args. Call once at startup."""
    if args.list_profiles:
        _list_profiles()
        sys.exit(0)

    if args.profile:
        profile = profile_mod.resolve_profile(args.profile)
        if profile is None:
            print(f"❌ 账号 '{args.profile}' 不存在。")
            print(f"   可用账号: {', '.join(profile_mod.load_profiles().keys()) or '(无)'}")
            print(f"   使用 --list-profiles 查看所有账号。")
            sys.exit(1)
        profile_mod.set_active_profile(args.profile)
        config.apply_profile_overrides(args.profile)
    elif profile_mod.is_multi_profile():
        # profiles.json exists but no --profile flag: use "default" if present
        if profile_mod.profile_exists("default"):
            profile_mod.set_active_profile("default")
            config.apply_profile_overrides("default")
        # Otherwise stay in legacy .env mode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YouTube → Bilibili 自动转载流水线")
    parser.add_argument("urls", nargs="*", help="YouTube 视频链接")
    parser.add_argument("--file", help="从文件批量读取 YouTube URL，默认每行一个")
    parser.add_argument("--login", action="store_true", help="重新扫码登录 B站")
    parser.add_argument("--refresh-youtube-cookies", action="store_true", help="从浏览器自动生成/刷新 YouTube cookies.txt (config/)")
    parser.add_argument("--check-auth", action="store_true", help="检查所有凭据（Bilibili/YouTube OAuth/YouTube Cookie）的有效期和状态")
    parser.add_argument("--monitor", action="store_true", help="每小时检查 YouTube 订阅更新并自动上传")
    parser.add_argument("--subtitle-only", action="store_true", help="仅轮询上传待处理字幕（不上传视频）")
    parser.add_argument("--subtitle-interval", type=int, default=600, help="字幕轮询间隔秒数（默认 600=10分钟）")
    parser.add_argument("--no-subtitle-upload", action="store_true", help="跳过延迟字幕上传（仅下载+翻译，不提交到 B站）")
    parser.add_argument("--discord", action="store_true", help="实时监听 Discord 频道消息并搬运到 B站动态")
    parser.add_argument("--once", action="store_true", help="仅执行一次（--monitor / --subtitle-only 模式下生效）")
    parser.add_argument("--dry-run", action="store_true", help="仅在 --monitor 模式下打印待处理视频")
    parser.add_argument(
        "--monitor-source",
        choices=("api", "rss"),
        default=config.YOUTUBE_MONITOR_SOURCE,
        help="订阅来源，默认读取 YOUTUBE_MONITOR_SOURCE",
    )
    parser.add_argument(
        "--monitor-limit",
        type=int,
        default=config.YOUTUBE_MONITOR_LIMIT,
        help="每轮最多读取多少条订阅视频",
    )
    parser.add_argument(
        "--monitor-interval",
        type=int,
        default=config.YOUTUBE_MONITOR_INTERVAL_SECONDS,
        help="订阅轮询间隔秒数",
    )
    parser.add_argument(
        "--monitor-state",
        type=Path,
        default=Path(config.YOUTUBE_MONITOR_STATE),
        help="订阅处理状态文件",
    )
    parser.add_argument(
        "--max-videos-per-channel",
        type=int,
        default=config.YOUTUBE_MAX_VIDEOS_PER_CHANNEL,
        help="每个订阅频道抓取最近多少条视频",
    )
    parser.add_argument(
        "--title-filter",
        type=str,
        default="",
        help="仅处理标题包含指定关键词的视频（不区分大小写，与 --monitor 配合）",
    )
    parser.add_argument(
        "--no-speed-protection",
        action="store_true",
        help="禁用下载低速保护（不限制最低下载速度）",
    )
    parser.add_argument(
        "--profile", default="",
        help="选择 B站 账号配置（config/profiles.json 中定义的名称），留空使用默认账号（.env）",
    )
    parser.add_argument(
        "--list-profiles", action="store_true",
        help="列出 config/profiles.json 中所有可用账号配置",
    )
    parser.add_argument(
        "--all-profiles", action="store_true",
        help="与 --monitor 配合，轮流处理所有配置了频道的账号",
    )
    parser.add_argument(
        "--channels-file", type=Path, default=None,
        help="指定 YouTube 频道列表文件（每行一个 channel_id,频道名称），覆盖 profile 的 channels",
    )
    parser.add_argument(
        "--resolve-channel", type=str, default=None,
        help="解析 YouTube 频道 @句柄 或 URL 为 Channel ID（用于配置 profiles.json）",
    )
    return parser


def _gather_urls(args: argparse.Namespace) -> list[str]:
    """Collect URLs from cli args > urls.txt > interactive input."""
    if args.file:
        return _read_urls_file(args.file)

    urls = [url for url in args.urls if url.startswith("http")]
    if urls:
        return urls

    # ── Auto-detect urls.txt ──────────────────────────────────
    urls_file = config.PROJECT_ROOT / "config" / "urls.txt"
    if urls_file.exists():
        urls = _read_urls_file(str(urls_file))
        if urls:
            return urls

    # ── Interactive mode ──────────────────────────────────────
    print("=" * 60)
    print("  YouTube → Bilibili 自动转载流水线")
    print("=" * 60)
    print()
    url = input("请输入 YouTube 视频链接: ").strip()
    return [url] if url else []


def _read_urls_file(path: str) -> list[str]:
    """Read YouTube URLs from a text file (one per line, skip blanks & #comments)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            urls = [line.strip() for line in f
                    if line.strip() and not line.strip().startswith("#")]
        print(f"📄 从 {path} 读取到 {len(urls)} 个链接")
        return urls
    except FileNotFoundError:
        print(f"❌ 文件不存在: {path}")
        return []


def _check_external_tools() -> None:
    """Check for optional external tools (ffmpeg, ffprobe, Node.js) on PATH.

    Missing tools print a warning but do not prevent startup — the pipeline
    degrades gracefully when they are absent.
    """
    import subprocess

    tools = {
        "ffmpeg": "视频分割不可用",
        "ffprobe": "分辨率/时长探测不可用",
        "node": "yt-dlp 将使用内置 JS 引擎（可能较慢）",
    }
    for tool, impact in tools.items():
        try:
            subprocess.run(
                [tool, "-version"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            version_check = "OK"
        except FileNotFoundError:
            version_check = f"未找到 — {impact}"
        except subprocess.TimeoutExpired:
            version_check = f"响应超时 — {impact}"
        except OSError as exc:
            version_check = f"启动失败: {exc} — {impact}"
        print(f"[工具] {tool}: {version_check}")


def main():
    """Main entry point."""
    args = _build_parser().parse_args()

    _check_external_tools()

    # 清理超过 3 天的旧 runs 报告
    _cleanup_old_runs(Path(config.RUNS_DIR))

    if args.no_speed_protection:
        config.DOWNLOAD_MIN_SPEED_KIB = 0

    if args.title_filter:
        config.TITLE_FILTER_KEYWORD = args.title_filter

    # ── Profile setup (before credential checks) ───────────────
    setup_profile(args)

    # ── --resolve-channel helper ───────────────────────────────
    if args.resolve_channel:
        from yt2bili.youtube.subscriptions import resolve_channel_handle_ytdlp
        try:
            cid, title = resolve_channel_handle_ytdlp(args.resolve_channel)
            print(f"✅ 频道解析成功:")
            print(f"   Channel ID : {cid}")
            print(f"   频道名称   : {title}")
            print()
            print(f"   将以下内容添加到 config/profiles.json 的 channels 列表中:")
            print(f'   {{"channel_id": "{cid}", "channel_title": "{title}"}}')
        except Exception as e:
            print(f"❌ 解析失败: {e}")
            return 1
        return 0

    # ── --list-profiles is handled in setup_profile ────────────

    if args.check_auth:
        from yt2bili.auth_checker import run_auth_check

        exit_code = run_auth_check()
        if not args.login and not args.monitor and not args.file and not args.urls and not args.refresh_youtube_cookies and not args.subtitle_only:
            return exit_code

    if args.refresh_youtube_cookies:
        from yt2bili.youtube.downloader import refresh_youtube_cookies

        cookie_path = refresh_youtube_cookies()
        if not cookie_path:
            return 1
        if not args.login and not args.monitor and not args.file and not args.urls and not args.subtitle_only:
            return 0

    if args.login:
        _login_interactive()
        if not args.monitor and not args.file and not args.urls and not args.subtitle_only:
            return 0

    # ── Subtitle-only mode ─────────────────────────────────────
    if args.subtitle_only:
        _ensure_credentials()
        from yt2bili.bilibili.subtitle import upload_pending_subtitles

        if args.once:
            uploaded = upload_pending_subtitles()
            if uploaded == 0 and not Path("state/pending_subtitles.json").exists():
                print("[字幕] 没有待处理的字幕。")
            return 0

        interval = max(10, args.subtitle_interval)
        print(f"[字幕] 字幕轮询模式启动，间隔 {interval}s，按 Ctrl+C 停止")
        while True:
            try:
                upload_pending_subtitles()
            except Exception as e:
                print(f"[字幕] 轮询异常（非致命）: {e}")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\n[字幕] 已停止。")
                return 0

    # ── Resolve monitor channels from profile / args ───────────
    from yt2bili.youtube.subscriptions import Subscription, load_channels_file

    monitor_channels: list[Subscription] | None = None
    if args.monitor:
        # 1) --channels-file takes top priority
        if args.channels_file:
            channels_path = Path(args.channels_file)
            if not channels_path.is_absolute():
                channels_path = config.PROJECT_ROOT / channels_path
            monitor_channels = load_channels_file(channels_path)
            print(f"[频道] 从文件加载 {len(monitor_channels)} 个频道: {args.channels_file}")
        # 2) Profile with explicit channels
        elif args.profile or profile_mod.is_multi_profile():
            prof = profile_mod.resolve_profile(profile_mod.get_active_profile_name())
            if prof and prof.youtube.channels:
                monitor_channels = [
                    Subscription(c.channel_id, c.channel_title)
                    for c in prof.youtube.channels
                ]
                print(f"[频道] 从账号 '{prof.name}' 加载 {len(monitor_channels)} 个频道")
                # Default to RSS when using profile channels to avoid API quota
                if not args.monitor_source and not prof.youtube.monitor_source:
                    args.monitor_source = "rss"
                elif not args.monitor_source:
                    args.monitor_source = prof.youtube.monitor_source or args.monitor_source

    # ── Both YouTube + Discord monitors (parallel) ──────────────
    if args.monitor and args.discord:
        if args.once or args.dry_run:
            print("⚠️  --once / --dry-run 在 --monitor --discord 并行模式下不适用")
            return 1
        _ensure_credentials()
        import asyncio
        import threading
        from yt2bili.youtube.monitor import project_path, run_monitor_loop
        from yt2bili.discord.monitor import run_discord_monitor

        def _run_discord():
            asyncio.run(run_discord_monitor())

        discord_thread = threading.Thread(target=_run_discord, daemon=True, name="discord-monitor")
        discord_thread.start()

        print("[并行] YouTube 监控 + Discord 监控 同时运行中...")
        print("       按 Ctrl+C 停止\n")

        return run_monitor_loop(
            process_video=process_video,
            write_run_report=_write_run_report,
            interval_seconds=args.monitor_interval,
            once=False,
            dry_run=False,
            skip_subtitle_upload=args.no_subtitle_upload,
            state_path=project_path(args.monitor_state),
            source=args.monitor_source,
            limit=args.monitor_limit,
            max_videos_per_channel=args.max_videos_per_channel,
            client_secret_file=project_path(config.YOUTUBE_CLIENT_SECRET_FILE),
            token_file=project_path(config.YOUTUBE_TOKEN_FILE),
            cache_file=project_path(config.YOUTUBE_SUBSCRIPTIONS_CACHE),
            channels=monitor_channels,
        )

    # ── Handle --all-profiles ──────────────────────────────────
    if args.monitor and args.all_profiles:
        profiles = profile_mod.load_profiles()
        profile_list = [p for p in profiles.values() if p.youtube.channels]
        if not profile_list:
            print("❌ 没有配置了频道的账号。请先在 profiles.json 中为账号添加 channels。")
            return 1
        if not args.dry_run:
            _ensure_credentials()
        from yt2bili.youtube.monitor import project_path, run_monitor_loop

        return run_monitor_loop(
            process_video=process_video,
            write_run_report=None if args.dry_run else _write_run_report,
            interval_seconds=args.monitor_interval,
            once=args.once,
            dry_run=args.dry_run,
            skip_subtitle_upload=args.no_subtitle_upload,
            profiles=profile_list,
            state_path=project_path(args.monitor_state),
            source=args.monitor_source,
            limit=args.monitor_limit,
            max_videos_per_channel=args.max_videos_per_channel,
            client_secret_file=project_path(config.YOUTUBE_CLIENT_SECRET_FILE),
            token_file=project_path(config.YOUTUBE_TOKEN_FILE),
            cache_file=project_path(config.YOUTUBE_SUBSCRIPTIONS_CACHE),
        )

    # ── YouTube monitor only ──────────────────────────────────
    if args.monitor:
        # Per-profile state file
        monitor_state = args.monitor_state
        if monitor_channels and not args.channels_file:
            prof = profile_mod.resolve_profile(profile_mod.get_active_profile_name())
            if prof:
                monitor_state = profile_mod.get_state_file_path(prof)
                # Ensure state directory exists
                monitor_state.parent.mkdir(parents=True, exist_ok=True)

        if args.monitor_source not in ("api", "rss"):
            raise SystemExit("--monitor-source must be api or rss")
        if args.monitor_limit <= 0:
            raise SystemExit("--monitor-limit must be positive")
        if args.max_videos_per_channel <= 0:
            raise SystemExit("--max-videos-per-channel must be positive")
        if not args.dry_run:
            _ensure_credentials()
        from yt2bili.youtube.monitor import project_path, run_monitor_loop

        return run_monitor_loop(
            process_video=process_video,
            write_run_report=None if args.dry_run else _write_run_report,
            interval_seconds=args.monitor_interval,
            once=args.once,
            dry_run=args.dry_run,
            skip_subtitle_upload=args.no_subtitle_upload,
            state_path=project_path(monitor_state),
            source=args.monitor_source,
            limit=args.monitor_limit,
            max_videos_per_channel=args.max_videos_per_channel,
            client_secret_file=project_path(config.YOUTUBE_CLIENT_SECRET_FILE),
            token_file=project_path(config.YOUTUBE_TOKEN_FILE),
            cache_file=project_path(config.YOUTUBE_SUBSCRIPTIONS_CACHE),
            channels=monitor_channels,
        )

    if args.once or args.dry_run:
        print("⚠️  --once / --dry-run 只在 --monitor 模式下生效，当前按普通模式运行。")

    if args.discord:
        if not config.DISCORD_BOT_TOKEN:
            print("❌ 未配置 DISCORD_BOT_TOKEN")
            print("   请在 .env 中设置 DISCORD_BOT_TOKEN")
            return 1
        if not config.DISCORD_CHANNEL_IDS:
            print("❌ 未配置 DISCORD_CHANNEL_IDS")
            print("   请在 .env 中设置 DISCORD_CHANNEL_IDS=频道ID1,频道ID2")
            return 1
        _ensure_credentials()
        from yt2bili.discord.monitor import run_discord_monitor
        import asyncio
        asyncio.run(run_discord_monitor())
        return 0

    # Step 0: Ensure logged in (QR code flow on first run)
    _ensure_credentials()

    # Build credential for active profile (used in batch/single mode)
    batch_credential = None
    profile_name = profile_mod.get_active_profile_name()
    if profile_name != "default" or profile_mod.is_multi_profile():
        batch_credential = auth.get_credential(profile_name=profile_name)

    # Step 1: Gather URLs
    urls = _gather_urls(args)

    if not urls:
        print("未提供任何链接，退出。")
        print("用法：")
        print("  python main.py <youtube_url>                 单个视频")
        print("  python main.py --file urls.txt               从文件批量读取")
        print("  python main.py --monitor                     每小时检查订阅更新")
        print("  python main.py --monitor --once --dry-run     只检查一次，不下载上传")
        print("  python main.py --monitor --profile snap       使用指定账号监控")
        print("  python main.py --monitor --all-profiles       轮流监控所有账号")
        print("  python main.py --login                        重新扫码登录")
        print("  python main.py --login --profile snap         登录指定账号")
        print("  python main.py --list-profiles                列出所有账号")
        print("  python main.py --resolve-channel @Handle      解析频道句柄")
        print("  python main.py                                自动读取 urls.txt 或交互输入")
        return 0

    # Step 2: Process all URLs
    results: list[ProcessResult] = []

    for i, url in enumerate(urls):
        if len(urls) > 1:
            print(f"\n[{i + 1}/{len(urls)}]")

        results.append(process_video(url, credential=batch_credential))

    # Summary
    success_count = sum(1 for r in results if r.success)
    fail_count = len(results) - success_count
    report_path = _write_run_report(results)
    print(f"\n📊 完成: 成功 {success_count}, 失败 {fail_count}")
    print(f"📝 结果记录: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
