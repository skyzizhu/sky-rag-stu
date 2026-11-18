"""验证保险在真正写文件/连接服务之前拒绝操作。"""
import socket
import os
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.config import PROJECT_ROOT
from tests._safety import TestIsolationError


class TestSafetyTests(unittest.TestCase):
    def test_production_writes_are_blocked(self):
        for path in ('storage/index_state.json', 'knowledge/real.md', 'settings.json'):
            with self.subTest(path=path), self.assertRaises(TestIsolationError):
                (PROJECT_ROOT / path).write_text('must not be written')

    def test_production_delete_and_sqlite_are_blocked(self):
        with self.assertRaises(TestIsolationError):
            (PROJECT_ROOT / 'storage/index_state.json').unlink()
        with self.assertRaises(TestIsolationError):
            sqlite3.connect(PROJECT_ROOT / 'storage/auth/auth.db')

    def test_network_is_blocked(self):
        with socket.socket() as client, self.assertRaises(TestIsolationError):
            client.connect(('127.0.0.1', 6333))

    def test_temporary_files_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text('{}')
            self.assertEqual(path.read_text(), '{}')
            path.unlink()

    def test_child_process_is_blocked(self):
        with self.assertRaises(TestIsolationError):
            subprocess.run([sys.executable, '-c', 'pass'], check=True)

    def test_relative_delete_respects_directory_fd(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'knowledge'
            path.mkdir()
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.rmdir('knowledge', dir_fd=fd)
                self.assertFalse(path.exists())
            finally:
                os.close(fd)
        fd = os.open(PROJECT_ROOT / 'storage', os.O_RDONLY)
        try:
            with self.assertRaises(TestIsolationError):
                os.remove('index_state.json', dir_fd=fd)
        finally:
            os.close(fd)
