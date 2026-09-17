# -*- coding: utf-8 -*-
"""Cython 扩展编译定义（由 build_exe.py 拷进 staging 后执行）。

把 20 个敏感核心模块编译为原生 .pyd；ORM 模型/Pydantic 路由/入口/checkin
脚本不在此列（保持 .py，原因见计划）。build_ext --inplace 把 .pyd 放到与
.py 同目录，编译后由 build_exe.py 删除 .py/.c。
"""
from setuptools import setup, Extension
from Cython.Build import cythonize

# (Extension 模块名, 源文件相对 staging 根的路径)
# 模块名决定 PyInit_xxx 符号名 = 最后一段；desktop/scheduler 顶层导入故命名为 scheduler。
MODULES = [
    ("backend.core.license", "backend/core/license.py"),
    ("backend.core.machine", "backend/core/machine.py"),
    ("backend.core.proxy", "backend/core/proxy.py"),
    ("backend.core.orchestrate", "backend/core/orchestrate.py"),
    ("backend.core.experience", "backend/core/experience.py"),
    ("backend.core.circuit", "backend/core/circuit.py"),
    ("backend.core.chat_archive", "backend/core/chat_archive.py"),
    ("backend.core.chat_log", "backend/core/chat_log.py"),
    ("backend.core.router_group", "backend/core/router_group.py"),
    ("backend.core.scheduler", "backend/core/scheduler.py"),
    ("backend.core.zen_sources", "backend/core/zen_sources.py"),
    ("backend.core.free_sources", "backend/core/free_sources.py"),
    ("backend.core.vision", "backend/core/vision.py"),
    ("backend.core.trace", "backend/core/trace.py"),
    ("backend.core.dead_model_recovery", "backend/core/dead_model_recovery.py"),
    ("backend.auth.security", "backend/auth/security.py"),
    ("backend.auth.middleware", "backend/auth/middleware.py"),
    ("backend.billing.plans", "backend/billing/plans.py"),
    # config.py / main.py 不编译 — 它们是导入树根，Analysis 需要读 .py
    # 才能发现 pydantic_settings 等传递依赖；编译后 Analysis 断链导致缺模块。
    ("scheduler", "desktop/scheduler.py"),
]

extensions = [Extension(name, [src]) for name, src in MODULES]

setup(
    name="ai-gateway-pro-core",
    ext_modules=cythonize(
        extensions,
        compiler_directives={"language_level": "3"},
        build_dir="cython_build",
    ),
    zip_safe=False,
)
