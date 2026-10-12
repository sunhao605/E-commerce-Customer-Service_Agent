"""核对电商项目的业务目录、Java 类正文和前端入口；不访问数据库或模型。"""
import hashlib
import json
from pathlib import Path
import re
import yaml

root = Path(__file__).resolve().parents[3]
layout = json.loads(Path(__file__).with_name('ecommerce-layout.json').read_text())
java_root = root / 'ecommerce-backend/src/main/java/com/teachdemo/ecommerce'
for name, expected in layout['java_body_sha256'].items():
    path = root / name
    text = path.read_text()
    for old, new in layout['java_classes'].items():
        text = text.replace(new, old)
    body = re.sub(r'^\s*(?:package|import) .*?;\s*$', '', text, flags=re.M).strip()
    assert hashlib.sha256(body.encode()).hexdigest() == expected, name
    package = re.search(r'^package ([\w.]+);', path.read_text(), re.M).group(1)
    assert (java_root.parent.parent.parent / package.replace('.', '/') / path.name).resolve() == path.resolve(), name
assert len(list(java_root.rglob('*.java'))) == len(layout['java_body_sha256'])
assert sorted(p.name for p in java_root.iterdir() if p.is_dir()) == sorted(name for name in layout['java_domains'] if name)
front = root / 'ecommerce-backend/frontend'
for name in layout['frontend_routes'].values():
    text = (front / name).read_text()
    source = re.search(r'<script[^>]+src="([^"]+)"', text).group(1)
    assert (front / source.lstrip('/')).is_file(), (name, source)
assert sorted(p.name for p in (front / 'src').iterdir()) == ['admin', 'shared', 'shop']
assert not (front / 'shop').exists() and not (front / 'shared').exists()
assert list((front / 'admin').iterdir()) == [front / 'admin/index.html']
config = json.loads((front / 'tsconfig.json').read_text())
assert config['include'] == ['src', 'vite.config.ts']
assert config['compilerOptions']['noUnusedLocals'] and config['compilerOptions']['noUnusedParameters']
source = '\n'.join(p.read_text() for p in (front / 'src').rglob('*.ts'))
for name in ('CurrentAccount', 'BalanceTransaction'):
    assert len(re.findall(r'export type ' + name + r'\s*=', source)) == 1, name
assert len(re.findall(r'export type ApiResponse<T>\s*=', source)) == 1
for name in layout['removed_files']:
    assert not (root / name).exists(), name
config = yaml.safe_load((root / 'ecommerce-backend/src/main/resources/application.yml').read_text())
assert config['springdoc']['group-configs'][0]['packages-to-scan'] == 'com.teachdemo.ecommerce'
result = {'java_classes_checked': len(layout['java_body_sha256']),
          'java_domains': sorted(name for name in layout['java_domains'] if name),
          'java_cleanup_checked': layout['java_cleanup']['removed_members'],
          'frontend_source_groups': ['admin', 'shared', 'shop'], 'frontend_entries': list(layout['frontend_routes'].values()),
          'shared_type_definitions': 'unique', 'layout_checks': 'passed', 'model_calls': 0}
Path(__file__).with_name('ecommerce-layout-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
