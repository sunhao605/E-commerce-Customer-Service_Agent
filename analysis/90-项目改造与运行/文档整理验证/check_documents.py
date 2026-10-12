"""只读检查文档结构、Markdown/HTML 本地链接、脚本语法及软链接。"""
import ast
import json
from pathlib import Path
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[3]
EXCLUDED = {'.runtime', '.before', 'frontend-build', 'frontend-before'}
REFERENCE = ROOT / 'docs/05-外部参考/0303_值得借鉴的产品设计_副本.md'
EXTERNAL_TARGETS = {
    'Customer-Agent Bad Case.md',
    'Customer-Agent/Agent/CustomerAgent/tools/get_product_knowledge.py',
    'Customer-Agent/Agent/CustomerAgent/custom/tool_decorator.py',
    'Customer-Agent/Message/handlers/keyword_handler.py',
}

files = [ROOT / 'README.md']
for folder in ('docs', 'analysis'):
    files.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() or p.is_symlink())
links = scripts = symlinks = 0
missing_reference = []
errors = []
for path in files:
    if path.is_symlink():
        symlinks += 1
        assert path.exists(), path
        continue
    if EXCLUDED.intersection(path.parts):
        continue
    if path.suffix == '.py':
        ast.parse(path.read_text(), filename=str(path))
        scripts += 1
    if path.suffix not in ('.md', '.html'):
        continue
    source = path.read_text()
    targets = re.findall(r'\]\(([^)\n]+)\)', source)
    if path.suffix == '.html':
        targets += re.findall(r'\b(?:href|src)="([^"\n]+)"', source)
    for url in targets:
        url = unquote(url.strip('<>'))
        if re.match(r'^[a-zA-Z][\w+.-]*:|^#', url):
            continue
        name = re.sub(r':\d+$', '', url.split('#')[0])
        if not name:
            continue
        target = Path(name) if name.startswith('/') else path.parent / name
        if target.exists():
            links += 1
        elif path == REFERENCE and name in EXTERNAL_TARGETS:
            missing_reference.append(url)
        else:
            errors.append({'file': str(path.relative_to(ROOT)), 'link': url})

for folder in ('docs', 'analysis'):
    for child in (ROOT / folder).iterdir():
        if child.is_dir():
            assert (child / 'README.md').is_file(), child
assert not errors, errors
print(json.dumps({'local_links_checked': links, 'python_scripts_parsed': scripts,
                  'symlinks_checked': symlinks, 'broken_project_links': errors,
                  'missing_original_project_references': missing_reference}, ensure_ascii=False, indent=2))
