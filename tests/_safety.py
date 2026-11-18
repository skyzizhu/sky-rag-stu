"""测试进程的统一保险：禁止写正式数据目录，禁止连接网络服务。

由 tests 包自动安装，夹具漏 patch 时直接失败。内存 Qdrant 与系统临时目录仍可用。
同时禁止启动子进程，避免通过外部命令绕过当前进程的保护。
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import sys

from src.config import PROJECT_ROOT, get_config


class TestIsolationError(RuntimeError):
    """测试试图接触正式数据或网络服务。"""


_PROTECTED = tuple(p.resolve() for p in (
    PROJECT_ROOT / 'storage', PROJECT_ROOT / 'knowledge', get_config().knowledge_dir,
    PROJECT_ROOT / 'settings.json', PROJECT_ROOT / '.env',
))


def _check_path(value, dir_fd=None):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(value))
    if not path.is_absolute() and dir_fd is not None and dir_fd != -1:
        if sys.platform == 'darwin':
            import fcntl
            base = os.fsdecode(fcntl.fcntl(dir_fd, fcntl.F_GETPATH, b'\0' * 1024).split(b'\0', 1)[0])
        else:
            base = os.readlink(f'/proc/self/fd/{dir_fd}')
        path = Path(base) / path
    path = path.resolve()
    if any(path == root or root in path.parents for root in _PROTECTED):
        raise TestIsolationError(f'测试禁止修改正式数据：{path}；请使用临时目录夹具')


def _audit(event, args):
    if event == 'open':
        path, mode, flags = args
        if (mode and any(c in mode for c in 'wax+')) or (
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        ):
            _check_path(path)
    elif event in ('os.remove', 'os.rmdir'):
        _check_path(args[0], args[1])
    elif event in ('os.mkdir', 'os.chmod'):
        _check_path(args[0], args[2])
    elif event in ('os.truncate', 'sqlite3.connect'):
        _check_path(args[0])
    elif event in ('os.rename', 'os.link'):
        _check_path(args[0], args[2])
        _check_path(args[1], args[3])
    elif event == 'os.symlink':
        _check_path(args[1], args[2])
    elif event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.exec'):
        raise TestIsolationError('测试禁止启动外部进程；请 mock 外部命令')
    elif event == 'socket.connect' and args[0].family in (socket.AF_INET, socket.AF_INET6):
        raise TestIsolationError('测试禁止连接网络服务；请使用内存数据库或 mock 客户端')


def install():
    if not getattr(sys, '_sky_test_safety_installed', False):
        sys.addaudithook(_audit)
        sys._sky_test_safety_installed = True
