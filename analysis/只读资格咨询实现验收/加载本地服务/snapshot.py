"""复用既有只读业务表快照，结果保留在本次加载目录。"""
from pathlib import Path
shared = Path(__file__).resolve().parents[2] / "取消意向验收/live/capture_live.py"
exec(compile(shared.read_text(), str(shared), "exec"))
