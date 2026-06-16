# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for yt2bili.exe (YouTube → Bilibili pipeline).

Build:  pyinstaller yt2bili.spec
Output: dist\yt2bili.exe

Requires UPX on PATH for compression (optional, saves ~40% size).
"""

import pkgutil
from pathlib import Path

# ── Collect yt-dlp extractor submodules ────────────────────────
# yt-dlp discovers extractors at runtime via pkgutil.iter_modules.
# Without this, the frozen EXE will fail with "No extractors found".
_yt_extractor_hiddenimports = []
try:
    import yt_dlp.extractor as _yt_extractors
    for _importer, _modname, _ispkg in pkgutil.walk_packages(
        _yt_extractors.__path__, prefix="yt_dlp.extractor."
    ):
        _yt_extractor_hiddenimports.append(_modname)
except Exception:
    pass

# ── Block 1: Analysis ──────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        (".env.example", "."),
    ],
    hiddenimports=[
        # yt-dlp internals (loaded dynamically)
        "yt_dlp.cookies",
        "yt_dlp.postprocessor.ffmpeg",
        "yt_dlp.postprocessor.metadataembed",
        "yt_dlp.postprocessor.thumbnail",
        # bilibili-api-python async internals
        "bilibili_api",
        "bilibili_api.video_uploader",
        "bilibili_api.login_v2",
        "bilibili_api.credential",
        # Google API + OAuth
        "googleapiclient",
        "googleapiclient.discovery",
        "google_auth_oauthlib",
        "google_auth_oauthlib.flow",
        "google_auth_httplib2",
        # HTTP / translation / misc
        "httpx",
        "aiohttp",
        "requests",
        "feedparser",
        "nest_asyncio",
        "deep_translator",
        "deep_translator.google",
        "openai",
        "socksio",
        "httplib2",
        "dotenv",
        # Pillow image processing
        "PIL",
    ] + _yt_extractor_hiddenimports,
    hookspath=[],
    hooksconfig={},
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "numpy",
        "unittest",
        "test",
        "pydoc",
        "ensurepip",
        "turtledemo",
        "venv",
        "ipykernel",
        "jupyter_client",
        "nbformat",
    ],
    noarchive=False,
)

# ── Strip debug symbols ────────────────────────────────────────
a.binaries = [b for b in a.binaries if not b[0].endswith(".pdb")]

# ── Block 2: Collect data files from key packages ──────────────
try:
    import PyInstaller.utils.hooks as _pi_hooks
    for _mod in [
        "yt_dlp", "bilibili_api", "PIL", "googleapiclient",
        "deep_translator", "openai", "httpx", "aiohttp",
        "feedparser", "nest_asyncio",
    ]:
        try:
            a.datas += _pi_hooks.collect_data_files(_mod)
        except Exception:
            pass
        try:
            a.binaries += _pi_hooks.collect_dynamic_libs(_mod)
        except Exception:
            pass
except Exception:
    pass

# ── Block 3: PYZ, EXE, COLLECT ─────────────────────────────────
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

_icon_path = "icon.ico" if Path("icon.ico").exists() else None

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="yt2bili",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon_path,
)
