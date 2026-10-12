"""对照去品牌化契约，检查商品数值和核心流程结构。"""
import ast
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[3]
contract = json.loads((Path(__file__).with_name('migration-contract.json')).read_text())
layout = json.loads((root / 'analysis/90-项目改造与运行/结构整理验证/backend-layout.json').read_text())
original_modules = {new: old for old, new in layout['modules'].items()}
java = root / 'ecommerce-backend/src/main/java/com/teachdemo/ecommerce/config/DataInitializer.java'
pattern = re.compile(r'(?:new Product|saveProductWithImage)\("(SKU-[A-Z0-9-]+)".*?new BigDecimal\("([0-9.]+)"\), (\d+), "[^"]*",\s*(true|false), (true|false)', re.S)
assert [list(row) for row in pattern.findall(java.read_text())] == contract['product_facts']

class StripText(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        node.module = original_modules.get(node.module, node.module)
        return node

    def visit_Constant(self, node):
        return ast.copy_location(ast.Constant(value='<text>'), node) if isinstance(node.value, str) else node

for name, expected in contract['ast_sha256_without_text'].items():
    current_name = layout['file_paths'].get(name, name)
    tree = ast.dump(StripText().visit(ast.parse((root / current_name).read_text())))
    assert hashlib.sha256(tree.encode()).hexdigest() == expected, name
manifest = json.loads((root / 'backend/resources/agent_capabilities.json').read_text())
for key in ('features', 'endpoints'):
    assert manifest[key] == contract[key], key
for item in json.loads((Path(__file__).with_name('product-mapping.json')).read_text()).values():
    ET.parse(root / 'ecommerce-backend/frontend/public/products' / f"{item['slug']}.svg")
for path in (root / 'backend').rglob('*.py'):
    ast.parse(path.read_text(), filename=str(path))
print('商品金额/库存/标志、四个核心流程结构、能力与接口契约一致；18 张 SVG 和 Python 语法通过。')
