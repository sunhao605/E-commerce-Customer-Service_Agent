"""003B：复用003执行器，独立冻结新样本和输出目录。"""
import importlib.util
from pathlib import Path
ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("routing", ROOT.parent / "run_routing.py")
routing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routing)
routing.ROOT = ROOT
if __name__ == "__main__":
    routing.main()
