# Auto-detect common install paths for WorkBuddy and Trae Work CN
import os
from pathlib import Path

def find_workbuddy_exe():
    # Common locations
    candidates = [
        r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
        r"C:\Program Files (x86)\WorkBuddy\WorkBuddy.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # Optional: check registry uninstall string
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall") as key:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name) as sub:
                        try:
                            name, _ = winreg.QueryValueEx(sub, "DisplayName")
                            if "WorkBuddy" in name:
                                loc, _ = winreg.QueryValueEx(sub, "InstallLocation")
                                exe = os.path.join(loc, "WorkBuddy.exe")
                                if os.path.isfile(exe):
                                    return exe
                        except FileNotFoundError:
                            pass
                    i += 1
                except OSError:
                    break
    except Exception:
        pass
    return None

def find_trae_exe():
    candidates = [
        r"C:\Program Files\Trae Work CN\TraeWorkCN.exe",
        r"C:\Program Files (x86)\Trae Work CN\TraeWorkCN.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Trae Work CN\TraeWorkCN.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Trae Work CN\TraeWorkCN.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall") as key:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name) as sub:
                        try:
                            name, _ = winreg.QueryValueEx(sub, "DisplayName")
                            if "Trae" in name and "Work" in name:
                                loc, _ = winreg.QueryValueEx(sub, "InstallLocation")
                                exe = os.path.join(loc, "TraeWorkCN.exe")
                                if os.path.isfile(exe):
                                    return exe
                        except FileNotFoundError:
                            pass
                    i += 1
                except OSError:
                    break
    except Exception:
        pass
    return None