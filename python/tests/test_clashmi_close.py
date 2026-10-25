"""ClashMi 四步原子关闭测试。Windows API 一律 mock/注入。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clashmi_close
from clashmi_close import close_clashmi_atomic


class FakeClient:
    def __init__(self, base_url="http://127.0.0.1:7890", fail_tun=False):
        self.settings = SimpleNamespace(base_url=base_url)
        self.calls = []
        self.fail_tun = fail_tun

    def disable_tun(self):
        self.calls.append("disable_tun")
        if self.fail_tun:
            raise RuntimeError("TUN API 不可用")

    def close_connections(self):
        self.calls.append("close_connections")
        return 3


class FakeWorld:
    """有状态的进程世界：快照随强杀结果变化。"""

    def __init__(self, procs):
        self.procs = [dict(p) for p in procs]
        self.events = []

    def snapshot(self):
        return [dict(p) for p in self.procs]

    def tree_kill(self, pid, name=None):
        self.events.append(("kill", pid, name))
        self.procs = [p for p in self.procs if p.get("pid") != pid]
        return True, "killed"


def main_and_kernel():
    return [
        {"name": "ClashMi.exe", "pid": 100, "ppid": 1, "exe": r"C:\ClashMi\ClashMi.exe"},
        {"name": "mihomo.exe", "pid": 101, "ppid": 100, "exe": r"C:\ClashMi\mihomo.exe"},
    ]


class TestAtomicCloseLinux(unittest.TestCase):
    def test_full_flow_and_order(self):
        world = FakeWorld(main_and_kernel())
        events = []
        client = FakeClient()

        def sleep_fn(seconds):
            events.append(("sleep", seconds))

        def window_closer(pids):
            events.append(("window", tuple(pids)))
            return {"ok": True, "detail": "WM_CLOSE 已发"}

        def tree_kill(pid, name=None):
            events.append(("kill", pid))
            return world.tree_kill(pid, name)

        result = close_clashmi_atomic(
            client,
            sleep_fn=sleep_fn,
            snapshot_fn=world.snapshot,
            tree_kill_fn=tree_kill,
            window_closer_fn=window_closer,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.calls, ["disable_tun", "close_connections"])
        # 顺序：API -> 窗口 -> 睡眠 1.5 秒 -> 强杀。
        kinds = [e[0] for e in events]
        self.assertEqual(kinds, ["window", "sleep", "kill", "kill"])
        self.assertEqual(events[1], ("sleep", 1.5))
        self.assertEqual([s["step"] for s in result["steps"]],
                         ["api_graceful", "close_main_window", "kill_process_tree", "proxy_cleanup"])
        # 非 Windows 下代理步骤跳过且不计失败。
        self.assertTrue(result["steps"][3]["detail"]["skipped"])
        self.assertTrue(result["details"]["main_gone"])
        self.assertTrue(result["details"]["proxy_clean"])

    def test_api_failure_does_not_abort_later_steps(self):
        world = FakeWorld(main_and_kernel())
        client = FakeClient(fail_tun=True)
        calls = []

        result = close_clashmi_atomic(
            client,
            sleep_fn=lambda s: None,
            snapshot_fn=world.snapshot,
            tree_kill_fn=lambda pid, name=None: world.tree_kill(pid, name),
            window_closer_fn=lambda pids: (calls.append("window"), {"ok": True})[1],
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["steps"][0]["ok"])  # API 步记失败
        self.assertIn("window", calls)
        self.assertEqual(world.procs, [])

    def test_main_survives_after_kill_is_failure(self):
        world = FakeWorld(main_and_kernel())
        client = FakeClient()

        def failing_kill(pid, name=None):
            return False, "拒绝访问"

        result = close_clashmi_atomic(
            client,
            sleep_fn=lambda s: None,
            snapshot_fn=world.snapshot,
            tree_kill_fn=failing_kill,
            window_closer_fn=lambda pids: {"ok": True},
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["details"]["main_gone"])

    def test_unrelated_kernel_not_killed(self):
        world = FakeWorld([
            {"name": "ClashMi.exe", "pid": 100, "ppid": 1, "exe": r"C:\ClashMi\ClashMi.exe"},
            {"name": "mihomo.exe", "pid": 200, "ppid": 1, "exe": r"C:\OtherClient\mihomo.exe"},
        ])
        client = FakeClient()
        result = close_clashmi_atomic(
            client,
            sleep_fn=lambda s: None,
            snapshot_fn=world.snapshot,
            tree_kill_fn=lambda pid, name=None: world.tree_kill(pid, name),
            window_closer_fn=lambda pids: {"ok": True},
        )
        # 主进程已消失；无关 mihomo 被跳过（不在 killed 里，仍在世界中）。
        self.assertTrue(result["details"]["main_gone"])
        remaining = [p["pid"] for p in world.procs]
        self.assertIn(200, remaining)
        kill_step = result["steps"][2]
        self.assertTrue(any(s["pid"] == 200 for s in kill_step["detail"]["skipped_kernel"]))


class TestProxyCleanupWindowsSimulated(unittest.TestCase):
    """模拟 Windows：注入注册表读写，验证代理兜底条件。"""

    def _run(self, proxy_state, client_url="http://127.0.0.1:7890"):
        world = FakeWorld(main_and_kernel())
        client = FakeClient(base_url=client_url)
        state = dict(proxy_state)
        writer_calls = []

        def reader():
            return {"enabled": state["enabled"], "server": state["server"]}

        def writer():
            writer_calls.append(1)
            state["enabled"] = 0
            return True, "ProxyEnable 已置 0"

        with mock.patch.object(clashmi_close, "IS_WINDOWS", True):
            result = close_clashmi_atomic(
                client,
                sleep_fn=lambda s: None,
                snapshot_fn=world.snapshot,
                tree_kill_fn=lambda pid, name=None: world.tree_kill(pid, name),
                window_closer_fn=lambda pids: {"ok": True},
                proxy_reader_fn=reader,
                proxy_writer_fn=writer,
                system_proxy_off_fn=lambda: (True, "系统代理已关"),
            )
        return result, writer_calls, state

    def test_dirty_clashmi_proxy_is_cleaned(self):
        result, writer_calls, state = self._run({"enabled": 1, "server": "127.0.0.1:7890"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(writer_calls)  # 兜底确实写了注册表
        self.assertEqual(state["enabled"], 0)
        self.assertTrue(result["details"]["proxy_clean"])

    def test_non_clashmi_proxy_is_left_alone(self):
        result, writer_calls, state = self._run({"enabled": 1, "server": "10.0.0.5:8080"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(writer_calls, [])
        self.assertEqual(state["enabled"], 1)  # 他人代理不动
        self.assertTrue(result["details"]["proxy_clean"])

    def test_cleanup_write_failure_is_failure(self):
        world = FakeWorld(main_and_kernel())
        client = FakeClient()

        def failing_writer():
            return False, "注册表拒绝写入"

        with mock.patch.object(clashmi_close, "IS_WINDOWS", True):
            result = close_clashmi_atomic(
                client,
                sleep_fn=lambda s: None,
                snapshot_fn=world.snapshot,
                tree_kill_fn=lambda pid, name=None: world.tree_kill(pid, name),
                window_closer_fn=lambda pids: {"ok": True},
                proxy_reader_fn=lambda: {"enabled": 1, "server": "127.0.0.1:7890"},
                proxy_writer_fn=failing_writer,
                system_proxy_off_fn=lambda: (True, "系统代理已关"),
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["details"]["proxy_clean"])


class TestProxyPortSelectionP0(unittest.TestCase):
    """P0-5：代理兜底端口必须来自 /configs 代理字段，而非 External Controller。"""

    def _client(self, base_url, configs):
        client = FakeClient(base_url=base_url)

        def get_configs():
            return dict(configs)

        client.get_configs = get_configs
        return client

    def _run(self, client, proxy_server):
        world = FakeWorld(main_and_kernel())
        state = {"enabled": 1, "server": proxy_server}
        writer_calls = []

        def reader():
            return {"enabled": state["enabled"], "server": state["server"]}

        def writer():
            writer_calls.append(1)
            state["enabled"] = 0
            return True, "ProxyEnable 已置 0"

        with mock.patch.object(clashmi_close, "IS_WINDOWS", True):
            result = close_clashmi_atomic(
                client,
                sleep_fn=lambda s: None,
                snapshot_fn=world.snapshot,
                tree_kill_fn=lambda pid, name=None: world.tree_kill(pid, name),
                window_closer_fn=lambda pids: {"ok": True},
                proxy_reader_fn=reader,
                proxy_writer_fn=writer,
                system_proxy_off_fn=lambda: (True, "系统代理已关"),
            )
        return result, writer_calls, state

    def test_controller_9090_proxy_7890_is_still_cleaned(self):
        # External Controller 9090，代理混合端口 7890：兜底必须识别 7890。
        client = self._client("http://127.0.0.1:9090", {"mixed-port": 7890})
        result, writer_calls, state = self._run(client, "127.0.0.1:7890")
        self.assertTrue(result["ok"], result)
        self.assertTrue(writer_calls)
        self.assertEqual(state["enabled"], 0)

    def test_socks_port_is_accepted_as_clashmi_proxy_port(self):
        client = self._client("http://127.0.0.1:9090", {"socks-port": 7891})
        result, writer_calls, state = self._run(client, "127.0.0.1:7891")
        self.assertTrue(result["ok"], result)
        self.assertTrue(writer_calls)

    def test_snapshot_unavailable_is_not_fake_success(self):
        client = self._client("http://127.0.0.1:9090", {"mixed-port": 7890})
        world = FakeWorld(main_and_kernel())
        with mock.patch.object(clashmi_close, "IS_WINDOWS", True):
            result = close_clashmi_atomic(
                client,
                sleep_fn=lambda s: None,
                snapshot_fn=lambda: None,
                tree_kill_fn=lambda pid, name=None: (True, "killed"),
                window_closer_fn=lambda pids: {"ok": True},
                proxy_reader_fn=lambda: {"enabled": 0, "server": None},
                proxy_writer_fn=lambda: (True, "disabled"),
                system_proxy_off_fn=lambda: (True, "系统代理已关"),
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["details"]["main_gone"])
        self.assertFalse(result["details"]["kernel_gone"])
        steps = {s["step"]: s for s in result["steps"]}
        self.assertIn("final_verify", steps)
        self.assertFalse(steps["final_verify"]["ok"])


if __name__ == "__main__":
    unittest.main()
