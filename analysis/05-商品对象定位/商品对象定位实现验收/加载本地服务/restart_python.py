"""复用日常Python默认配置；不调用模型。"""
from pathlib import Path
shared = Path(__file__).resolve().parents[3] / "03-取消与续办/当前申请停止确认验收/restart_python.py"
source = shared.read_text().replace("ROOT = OUT.parents[2]", "ROOT = OUT.parents[3]")
exec(compile(source, str(shared), "exec"))
