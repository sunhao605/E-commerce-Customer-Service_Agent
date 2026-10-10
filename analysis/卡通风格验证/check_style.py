"""卡通样式检查：当前后端迁移契约、SVG 和主要主题色；旧样式结果保留。"""
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent
subprocess.run([sys.executable, str(root / 'analysis/结构整理验证/check_structure.py')], check=True)

styles = ['frontend/src/styles.css', 'ecommerce-backend/frontend/src/shop/styles.css', 'ecommerce-backend/frontend/src/admin/styles.css']
themes = [dict(re.findall(r'--([\w-]+): (#[0-9a-f]{6});', (root / name).read_text())) for name in styles]
assert themes[0] == themes[1] == themes[2]
files = list((root / 'ecommerce-backend/frontend/public/products').glob('*.svg')) + [root / 'frontend/public/cartoon-camp.svg', root / 'ecommerce-backend/frontend/public/cartoon-camp.svg']
for path in files:
    ET.parse(path)
assert files[-2].read_bytes() == files[-1].read_bytes()

def luminance(color):
    values = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    return sum(a * b for a, b in zip(values, (.2126, .7152, .0722)))

ratios = {}
for fg, bg in [('ink', 'paper'), ('ink', 'sky'), ('ink', 'sun'), ('ink', 'coral'), ('muted', 'panel'), ('muted', 'soft-blue'), ('muted', 'sky')]:
    low, high = sorted((luminance(themes[0][fg]), luminance(themes[0][bg])))
    ratio = (high + .05) / (low + .05)
    assert ratio >= 4.5, (fg, bg, ratio)
    ratios[f'{fg} / {bg}'] = round(ratio, 2)

result = {'backend_logic_baseline': 'backend-layout.json（2026-10-07 目录迁移前）', 'svg_valid': len(files), 'measured_theme_contrast': ratios, 'scope': '主要主题色对；不等于全站无障碍认证；旧业务源码哈希为历史记录'}
(out / 'current-style-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False))
