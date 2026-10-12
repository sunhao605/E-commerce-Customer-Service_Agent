"""复用只读业务表快照脚本，结果保存到本轮目录。"""
from pathlib import Path

shared = Path(__file__).resolve().parents[1] / "取消意向验收/live/capture_live.py"
exec(compile(shared.read_text(), str(shared), "exec"))
