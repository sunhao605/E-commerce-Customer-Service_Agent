"""用同一实验执行器读取英语描述版本；保留002的输入、组合与调用设置。"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('targeted_compare', ROOT.parent / 'run_targeted.py')
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)
experiment.ROOT = ROOT

if __name__ == '__main__':
    experiment.main()
