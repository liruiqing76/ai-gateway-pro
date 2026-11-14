# ============================================================
# 智能网关 — 机器码(HWID)采集
# Windows: 主机名 + CPU + 主板 + 内置磁盘序列号的组合哈希
# 抗伪造: 任一硬件变化会改变机器码; 组合多因子降低碰撞
#
# 稳定性设计(2026-10-24 事故后重写):
#   旧实现用 wmic 取"第一行非空值", 有三个坑:
#     ① Win11 24H2 起 wmic 已被移除 → 回退 PowerShell, 两条路径取到的字符串
#        (表头/大小写/空白)不同 → 同一台机器算出不同 HWID;
#     ② 多磁盘/多硬盘时"第一行"顺序不保证 → 插拔 U 盘都可能改变机器码;
#     ③ 子进程偶尔超时 → 某个分量为空 → 哈希完全不同。
#   结果就是: 账号绑定后被自己锁在门外("账号已绑定其他设备")。
#   现在改为**一次 PowerShell 取结构化 JSON**, 分量做规范化 + 排序 + 去重,
#   磁盘只取内置固定盘(排除 USB), 并且保留 `machine_code_candidates()`
#   兼容旧算法算出的历史机器码(命中即自动迁移到新码)。
# ============================================================

import hashlib
import json
import logging
import platform
import subprocess

logger = logging.getLogger("gateway.machine")

# 厂商占位串: 这些值等于"没填", 参与哈希只会引入噪声
_PLACEHOLDERS = {
    "", "NONE", "NULL", "N/A", "UNKNOWN", "NOT SPECIFIED", "NOT APPLICABLE",
    "TO BE FILLED BY O.E.M.", "TO BE FILLED BY OEM", "DEFAULT STRING",
    "SYSTEM SERIAL NUMBER", "0", "0000",
}

# 一次调用取齐三类硬件信息(减少子进程次数, 也避免多次调用间的不一致)
_CIM_SCRIPT = (
    "$o=[ordered]@{"
    "cpu=@(Get-CimInstance Win32_Processor | ForEach-Object { $_.ProcessorId });"
    "board=@(Get-CimInstance Win32_BaseBoard | ForEach-Object { $_.SerialNumber });"
    "disk=@(Get-CimInstance Win32_DiskDrive "
    "| Where-Object { $_.InterfaceType -ne 'USB' -and $_.MediaType -like '*Fixed*' } "
    "| ForEach-Object { $_.SerialNumber })"
    "}; $o | ConvertTo-Json -Compress"
)


def _norm(value) -> str:
    """规范化: 去空白 + 转大写; 厂商占位串视为空。"""
    s = str(value or "").strip().upper()
    return "" if s in _PLACEHOLDERS else s


def _run_json(script: str, timeout: float = 20.0):
    """跑一段 PowerShell, 把 stdout 当 JSON 解析; 失败一律返回 None(不抛)。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or "").strip()
        if not out:
            return None
        return json.loads(out)
    except Exception as e:  # 超时/权限/非 Windows: 降级为空, 不让机器码炸掉登录
        logger.warning("machine probe failed: %s", e)
        return None


def _as_list(value) -> list:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    return []


def _collect() -> dict:
    """采集硬件信息。

    返回 {"host": str, "cpu": [raw...], "board": [...], "disk": [...], "raw_*": [...]}
    —— `raw_*` 保留未规范化的原始串, 供旧机器码兼容计算使用。
    """
    data = _run_json(_CIM_SCRIPT) or {}
    raw = {k: [str(x).strip() for x in _as_list(data.get(k)) if str(x or "").strip()]
           for k in ("cpu", "board", "disk")}
    out = {"host": _norm(platform.node()),
           "host_raw": platform.node() or ""}
    for k, vals in raw.items():
        out[k] = sorted({n for n in (_norm(v) for v in vals) if n})
        out[f"raw_{k}"] = vals
    return out


def _digest(parts: list) -> str:
    joined = "|".join(parts)
    return "HW-" + hashlib.sha256(joined.encode("utf-8", errors="ignore")).hexdigest()[:40]


def _join(values) -> str:
    """排序去重后拼接: 分量顺序不影响结果(多盘/多路 CPU 时保持稳定)。"""
    return ",".join(sorted({str(v) for v in values if str(v)}))


def get_machine_code() -> str:
    """采集本机硬件信息并生成 HWID。

    返回形如 'HW-<40位hex>'; 非 Windows 或无任何硬件信息时, 退化为仅主机名参与。
    """
    c = _collect()
    return _digest([
        c["host"],
        _join(c["cpu"]),
        _join(c["board"]),
        _join(c["disk"]),
    ])


def machine_code_candidates() -> set:
    """本机可能算出的机器码集合(含旧算法结果), 用于兼容历史绑定。

    旧算法: 主机名 + CPU/主板/磁盘的"第一条原始值", 未规范化、未排序, 且允许
    某个分量因采集失败为空。这里把三类的"首条原始值 / 首条规范化值 / 空"做组合,
    就能覆盖绝大多数历史机器码; 至少要求 CPU 或主板之一存在, 不允许只有主机名
    (否则同主机名的机器就能互相顶替, 那就不叫设备绑定了)。
    """
    c = _collect()
    host = c["host"]

    # 归一化首条
    first_norm = {k: (sorted(c[k])[:1] or [""])[0] for k in ("cpu", "board", "disk")}
    # 原始首条(旧算法就是原样拼进哈希)
    first_raw = {k: (c[f"raw_{k}"][:1] or [""])[0] for k in ("cpu", "board", "disk")}

    codes = set()
    for source in (first_norm, first_raw):
        for cpu in {source["cpu"], ""}:
            for board in {source["board"], ""}:
                for disk in {source["disk"], ""}:
                    if not cpu and not board:
                        continue        # 只靠主机名不算设备身份
                    codes.add(_digest([host, cpu, board, disk]))
    # 旧算法用的是原始主机名(大小写/空白未处理)
    host_raw = c["host_raw"]
    if host_raw and host_raw != host:
        for source in (first_norm, first_raw):
            for cpu in {source["cpu"], ""}:
                for board in {source["board"], ""}:
                    for disk in {source["disk"], ""}:
                        if not cpu and not board:
                            continue
                        codes.add(_digest([host_raw, cpu, board, disk]))

    # 特例: 旧实现走 wmic 时, 取到的是**表头**而不是序列号
    # (`wmic cpu get ProcessorId` 的第一行非空内容就是 "ProcessorId"), 于是
    # 机器码 = hash(主机名 + "ProcessorId" + "SerialNumber" + "SerialNumber")。
    # 本机 2026-10-24 的账号绑定值正是这个形状(HW-ae1cb2b8…), 必须兼容,
    # 否则 Win11 移除 wmic 后老账号会被自己锁在门外。
    for h in {host, host_raw}:
        if not h:
            continue
        codes.add(_digest([h, "ProcessorId", "SerialNumber", "SerialNumber"]))
        codes.add(_digest([h, "ProcessorId", "SerialNumber", ""]))
        codes.add(_digest([h, "ProcessorId", "", "SerialNumber"]))
        codes.add(_digest([h, "ProcessorId", "", ""]))

    codes.add(get_machine_code())
    return codes


def machine_code_available() -> bool:
    """判断是否真正采集到硬件信息(非空), 用于决定是否启用本地硬件锁定。"""
    c = _collect()
    return bool(c["cpu"] or c["board"] or c["disk"])
