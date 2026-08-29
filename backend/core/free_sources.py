#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
免费源自动同步 — 对齐 CX-AIRouter 的"每6小时扫描全网免费源"玩法。

原理:
  1. 本地基线: backend/data/provider_templates.json (内置免费厂商清单)
  2. 外部源: backend/data/free_sources.json 里 enabled 的 URL, 每个 URL 是一个
     与模板同格式的 JSON 列表 [{name, base_url, models, free_only, ...}]
  3. 合并: 按 name 去重, models 求并集, 结果写回 provider_templates.json
     -> "添加渠道"模板池自动多出新发现的服务商, 无需改任何用户数据
  4. 容错: 单个外部源拉取失败不影响其它源与本地模板

提供手动同步入口 sync_free_sources(), 被定时任务和 API 共用。
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger("gateway")

# 内置资源目录: 源码模式 = backend/data; 打包 onefile 模式 = 临时解压目录里的
# backend/data(ai_gateway_pro.spec 显式打进 .json 静态资源)。仅作 seed 基线,
# 运行时禁止写回此路径(onefile 退出即销毁, 写回会丢)。
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BUILTIN_TEMPLATES_FILE = DATA_DIR / "provider_templates.json"
BUILTIN_SOURCES_FILE = DATA_DIR / "free_sources.json"
# 兼容旧引用(指向内置基线)
TEMPLATES_FILE = BUILTIN_TEMPLATES_FILE
SOURCES_FILE = BUILTIN_SOURCES_FILE

_sync_lock = threading.Lock()
_runtime_state = {"last_sync_at": None, "last_result": None}  # 内存态, 供 /api/free-sources 展示


def runtime_data_dir() -> Path:
    """持久化数据目录: 桌面模式(GW_DB_PATH)用 exe 旁的 data/ 与 gateway.db 同级;
    源码模式退回内置 DATA_DIR。模板/免费源配置在此读写, 重启不丢且同步可写回。"""
    gw = os.environ.get("GW_DB_PATH")
    if gw:
        d = Path(gw).resolve().parent
        d.mkdir(parents=True, exist_ok=True)
        return d
    return DATA_DIR


def runtime_templates_file() -> Path:
    return runtime_data_dir() / "provider_templates.json"


def runtime_sources_file() -> Path:
    return runtime_data_dir() / "free_sources.json"


def _seed_from_builtin(dst: Path, builtin: Path) -> bool:
    """持久化文件缺失时从打包内置基线拷一份(首次启动 seed)。返回是否新建。
    builtin 不存在(打包漏带静态资源)时静默返回 False, 由调用方 load 返回 [] 兜底, 不崩。"""
    if dst.exists() or not builtin.exists():
        return False
    try:
        import shutil
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(builtin), str(dst))
        logger.info("seed 持久化资源 %s <- 内置基线", dst.name)
        return True
    except Exception as e:
        logger.warning("seed %s 失败: %s", dst, e)
        return False


def ensure_runtime_assets() -> None:
    """启动/首次读取前确保持久化模板与免费源配置存在(从内置基线 seed)。
    桌面打包版: 把 spec 打进包的内置 .json 拷到 exe 旁 data/, 保证模板一键添加可用。"""
    _seed_from_builtin(runtime_templates_file(), BUILTIN_TEMPLATES_FILE)
    _seed_from_builtin(runtime_sources_file(), BUILTIN_SOURCES_FILE)


def load_templates(templates_path: Path = None) -> list:
    """读取本地模板池。默认读持久化运行时文件(桌面模式 = exe 旁 data/),
    缺失时先从内置基线 seed 一次; 显式传 templates_path 时(测试用)直接读它。"""
    path = templates_path or runtime_templates_file()
    if not path.exists() and not templates_path:
        _seed_from_builtin(path, BUILTIN_TEMPLATES_FILE)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取模板池失败 %s: %s", path, e)
        return []


