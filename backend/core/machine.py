# ============================================================
# 智能网关 — 机器码(HWID)采集
# Windows: CPU + 主板 + 磁盘序列号的组合哈希
# 抗伪造: 任一硬件变化会改变机器码; 组合多因子降低碰撞
# ============================================================

import hashlib
import logging
import platform
import subprocess

logger = logging.getLogger("gateway.machine")


def _run(cmd: list) -> str:
    """执行命令并返回 stdout 首行非空内容(去空白)"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in (r.stdout or "").splitlines():
            s = line.strip()
            if s:
                return s
    except Exception as e:
        logger.warning("machine cmd failed %s: %s", cmd, e)
    return ""


def _cpu_serial() -> str:
    return _run(["wmic", "cpu", "get", "ProcessorId"]) or _run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Processor).ProcessorId"])


def _board_serial() -> str:
    return _run(["wmic", "baseboard", "get", "SerialNumber"]) or _run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_BaseBoard).SerialNumber"])


def _disk_serial() -> str:
    return _run(["wmic", "diskdrive", "get", "SerialNumber"]) or _run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_DiskDrive).SerialNumber"])


def get_machine_code() -> str:
    """采集本机硬件信息并生成 HWID。

    返回形如 'HW-<40位hex>'; 非 Windows 或无数据时返回确定性 fallback。
    """
    parts = "|".join([
        platform.node(),
        _cpu_serial(),
        _board_serial(),
        _disk_serial(),
    ]).strip()
    digest = hashlib.sha256(parts.encode("utf-8", errors="ignore")).hexdigest()[:40]
    return f"HW-{digest}"


def machine_code_available() -> bool:
    """判断是否真正采集到硬件信息(非空), 用于 deciding 是否启用本地硬件锁定。"""
    return bool(_cpu_serial() or _board_serial() or _disk_serial())
