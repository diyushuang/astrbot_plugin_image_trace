"""命令控制器：承载各指令的处理流程。

`main.py` 只保留 Star 入口、服务组装、生命周期与命令注册；具体处理器
实现在本包内。控制器不反向依赖 `main`，所需依赖（插件实例与其服务、
配置门面）通过构造函数注入。
"""

from __future__ import annotations

from .admin import AdminController
from .checkin import CheckinController
from .original import OriginalController
from .random import RandomController
from .register import RegisterController
from .trace import TraceController

__all__ = [
    "AdminController",
    "CheckinController",
    "OriginalController",
    "RandomController",
    "RegisterController",
    "TraceController",
]