def load_source_config(sources_path: Path = None) -> dict:
    """读取外部免费源配置 {sources: [{name,url,enabled}], meta: {...}}。"""
    path = sources_path or runtime_sources_file()
    if not path.exists() and not sources_path:
        _seed_from_builtin(path, BUILTIN_SOURCES_FILE)
    if not path.exists():
        return {"sources": [], "meta": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取免费源配置失败 %s: %s", path, e)
        return {"sources": [], "meta": {}}


def save_source_config(cfg: dict, sources_path: Path = None):
    path = sources_path or runtime_sources_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def fetch_source_list(url: str, timeout: float = 15.0) -> list:
    """拉取一个外部免费源, 返回模板格式的条目列表。支持顶层 list 或 {"sources":[...]}。"""
    resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                     headers={"User-Agent": "ai-gateway-pro/free-source-sync"})
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        return data["sources"]
    raise ValueError(f"无法识别的源格式: {url}")


def _merge_into_pool(pool: list, entries: list) -> dict:
    """把一批条目合并进模板池(按 name 去重, models 求并集)。返回 (pool, added, updated)。"""
    added, updated = [], []
    index = {t["name"]: i for i, t in enumerate(pool)}
    for e in entries:
        if not isinstance(e, dict) or not e.get("name") or not e.get("base_url"):
            continue
        name = e["name"]
        models = [m for m in (e.get("models") or []) if isinstance(m, str)]
        if name in index:
            i = index[name]
            old_models = set(pool[i].get("models") or [])
            new_models = [m for m in models if m not in old_models]
            if new_models:
                pool[i]["models"] = (pool[i].get("models") or []) + new_models
                updated.append(name)
            # 保留本地已有字段, 外部源只补 models 与自由字段
        else:
            pool.append({
                "name": name,
                "base_url": e["base_url"],
                "models": models,
                "free_only": e.get("free_only", True),
                "signup_url": e.get("signup_url"),
                "config_url": e.get("config_url"),
                "notes": e.get("notes"),
                "tags": e.get("tags") or ["免费", "自动发现"],
            })
            index[name] = len(pool) - 1
            added.append(name)
    return pool, added, updated


def sync_free_sources(templates_path: Path = None, sources_path: Path = None) -> dict:
    """执行一次完整免费源同步(线程安全)。返回统计结果, 可通过 API 暴露。"""
    with _sync_lock:
        pool = load_templates(templates_path)
        base_names = {t["name"] for t in pool}
        cfg = load_source_config(sources_path)
        result = {
            "ok": True,
            "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_templates": 0,
            "added": [],
            "updated": [],
            "failed_sources": [],
        }
        enabled = [s for s in cfg.get("sources", []) if s.get("enabled") and s.get("url")]
        for source in enabled:
            url = source["url"]
            name = source.get("name") or url
            try:
                entries = fetch_source_list(url)
                pool, added, updated = _merge_into_pool(pool, entries)
                result["added"].extend(added)
                result["updated"].extend(updated)
                logger.info("免费源 %s 同步完成: +%d 新增, %d 更新", name, len(added), len(updated))
            except Exception as e:
                result["failed_sources"].append(f"{name}: {e}")
                logger.warning("免费源 %s 拉取失败: %s", name, e)

        # 写回模板池(仅当有新内容或外部源配置存在时也刷新 meta)
        path = templates_path or runtime_templates_file()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

        result["total_templates"] = len(pool)
        result["base_templates"] = len(base_names)
        result["added"] = list(dict.fromkeys(result["added"]))  # 去重保序
        result["updated"] = list(dict.fromkeys(result["updated"]))

        # 更新源配置的 meta(持久化上次同步信息)
        cfg.setdefault("meta", {})
        cfg["meta"]["last_sync_at"] = result["synced_at"]
        cfg["meta"]["last_result"] = {
            "total_templates": result["total_templates"],
            "added": result["added"],
            "updated": result["updated"],
            "failed_sources": result["failed_sources"],
        }
        save_source_config(cfg, sources_path)

        _runtime_state["last_sync_at"] = result["synced_at"]
        _runtime_state["last_result"] = result
        return result


def runtime_state() -> dict:
    return _runtime_state