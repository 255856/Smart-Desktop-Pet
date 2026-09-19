"""测试 open_app / open_website 工具的可用性，找出实际的问题。"""
import sys
from pathlib import Path
import unittest.mock as mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.engine.tools._system import register
from app.engine.tools._core import ToolRegistry

reg = ToolRegistry()
register(reg)
open_website = reg.get("open_website").fn
open_app = reg.get("open_app").fn


print("=" * 60)
print("测试 open_website")
print("=" * 60)

calls = []
def fake_open(url, *args, **kwargs):
    calls.append(url)
    return True

with mock.patch("webbrowser.open", side_effect=fake_open):
    cases = [
        "https://www.baidu.com",
        "http://example.com",
        "www.baidu.com",
        "baidu.com",
        "ftp://example.com",
        "  https://github.com  ",
    ]
    for url in cases:
        calls.clear()
        result = open_website(url)
        print(f"  open_website({url!r})")
        print(f"    → result: {result!r}")
        print(f"    → calls: {calls}")
print()


print("=" * 60)
print("测试 open_app")
print("=" * 60)

startfile_calls = []
popen_calls = []

def fake_startfile(path):
    startfile_calls.append(path)

def fake_popen(*args, **kwargs):
    popen_calls.append((args, kwargs))
    class FakeProc:
        pid = 1234
    return FakeProc()

test_cases = [
    "notepad",
    "记事本",
    "totallynonexistentapp123",
    "C:/Windows/notepad.exe",
    "C:/nonexistent/file.exe",
    "星穹铁道",
    "",
    " ",
]

with mock.patch("os.startfile", side_effect=fake_startfile), \
     mock.patch("subprocess.Popen", side_effect=fake_popen):
    for name in test_cases:
        startfile_calls.clear()
        popen_calls.clear()
        try:
            result = open_app(name)
        except Exception as e:
            result = f"EXCEPTION: {e!r}"
        print(f"  open_app({name!r})")
        print(f"    → result: {result!r}")
        print(f"    → startfile: {startfile_calls}")
        print(f"    → popen: {popen_calls}")
        print()
