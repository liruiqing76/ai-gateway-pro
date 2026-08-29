# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — 智能网关 桌面版

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_dynamic_libs

BASE = Path(os.path.abspath('.'))
# 混淆构建时 build_exe.py 设 GW_SRC_ROOT 指向 staging(build_src/)，其中核心
# 模块已编译为 .pyd、对应 .py 已删除; 不设则等于 BASE(明文源码, 供 dev 构建)。
SRC_ROOT = Path(os.getenv("GW_SRC_ROOT") or os.path.abspath('.'))
WEB_DIR = BASE / 'web'


def collect_source_tree(src, dst):
    """源码目录进包, 但剔除运行时垃圾: __pycache__/数据库及备份/签到截图缓存/日志。
    数据库属于用户机器上的运行数据, 打进安装包既泄密又白占体积。

    注意 backend/data/ 不能整目录排除: 该目录同时放静态资源
    provider_templates.json / free_sources.json(模板一键添加依赖), 必须打进包。
    因此对该目录改为"只收这两个 .json, 排除 db/-shm/-wal/备份/搜索结果"的精筛。"""
    exclude_rel_prefixes = tuple(p.replace('\\', '/')
                                 for p in ('desktop/checkin/shots/',
                                           'desktop/logs/', 'logs/'))
    # backend/data/ 下只保留两个静态配置资源; 其余(数据库 gateway.db 及 -shm/-wal/
    # 备份, free_models/ 搜索结果运行数据)全部排除, 避免泄密与白占体积
    DATA_REL_PREFIX = 'backend/data/'
    DATA_KEEP = ('backend/data/provider_templates.json',
                 'backend/data/free_sources.json')
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.pytest_cache',
                                               'build', 'cython_build')]
        for f in files:
            rel = (Path(root) / f).relative_to(src).as_posix()
            if rel.startswith(exclude_rel_prefixes):
                continue
            if rel.startswith(DATA_REL_PREFIX) and rel not in DATA_KEEP:
                continue
            # .pyd 不排除(混淆构建靠它交付编译模块); .c/.obj 等编译中间产物排除
            # db 类(含 -shm/-wal/备份, '.db' 子串覆盖全家族)排除
            if f.endswith(('.pyc', '.log', '.c', '.obj', '.exp', '.lib', '.o')) \
                    or '.db' in f:
                continue
            parent = os.path.dirname(rel)
            out.append((str(Path(src) / rel), str(Path(dst) / parent) if parent else dst))
    return out


# 完整收集关键包（含数据文件/dll）
datas, binaries, hiddenimports = [], [], []
for pkg in ['uvicorn', 'webview', 'apscheduler', 'fastapi', 'starlette',
            'pydantic', 'httpx', 'bcrypt', 'jwt', 'cryptography',
            'typing_extensions', 'pythoncom', 'pywintypes', 'win32com',
            'webview.lib',
            # 托盘图标(关闭最小化到托盘)
            'pystray', 'PIL._tkinter_finder',
            # 签到脚本依赖的 OCR 运行时 + GUI 自动化(必须打进包; 脚本由 runpy
            # 动态执行, PyInstaller 静态分析抓不到这些 import):
            'rapidocr_onnxruntime', 'onnxruntime',
            'pyautogui', 'pygetwindow', 'pymsgbox', 'pytweening', 'pyrect',
            'pyperclip', 'mouseinfo', 'PIL', 'numpy']:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# sqlalchemy 只收代码与扩展, 不用 collect_all(会连带 docs/examples 多吃 ~10MB);
# numpy 同理只由上面 OCR 链的 collect_all 覆盖。二者均由 runpy 动态加载, 必须显式声明。
hiddenimports += collect_submodules('sqlalchemy')
binaries += collect_dynamic_libs('sqlalchemy')

a = Analysis(
    [str(SRC_ROOT / 'desktop' / 'desktop.py')],
    pathex=[str(SRC_ROOT)],
    binaries=binaries,
    datas=datas + [
        (str(WEB_DIR), 'web'),
    ] + collect_source_tree(SRC_ROOT / 'backend', 'backend') + collect_source_tree(
        SRC_ROOT / 'desktop', 'desktop'),
    hiddenimports=hiddenimports + [
        'pythoncom',
        'pywintypes',
        'win32timezone',
        'win32com',
        'win32com.client',
        'win32com.shell',
        'sqlalchemy',
        'sqlalchemy.dialects.sqlite',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'pandas', 'PySide6',
              'pytest', '_pytest', 'py', 'pluggy', 'iniconfig', 'packaging',
              # 全局环境里的重依赖会被依赖分析连带拖入(如 pywebview 的 Qt 备用
              # 后端; onnxruntime hook 会连带 onnxruntime.transformers ->
              # transformers -> huggingface_hub -> sklearn 整条训练侧依赖),
              # 项目代码与签到 OCR(onnxruntime 推理本体)均不使用。
              # 注意: sklearn 与 scipy 必须同时排除, 只留一个会运行时 ImportError:
              'torch', 'torchaudio', 'torchvision', 'scipy', 'sklearn',
              'moviepy', 'PyQt5', 'IPython',
              'transformers', 'huggingface_hub', 'yt_dlp', 'curl_cffi',
              'mutagen'],
    noarchive=False,
)

pyz = PYZ(a.pure)

# 应用图标: 用户可随时覆盖 desktop/assets/app.ico 换图, 重新打包即生效;
# 文件不存在时不设置(PyInstaller 会报错), 托盘/窗口走运行时占位逻辑
_APP_ICON = BASE / 'desktop' / 'assets' / 'app.ico'

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='智能网关',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(_APP_ICON) if _APP_ICON.exists() else None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)