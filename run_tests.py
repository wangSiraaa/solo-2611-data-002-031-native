"""测试运行器：把会 SIGKILL 进程、触发集群 WAL 恢复的崩溃演练放到最后。

其余测试模块按顺序先跑；崩溃测试可能让集群短暂进入 recovery mode，
连接侧由 connect_with_recovery_retry 自动等待恢复完成。
"""

from __future__ import annotations

import sys
import unittest

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    normal_modules = [
        "tests.test_reference",
        "tests.test_graphs",
        "tests.test_idempotency",
        "tests.test_concurrency",
        "tests.test_synthetic_cli",
    ]
    for name in normal_modules:
        suite.addTests(loader.loadTestsFromName(name))
    # 崩溃演练最后运行
    suite.addTests(loader.loadTestsFromName("tests.test_crash_recovery"))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
