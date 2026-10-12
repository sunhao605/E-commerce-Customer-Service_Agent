"""复用隔离服务与固定模型，结果独立保存，不覆盖上一轮证据。"""
import importlib.util
from pathlib import Path
import sys

OUT = Path(__file__).resolve().parent
SHARED = OUT.parent / "取消闭环验收"


if __name__ == "__main__":
    for name in ("CheckServer.java", "serve.py", "cases.json"):
        target = OUT / name
        if not target.exists():
            target.symlink_to(Path("../取消闭环验收") / name)
    spec = importlib.util.spec_from_file_location("closure_runner", SHARED / "run.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.OUT = OUT
    runner.RUNTIME = OUT / ".runtime"
    sys.argv = [sys.argv[0], "scripted"]
    runner.main()
