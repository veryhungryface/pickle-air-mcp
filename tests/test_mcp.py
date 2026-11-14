import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pickle_mcp.server import (
    Adapter, AskInput, DoctorInput, DownloadInput, ReadInput, Settings,
    SafeError, call_lock, run_cli,
)

PACKAGE = Path(__file__).resolve().parents[1]


class ValidationTests(unittest.TestCase):
    def test_strict_arguments(self):
        invalid = [
            {"agent": "A", "message": "x", "request_id": "ok", "timeout": True},
            {"agent": "A", "message": "x", "request_id": "ok", "timeout": "120"},
            {"agent": "A", "message": "x", "request_id": "ok", "timeout": 601},
            {"agent": "A", "message": "x", "request_id": "ok", "timeout": 9},
            {"agent": "A", "message": "x", "request_id": "../bad"},
            {"agent": "A", "message": "x", "request_id": "ok\n"},
            {"agent": "A", "message": "x", "request_id": "x" * 81},
            {"agent": "A", "message": "x", "request_id": "ok", "attachments": ["../x.txt"]},
            {"agent": "A", "message": "x", "request_id": "ok", "attachments": ["x.txt\n"]},
            {"agent": "A", "message": "x", "request_id": "ok", "attachments": ["x.txt"] * 5},
            {"agent": "A", "message": "x", "request_id": "ok", "port": 6666},
            {"agent": "A", "message": "x", "request_id": "ok", "host": "example.org"},
            {"agent": "A", "message": "x", "request_id": "ok", "command": "shell"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AskInput.model_validate(value)

    def test_invalid_message(self):
        for message in ["", " ", "a\0b", "x" * 12001]:
            with self.assertRaises(ValidationError):
                AskInput(agent="A", message=message, request_id="test")

    def test_korean_newline_and_shell_like_text_are_data(self):
        m = "한글\n$(touch /tmp/not-run); hello"
        self.assertEqual(AskInput(agent="도우미", message=m, request_id="test").message, m)

    def test_control_in_agent_rejected(self):
        for agent in ["", " ", "A\nB", "A\0B", "x" * 121]:
            with self.assertRaises(ValidationError):
                ReadInput(agent=agent)

    def test_settings_bounds(self):
        for port in [0, 1023, 65536, True]:
            with self.assertRaises(ValueError):
                Settings(agents=("A",), port=port)
        with self.assertRaises(ValueError):
            Settings(agents=())

    def test_doctor_rejects_unknown_args(self):
        with self.assertRaises(ValidationError):
            DoctorInput(host="remote")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home_patch = patch("pathlib.Path.home", return_value=self.root / "home")
        self.home_patch.start()
        self.settings = Settings(agents=("A",), state_dir=self.root / "state",
                                 upload_dir=self.root / "upload", download_dir=self.root / "downloads",
                                 enable_ask=True)
        self.calls = []
        def runner(argv, timeout):
            self.calls.append((argv, timeout))
            return 0, {"state": "completed"}
        self.adapter = Adapter(self.settings, runner)

    async def asyncTearDown(self):
        self.home_patch.stop()
        self.temp.cleanup()

    def ask(self, **extra):
        return AskInput(agent="A", message="synthetic message", request_id="one", **extra)

    async def test_sending_is_opt_in(self):
        settings = Settings(agents=("A",), state_dir=self.root / "state")
        answer = await Adapter(settings, self.adapter.runner).invoke("ask", self.ask())
        self.assertTrue(answer.isError)
        self.assertEqual(self.calls, [])

    async def test_call_needs_its_own_opt_in_and_uses_ask_path(self):
        from pickle_mcp.server import CallInput
        from pydantic import ValidationError
        call = CallInput(agent="A", target="number", number="010-1234-5678", purpose="synthetic purpose", request_id="call1")
        blocked = await self.adapter.invoke("call", call)
        self.assertTrue(blocked.isError)
        self.assertEqual(self.calls, [])
        settings = Settings(agents=("A",), state_dir=self.root / "state", enable_ask=True, enable_call=True)
        answer = await Adapter(settings, self.adapter.runner).invoke("call", call)
        self.assertFalse(answer.isError)
        argv, _ = self.calls[0]
        self.assertEqual(argv[4], "ask")
        self.assertIn("--quiet", argv)
        message = next(a for a in argv if a.startswith("--message="))
        self.assertIn("010-1234-5678", message)
        self.assertIn("synthetic purpose", message)
        again = await Adapter(settings, self.adapter.runner).invoke("call", call)
        self.assertTrue(again.isError)  # same request ID is never sent twice
        self.assertEqual(len(self.calls), 1)
        for bad in [dict(target="me", number="010-1234-5678"), dict(target="number"), dict(target="contact"),
                    dict(target="number", number="call mom"), dict(target="contact", contact_name="a\nb")]:
            with self.assertRaises(ValidationError):
                CallInput(agent="A", purpose="x", request_id="r", **bad)

    async def test_agent_allowlist(self):
        answer = await self.adapter.invoke("read", ReadInput(agent="Other"))
        self.assertTrue(answer.isError)
        self.assertEqual(self.calls, [])

    async def test_duplicate_request_id_blocked_after_success(self):
        first = await self.adapter.invoke("ask", self.ask())
        second = await self.adapter.invoke("ask", self.ask())
        self.assertFalse(first.isError)
        self.assertTrue(second.isError)
        self.assertEqual(len(self.calls), 1)

    async def test_duplicate_blocked_after_timeout_and_restart(self):
        def uncertain(argv, timeout):
            self.calls.append(argv)
            return 124, {"state": "uncertain"}
        adapter = Adapter(self.settings, uncertain)
        self.assertTrue((await adapter.invoke("ask", self.ask())).isError)
        restarted = Adapter(self.settings, uncertain)
        self.assertTrue((await restarted.invoke("ask", self.ask())).isError)
        self.assertEqual(len(self.calls), 1)

    async def test_process_exception_leaves_reservation(self):
        def failed(argv, timeout):
            raise OSError("must not leak private diagnostic")
        result = await Adapter(self.settings, failed).invoke("ask", self.ask())
        self.assertEqual(result.structuredContent["state"], "uncertain")
        self.assertNotIn("private diagnostic", str(result))
        self.assertTrue((await self.adapter.invoke("ask", self.ask())).isError)
        self.assertEqual(self.calls, [])

    async def test_request_body_not_in_reservation(self):
        await self.adapter.invoke("ask", self.ask())
        text = (self.settings.state_dir / "mcp-requests/one.json").read_text()
        self.assertNotIn("synthetic message", text)
        self.assertNotIn('"agent"', text)
        self.assertEqual(json.loads(text)["state"], "reserved_do_not_resend")
        self.assertEqual((self.settings.state_dir / "mcp-requests/one.json").stat().st_mode & 0o777, 0o600)

    async def test_argv_and_attachment_root(self):
        self.settings.upload_dir.mkdir()
        (self.settings.upload_dir / "sample.txt").write_text("synthetic")
        response = await self.adapter.invoke("ask", self.ask(attachments=["sample.txt"], download_name="reply.txt"))
        self.assertFalse(response.isError)
        argv, timeout = self.calls[0]
        self.assertEqual(argv[:5], ["--port", "5555", "--state-dir", str(self.settings.state_dir), "ask"])
        self.assertIn(str(self.settings.upload_dir / "sample.txt"), argv)
        self.assertIn(str(self.settings.download_dir), argv)
        self.assertEqual(timeout, 1800)

    async def test_ask_quiet_is_passed_only_when_set(self):
        await self.adapter.invoke("ask", AskInput(agent="A", message="synthetic message", request_id="quiet-default"))
        self.assertNotIn("--quiet", self.calls[0][0])
        await self.adapter.invoke("ask", AskInput(agent="A", message="synthetic message", request_id="quiet-set", quiet=180))
        argv, _ = self.calls[1]
        self.assertEqual(argv[argv.index("--quiet") + 1], "180")
        with self.assertRaises(ValidationError):
            AskInput(agent="A", message="synthetic message", request_id="quiet-bad", quiet=301)

    def test_progress_indicators_and_open_menu_are_not_messages(self):
        from pickle_cli.core import STATUS, Pickle
        for text in ("Thinking...", "Typing…", "Searching the web..."):
            self.assertTrue(STATUS.fullmatch(text))
        for text in ("알겠어요…", "수학 문제집 페이지 분석 시작할게요.", "Thinking about it, the answer is 3."):
            self.assertFalse(STATUS.fullmatch(text))
        node = lambda **kw: kw
        self.assertTrue(Pickle.menu_open([node(text="Reply"), node(text="Copy"), node(text="Select")]))
        self.assertTrue(Pickle.menu_open([{"content-desc": "More emoji"}]))
        self.assertFalse(Pickle.menu_open([node(text="Copy that, thanks"), node(text="Reply")]))

    def test_file_card_with_page_count_and_hidden_extension(self):
        from pickle_cli.core import FILE_SIZE, card_ext, card_name
        for info in ("77B · txt", "2 pages · 15.9MB · pdf", "1.2 KB • csv"):
            self.assertTrue(FILE_SIZE.fullmatch(info))
        for text in ("quiz-linear-function", "PDF", "15.9MB", "오늘 15.9MB · 파일을 두 개 보냈어요 · 확인해 주세요"):
            self.assertFalse(FILE_SIZE.fullmatch(text))
        self.assertEqual(card_ext("2 pages · 15.9MB · pdf"), "pdf")
        self.assertEqual(card_name("quiz-linear-function", "2 pages · 15.9MB · pdf"), "quiz-linear-function.pdf")
        self.assertEqual(card_name("test-data.txt", "77B · txt"), "test-data.txt")
        self.assertEqual(card_name("메모", ""), "메모")

    def test_large_files_are_read_in_pieces_and_review_dialog_is_closed(self):
        import base64
        from pickle_cli.core import Pickle
        blob = bytes(range(256)) * 5000  # 1.28 MB
        calls = []
        class FakeADB:
            def shell(self, command):
                calls.append(command)
                if command.startswith("dd "):
                    i = int(command.split("skip=")[1].split()[0]); step = 512 * 1024
                    return base64.b64encode(blob[i * step:(i + 1) * step]).decode()
                return base64.b64encode(blob).decode()
        ui = Pickle(FakeADB(), "A")
        self.assertEqual(ui.read_remote("/sdcard/Download/x.pdf", len(blob)), blob)
        self.assertEqual(sum(c.startswith("dd ") for c in calls), 3)
        self.assertEqual(ui.read_remote("/sdcard/Download/x.pdf", 100)[:10], blob[:10])

        dialog = '<?xml version="1.0"?><hierarchy><node package="com.android.vending" bounds="[0,0][10,10]"/></hierarchy>'
        chat = '<?xml version="1.0"?><hierarchy><node package="com.pickle.client" bounds="[0,0][10,10]"/></hierarchy>'
        seen = []
        class PopupADB:
            def __init__(self): self.open = True
            def shell(self, command):
                seen.append(command)
                if command.startswith("uiautomator"): return dialog if self.open else chat
                if command.startswith("dumpsys"): return "mCurrentFocus=Window{1 u0 com.android.vending/com.google.android.finsky.inappreviewdialog.InAppReviewActivity}"
                if command == "input keyevent 4": self.open = False
                return ""
        with patch("pickle_cli.core.time.sleep"):
            nodes = Pickle(PopupADB(), "A").snapshot()
        self.assertEqual([n.get("package") for n in nodes], ["com.pickle.client"])
        self.assertEqual(seen.count("input keyevent 4"), 1)

    async def test_option_like_message_remains_one_data_argument(self):
        from pickle_cli.__main__ import parser
        args = AskInput(agent="A", message="--help", request_id="option-text")
        answer = await self.adapter.invoke("ask", args)
        self.assertFalse(answer.isError)
        parsed = parser().parse_args(self.calls[0][0])
        self.assertEqual(parsed.message, "--help")
        self.assertEqual(parsed.agent, "A")

    async def test_option_like_agent_remains_one_data_argument(self):
        from pickle_cli.__main__ import parser
        settings = Settings(agents=("--help",), state_dir=self.root / "state")
        response = await Adapter(settings, self.adapter.runner).invoke("read", ReadInput(agent="--help"))
        self.assertFalse(response.isError)
        self.assertEqual(parser().parse_args(self.calls[0][0]).agent, "--help")

    async def test_symlink_attachment_rejected(self):
        self.settings.upload_dir.mkdir()
        outside = self.root / "private.txt"
        outside.write_text("private")
        (self.settings.upload_dir / "link.txt").symlink_to(outside)
        self.assertTrue((await self.adapter.invoke("ask", self.ask(attachments=["link.txt"]))).isError)
        self.assertEqual(self.calls, [])

    async def test_directory_and_bad_extension_attachment_rejected(self):
        self.settings.upload_dir.mkdir()
        (self.settings.upload_dir / "dir.txt").mkdir()
        (self.settings.upload_dir / "run.sh").write_text("echo no")
        for name in ["dir.txt", "run.sh", "missing.txt"]:
            self.assertTrue((await self.adapter.invoke("ask", self.ask(attachments=[name]))).isError)
        self.assertEqual(self.calls, [])

    async def test_large_attachment_rejected(self):
        self.settings.upload_dir.mkdir()
        with (self.settings.upload_dir / "large.txt").open("wb") as f:
            f.truncate(8 * 1024 * 1024 + 1)
        self.assertTrue((await self.adapter.invoke("ask", self.ask(attachments=["large.txt"]))).isError)
        self.assertEqual(self.calls, [])

    async def test_missing_upload_setting_rejected(self):
        settings = Settings(agents=("A",), state_dir=self.root / "state", enable_ask=True)
        response = await Adapter(settings, self.adapter.runner).invoke("ask", self.ask(attachments=["file.txt"]))
        self.assertTrue(response.isError)
        self.assertEqual(self.calls, [])

    async def test_download_no_overwrite_even_dangling_symlink(self):
        self.settings.download_dir.mkdir()
        p = self.settings.download_dir / "answer.txt"
        p.write_text("existing")
        self.assertTrue((await self.adapter.invoke("download", DownloadInput(agent="A", name="answer.txt"))).isError)
        self.assertEqual(p.read_text(), "existing")
        (self.settings.download_dir / "missing.txt").symlink_to(self.root / "not-there")
        self.assertTrue((await self.adapter.invoke("download", DownloadInput(agent="A", name="missing.txt"))).isError)
        self.assertEqual(self.calls, [])

    async def test_changed_root_symlink_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.settings.download_dir.symlink_to(outside, target_is_directory=True)
        self.assertTrue((await self.adapter.invoke("download", DownloadInput(agent="A", name="x.txt"))).isError)
        self.assertEqual(self.calls, [])

    async def test_calls_are_serialized(self):
        active = 0
        maximum = 0
        def runner(argv, timeout):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.04)
            active -= 1
            return 0, {}
        adapter = Adapter(self.settings, runner)
        results = await asyncio.gather(*(adapter.invoke("read", ReadInput(agent="A")) for _ in range(4)))
        self.assertTrue(all(not r.isError for r in results))
        self.assertEqual(maximum, 1)

    async def test_cross_process_style_lock_blocks_second_adapter(self):
        with call_lock(5555):
            response = await self.adapter.invoke("read", ReadInput(agent="A"))
        self.assertTrue(response.isError)
        self.assertEqual(self.calls, [])

    async def test_cancellation_keeps_lock_until_running_call_finishes(self):
        started = threading.Event()
        finish = threading.Event()
        calls = []
        def runner(argv, timeout):
            calls.append(argv[4])
            if len(calls) == 1:
                started.set()
                finish.wait(2)
            return 0, {}
        adapter = Adapter(self.settings, runner)
        first = asyncio.create_task(adapter.invoke("ask", self.ask()))
        await asyncio.to_thread(started.wait, 1)
        first.cancel()
        second = asyncio.create_task(adapter.invoke("read", ReadInput(agent="A")))
        await asyncio.sleep(0.05)
        self.assertEqual(calls, ["ask"])
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse((await second).isError)
        self.assertEqual(calls, ["ask", "read"])


class RunnerTests(unittest.TestCase):
    def test_stdout_captured_and_no_shell(self):
        with patch("pickle_mcp.server.subprocess.Popen") as popen:
            proc = popen.return_value
            proc.communicate.return_value = (b'{"ok":true}', b"private diagnostic")
            proc.returncode = 0
            code, data = run_cli(["doctor"], 60)
            self.assertEqual((code, data), (0, {"ok": True}))
            argv = popen.call_args.args[0]
            self.assertEqual(argv[0:2], [sys.executable, "-I"])
            self.assertTrue(argv[2].endswith("cli_bridge.py"))
            self.assertNotIn("shell", popen.call_args.kwargs)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_timeout_kills_group_once_no_retry(self):
        with patch("pickle_mcp.server.subprocess.Popen") as popen, patch("pickle_mcp.server.os.killpg") as kill:
            proc = popen.return_value
            proc.pid = 123
            proc.communicate.side_effect = [subprocess.TimeoutExpired("cli", 60), (b"", b"")]
            code, data = run_cli(["ask"], 60)
            self.assertEqual(code, 124)
            self.assertEqual(data["state"], "uncertain")
            kill.assert_called_once()
            popen.assert_called_once()

    def test_invalid_cli_output_is_sanitized(self):
        for output in [b"secret raw output", b"[]", b"\xff"]:
            with patch("pickle_mcp.server.subprocess.Popen") as popen:
                popen.return_value.communicate.return_value = (output, b"")
                code, data = run_cli(["read"], 60)
                self.assertEqual(code, 2)
                self.assertNotIn("secret", str(data))


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_stdio_all_four_tools_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "upload").mkdir()
            (root / "upload/sample.txt").write_text("synthetic")
            params = StdioServerParameters(command=sys.executable,
                args=[str(PACKAGE / "tests/mock_server.py"), str(root)],
                env={**os.environ, "HOME": str(root / "home")})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    self.assertEqual(init.serverInfo.name, "pickle-air-mcp")
                    self.assertEqual(init.serverInfo.version, "0.1.0")
                    listed = await session.list_tools()
                    tools = {t.name: t for t in listed.tools}
                    self.assertEqual(set(tools), {"pickle_doctor", "pickle_read", "pickle_ask", "pickle_download", "pickle_download_image", "pickle_usage", "pickle_call"})
                    self.assertFalse(tools["pickle_ask"].annotations.readOnlyHint)
                    self.assertFalse(tools["pickle_ask"].annotations.idempotentHint)
                    self.assertFalse(tools["pickle_ask"].inputSchema["additionalProperties"])
                    self.assertEqual(tools["pickle_read"].inputSchema["properties"]["agent"]["enum"], ["Synthetic Agent"])
                    await session.send_ping()
                    doctor = await session.call_tool("pickle_doctor", {})
                    self.assertFalse(doctor.isError)
                    read_result = await session.call_tool("pickle_read", {"agent": "Synthetic Agent"})
                    self.assertEqual(read_result.structuredContent["data"]["messages"][0]["text"], "synthetic reply")
                    args = {"agent": "Synthetic Agent", "message": "synthetic question", "request_id": "protocol-one", "attachments": ["sample.txt"]}
                    ask = await session.call_tool("pickle_ask", args)
                    self.assertFalse(ask.isError)
                    duplicate = await session.call_tool("pickle_ask", args)
                    self.assertTrue(duplicate.isError)
                    download = await session.call_tool("pickle_download", {"agent": "Synthetic Agent", "name": "reply.txt"})
                    self.assertFalse(download.isError)
                    self.assertEqual((root / "downloads/reply.txt").read_bytes(), b"synthetic file\n")
                    error = await session.call_tool("pickle_ask", {**args, "request_id": "protocol-two", "message": "simulate uncertainty"})
                    self.assertTrue(error.isError)
                    self.assertEqual(error.structuredContent["data"]["state"], "uncertain")
                    invalid = await session.call_tool("pickle_read", {"agent": "Synthetic Agent", "host": "forbidden"})
                    self.assertTrue(invalid.isError)
                    self.assertEqual(invalid.structuredContent["code"], "invalid_arguments")
                    secret_key = await session.call_tool("pickle_read", {"agent": "Synthetic Agent", "private-body-as-key": "value"})
                    self.assertTrue(secret_key.isError)
                    self.assertNotIn("private-body-as-key", str(secret_key.structuredContent))
                    unknown = await session.call_tool("raw_adb_shell", {"command": "not run"})
                    self.assertTrue(unknown.isError)
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            self.assertEqual([a[4] for a in calls], ["doctor", "read", "ask", "download", "ask"])
            # The reservation also persists across a completely new server process.
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.assertTrue((await session.call_tool("pickle_ask", args)).isError)
            self.assertEqual(len((root / "calls.jsonl").read_text().splitlines()), 5)

    async def test_production_entrypoint_initializes_without_adb(self):
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "pickle_mcp", "--agent", "Synthetic Agent"], cwd=str(PACKAGE))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                response = await session.initialize()
                self.assertEqual(response.serverInfo.name, "pickle-air-mcp")
                self.assertEqual(len((await session.list_tools()).tools), 7)


if __name__ == "__main__":
    unittest.main()
