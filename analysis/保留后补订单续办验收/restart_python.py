"""复用既有本地服务恢复脚本，记录留在本轮目录。"""
from pathlib import Path

shared = Path(__file__).resolve().parents[1] / "本次申请停止确认验收/restart_python.py"
exec(compile(shared.read_text(), str(shared), "exec"))
