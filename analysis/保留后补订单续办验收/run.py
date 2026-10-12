"""复用已有隔离启动器，结果单独保存。"""
import importlib.util
from pathlib import Path

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("runner", OUT.parent / "取消闭环验收/run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runner.OUT, runner.RUNTIME = OUT, OUT / ".runtime"
runner.main()
