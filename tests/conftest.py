# ============================================================
# pytest 全局配置
# 默认关闭设备绑定校验(LICENSE_ENABLED=0), 避免单机单账号限制
# 干扰其它测试; 测试 license 功能的用例再自行 monkeypatch 开启。
# ============================================================
import os

os.environ.setdefault("GW_LICENSE_ENABLED", "0")
