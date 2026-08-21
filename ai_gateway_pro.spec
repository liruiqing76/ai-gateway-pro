# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — 智能网关 桌面版

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

BASE = Path(os.path.abspath('.'))
WEB_DIR = BASE / 'web'

# 完整收集关键包（含数据文件/dll）
datas, binaries, hiddenimports = [], [], []
for pkg in ['uvicorn', 'webview', 'apscheduler', 'fastapi', 'starlette',
            'pydantic', 'sqlalchemy', 'httpx', 'bcrypt', 'jwt', 'cryptography',
            'typing_extensions', 'pythoncom', 'pywintypes', 'win32com',
            'webview.lib',
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

a = Analysis(
    ['desktop/desktop.py'],
    pathex=[str(BASE)],
    binaries=binaries,
    datas=datas + [
        (str(WEB_DIR), 'web'),
        ('backend', 'backend'),
        ('desktop', 'desktop'),
    ],
    hiddenimports=hiddenimports + [
        'pythoncom',
        'pywintypes',
        'win32timezone',
        'win32com',
        'win32com.client',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'pandas', 'PySide6',
              'pytest', '_pytest', 'py', 'pluggy', 'iniconfig', 'packaging'],
    noarchive=False,
)

pyz = PYZ(a.pure)

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
    icon=None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)