# Auto-detect common install paths for WorkBuddy and Trae Work CN
# 首次搜索注册表+候选路径，找到后缓存到 detect_cache.json，后续直接读缓存
import os
import json
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parent / "detect_cache.json"


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_exe(path: str) -> str | None:
    return path if path and os.path.isfile(path) else None


def _search_registry(name_keyword: str, exe_names: list[str]) -> str | None:
    """搜索 HKLM + HKCU 注册表，返回找到的第一个 exe 路径"""
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall") as key:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub) as sk:
                            try:
                                name, _ = winreg.QueryValueEx(sk, "DisplayName")
                                if name_keyword.lower() in name.lower():
                                    loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                                    for exe in exe_names:
                                        p = _check_exe(os.path.join(loc, exe))
                                        if p:
                                            return p
                            except FileNotFoundError:
                                pass
                        i += 1
                    except OSError:
                        break
        except Exception:
            pass
    return None


def find_workbuddy_exe() -> str | None:
    cache = _load_cache()
    if "workbuddy" in cache:
        p = _check_exe(cache["workbuddy"])
        if p:
            return p

    candidates = [
        r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
        r"C:\Program Files (x86)\WorkBuddy\WorkBuddy.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe"),
    ]
    for p in candidates:
        if _check_exe(p):
            cache["workbuddy"] = p
            _save_cache(cache)
            return p

    p = _search_registry("WorkBuddy", ["WorkBuddy.exe"])
    if p:
        cache["workbuddy"] = p
        _save_cache(cache)
    return p


def find_trae_exe() -> str | None:
    cache = _load_cache()
    if "trae" in cache:
        p = _check_exe(cache["trae"])
        if p:
            return p

    candidates = [
        r"C:\Program Files\Trae Work CN\TraeWorkCN.exe",
        r"C:\Program Files (x86)\Trae Work CN\TraeWorkCN.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Trae Work CN\TraeWorkCN.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Trae Work CN\TraeWorkCN.exe"),
        r"D:\Programs\TRAE SOLO CN\TRAE SOLO CN.exe",
    ]
    for p in candidates:
        if _check_exe(p):
            cache["trae"] = p
            _save_cache(cache)
            return p

    p = _search_registry("Trae", ["TraeWorkCN.exe", "TRAE SOLO CN.exe", "Trae CN.exe"])
    if p:
        cache["trae"] = p
        _save_cache(cache)
    return p
