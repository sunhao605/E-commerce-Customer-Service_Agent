"""检查目录迁移、文档链接、构建配置和旧品牌残留；不调用网络或模型。"""
import ast
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile

root = Path(__file__).resolve().parents[2]
# 检测词用码点拼接，避免检查器自己保留旧品牌文字。
old_brand = ''.join(map(chr, (23567, 21746)))
old_slug = 'xiao' + 'zhe'
patterns = [old_brand.encode(), old_brand.encode('utf-16le')]
patterns += [separator.join(('xiao', 'zhe')).encode() for separator in ('', '-', '_')]
residual = []
files = archive_entries = 0

def scan(name, data):
    if any(pattern in data.lower() for pattern in patterns):
        residual.append(name)

for path in root.rglob('*'):
    if not path.is_file() or path.is_symlink():
        continue
    files += 1
    name = str(path.relative_to(root))
    scan(name, name.encode())
    scan(name, path.read_bytes())
    if path.name == 'm2-cache.tar.gz':
        with tarfile.open(path) as archive:
            for member in archive:
                if not member.isfile():
                    continue
                archive_entries += 1
                data = archive.extractfile(member).read()
                scan(name + '!' + member.name, data)
                if member.name.endswith('.jar'):
                    with zipfile.ZipFile(io.BytesIO(data)) as jar:
                        for entry in jar.infolist():
                            if not entry.is_dir():
                                archive_entries += 1
                                scan(name + '!' + member.name + '!' + entry.filename, jar.read(entry))
assert not residual, residual

checked_links = 0
for path in [root / 'README.md', *(root / 'docs').glob('*.md'), *(root / 'analysis').glob('*.md')]:
    for link in re.findall(r'\]\(([^)]+)\)', path.read_text()):
        link = link.strip('<>')
        if re.match(r'^[a-z]+://|^#', link):
            continue
        name = re.sub(r':\d+$', '', link.split('#')[0])
        target = Path(name) if name.startswith('/') else path.parent / name
        assert target.exists(), (path, link)
        checked_links += 1
for name in ('产品设计学习.md', '项目交接.md', '项目评测记录.md', 'BadCase.md', '本地测试用例.md'):
    assert (root / 'docs' / name).is_file()
    assert not (root / name).exists()
assert not (root / 'screenshots').exists()
assert (root / 'analysis/课程参考截图').is_dir()
layout = json.loads(Path(__file__).with_name('backend-layout.json').read_text())
backend_dirs = sorted(path.name for path in (root / 'backend').iterdir()
                      if path.is_dir() and path.name != '__pycache__')
assert backend_dirs == layout['current_top_level_folders'], backend_dirs
path_names = {'CAPABILITIES_PATH', 'CASES_PATH', 'DEFAULT_COURSE_ENV_PATH', 'PROMPTS_DIR', 'KNOWLEDGE_DIR'}
for name, expected in layout['logic_sha256_without_imports_and_resource_paths'].items():
    tree = ast.parse((root / name).read_text())
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))
                 and not (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                          and target.id in path_names for target in node.targets))]
    assert hashlib.sha256(ast.dump(tree).encode()).hexdigest() == expected, name
for name, expected in layout['resource_sha256'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
assert json.loads((root / 'frontend/tsconfig.node.json').read_text())['compilerOptions']['noEmit'] is True
for name in ('vite.config.js', 'vite.config.d.ts'):
    assert not (root / 'frontend' / name).exists()
result = {'scanned_files': files, 'archive_entries': archive_entries, 'brand_residual_paths': residual,
          'checked_links': checked_links, 'broken_links': [], 'layout_checks': 'passed',
          'backend_folders': backend_dirs, 'logic_files_checked': len(layout['logic_sha256_without_imports_and_resource_paths']),
          'resource_files_checked': len(layout['resource_sha256'])}
Path(__file__).with_name('structure-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
