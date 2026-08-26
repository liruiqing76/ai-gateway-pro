#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建混淆版 exe: 把敏感核心模块编译为原生 .pyd 再打包。

流程:
  1. 清空 build_src/ staging 目录
  2. 复制 backend/ + desktop/ 源码树（跳过 data/logs/shots/__pycache__/二进制）
  3. Cython 编译 20 个核心模块 → .pyd（build_ext --inplace）
  4. 删除这 20 个模块的 .py 和 .c（保留 .pyd）
  5. 逐个 import .pyd 做物化校验
  6. 设 GW_SRC_ROOT=build_src 跑 PyInstaller（spec 据此从 staging 收集）

真实源码树零改动；不带 GW_SRC_ROOT 的普通打包仍走明文源码。

用法: ./.venv/Scripts/python.exe scripts/build_exe.py
      --stage-only    只做 1-5（staging 就绪，不打包）
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
VENV_PY = BASE / ".venv" / "Scripts" / "python.exe"
STAGING = BASE / "build_src"
SETUP_TEMPLATE = BASE / "scripts" / "setup_cython.py"

# 复制时跳过的子目录名
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "data", "shots", "logs",
                "cython_build", "build"}
# 复制时跳过的文件后缀
EXCLUDE_SUFFIXES = (".pyc", ".pyd", ".c", ".obj", ".exp", ".lib", ".log", ".o")
# 跳过 .db 及 .db.bak

# 编译后要删除 .py/.c 的模块（与 setup_cython.py MODULES 一致）
COMPILE_REL = [
    "backend/core/license.py", "backend/core/machine.py",
    "backend/core/proxy.py", "backend/core/orchestrate.py",
    "backend/core/experience.py", "backend/core/circuit.py",
    "backend/core/chat_archive.py", "backend/core/chat_log.py",
    "backend/core/router_group.py", "backend/core/scheduler.py",
    "backend/core/zen_sources.py", "backend/core/free_sources.py",
    "backend/core/vision.py", "backend/core/trace.py",
    "backend/auth/security.py", "backend/auth/middleware.py",
    "backend/billing/plans.py",
    # config.py/main.py 不编译: Analysis 通过它们发现 pydantic_settings 等传递依赖
    "desktop/scheduler.py",
]


def _skip_dir(name: str) -> bool:
    return name in EXCLUDE_DIRS


def _skip_file(name: str) -> bool:
    return name.endswith(EXCLUDE_SUFFIXES) or name.endswith(".db") or ".db.bak" in name


def _copy_tree(src: Path, dst: Path) -> int:
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if _skip_file(f):
                continue
            shutil.copy2(Path(root) / f, dst / rel / f)
            n += 1
    return n


def stage() -> None:
    """1-4: 清空 → 复制 → 编译 → 删源码"""
    if not VENV_PY.exists():
        sys.exit(f"找不到 venv python: {VENV_PY}")

    if STAGING.exists():
        shutil.rmtree(STAGING, ignore_errors=True)
    STAGING.mkdir(parents=True)
    print(f"[1] staging 已清空: {STAGING}")

    n = 0
    for sub in ("backend", "desktop"):
        src = BASE / sub
        if src.exists():
            n += _copy_tree(src, STAGING / sub)
    print(f"[2] 已复制源码树 {n} 个文件")

    shutil.copy2(SETUP_TEMPLATE, STAGING / "setup_cython.py")
    print(f"[3] 开始 Cython 编译 20 个核心模块...")
    r = subprocess.run(
        [str(VENV_PY), "setup_cython.py", "build_ext", "--inplace"],
        cwd=str(STAGING),
    )
    if r.returncode != 0:
        sys.exit("ERROR: Cython 编译失败 (MSVC 工具链？)")
    # 清理编译中间产物
    for d in ("cython_build", "build"):
        p = STAGING / d
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    deleted = 0
    for rel in COMPILE_REL:
        py = STAGING / rel
        c = py.with_suffix(".c")
        if py.exists():
            py.unlink()
            deleted += 1
        if c.exists():
            c.unlink()
    print(f"[4] 已删除 {deleted} 个 .py 源 + 其 .c（保留 .pyd）")

    # scheduler 是顶层 Extension(build_ext --inplace 把它放在 staging 根),
    # 但运行时靠 desktop/ 在 sys.path 上导入, 必须移回 desktop/。
    for sched in STAGING.glob("scheduler.*.pyd"):
        target = STAGING / "desktop" / sched.name
        if sched.resolve() != target.resolve():
            shutil.move(str(sched), str(target))
            print(f"    scheduler.pyd 移回 desktop/")


def verify_imports() -> bool:
    """5: 逐个 import .pyd 做物化校验"""
    print("[5] 逐个 import .pyd 校验...")
    # 与 setup_cython.py 的 MODULES 一致
    imports = [
        "backend.core.license", "backend.core.machine", "backend.core.proxy",
        "backend.core.orchestrate", "backend.core.experience",
        "backend.core.circuit", "backend.core.chat_archive",
        "backend.core.chat_log", "backend.core.router_group",
        "backend.core.scheduler", "backend.core.zen_sources",
        "backend.core.free_sources", "backend.core.vision",
        "backend.core.trace", "backend.auth.security",
        "backend.auth.middleware", "backend.billing.plans",
        # backend.config / backend.main 未编译, 由 Analysis + datas 交付
        "scheduler",
    ]
    ok = True
    for mod in imports:
        code = (
            "import sys; sys.path.insert(0, r'{stg}'); "
            "sys.path.insert(0, r'{stg}\\desktop'); "
            "import importlib; importlib.import_module('{m}'); "
            "print('OK {m}')".format(stg=str(STAGING), m=mod)
        )
        r = subprocess.run([str(VENV_PY), "-c", code],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  FAIL {mod}\n{r.stderr.strip()}")
            ok = False
        else:
            print(f"  {r.stdout.strip()}")
    return ok


def build_pyinstaller() -> None:
    """6: 设 GW_SRC_ROOT 跑 PyInstaller"""
    print(f"[6] PyInstaller 打包 (GW_SRC_ROOT={STAGING})...")
    env = os.environ.copy()
    env["GW_SRC_ROOT"] = str(STAGING)
    r = subprocess.run(
        [str(VENV_PY), "-m", "PyInstaller",
         str(BASE / "ai_gateway_pro.spec"), "--noconfirm",
         "--distpath", str(BASE / "dist"),
         "--workpath", str(BASE / "build")],
        cwd=str(BASE), env=env,
    )
    if r.returncode != 0:
        sys.exit("ERROR: PyInstaller 打包失败")
    print("\n✅ 构建完成: dist/智能网关.exe")


def main():
    stage_only = "--stage-only" in sys.argv
    stage()
    if not verify_imports():
        sys.exit("ERROR: .pyd 导入校验未全部通过，已中止打包")
    if stage_only:
        print("\n✅ staging 就绪（--stage-only，跳过打包）")
        return
    build_pyinstaller()


if __name__ == "__main__":
    main()
