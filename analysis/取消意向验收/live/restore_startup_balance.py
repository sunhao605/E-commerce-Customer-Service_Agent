"""仅回滚本次启动自动补款；恢复候选必须精确匹配更新前整表哈希。"""
import hashlib
import json
import re
import subprocess
from pathlib import Path

out = Path(__file__).resolve().parent
before = json.loads((out / 'live-before.json').read_text())['tables']['balance_account']
proof = json.loads((out / 'balance-restore-proof.json').read_text())

def sql(statement):
    return subprocess.check_output(['docker', 'exec', 'huangye-outdoor-mysql-1', 'sh', '-c',
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root --batch --raw --skip-column-names huangye_demo -e "$1"',
        'm07-restore', statement], text=True)

columns = before['columns']
assert all(name.replace('_', '').isalnum() for name in columns)
query = 'select ' + ','.join('`' + name + '`' for name in columns) + ' from balance_account order by id'
rows = sql(query)
assert hashlib.sha256(rows.encode()).hexdigest() == proof['current_hash'], '当前数据已变化，停止回滚'
lines = rows.splitlines()
index = next(i for i, line in enumerate(lines) if line.split('\t')[columns.index('user_id')] == 'U1001')
fields = lines[index].split('\t')
current_amount, current_time = fields[columns.index('available_balance')], fields[columns.index('updated_at')]
amount, timestamp = proof['restore_balance'], proof['restore_updated_at']
assert all(re.fullmatch(r'[0-9.]+', value) for value in (amount, current_amount))
assert all(re.fullmatch(r'[0-9 .:-]+', value) for value in (timestamp, current_time))
fields[columns.index('available_balance')], fields[columns.index('updated_at')] = amount, timestamp
lines[index] = '\t'.join(fields)
assert hashlib.sha256(('\n'.join(lines) + '\n').encode()).hexdigest() == before['sha256']
changed = sql(f"START TRANSACTION; UPDATE balance_account SET available_balance={amount}, updated_at='{timestamp}' "
    f"WHERE user_id='U1001' AND available_balance={current_amount} AND updated_at='{current_time}'; SELECT ROW_COUNT(); COMMIT;")
assert changed.strip() == '1', '账户并发变化，未恢复'
assert hashlib.sha256(sql(query).encode()).hexdigest() == before['sha256']
result = {'restored': True, 'balance': amount, 'before_hash_matched': True,
          'rows_changed': 1, 'business_transactions_added': 0, 'reason': '回滚本次启动初始化自动补款'}
(out / 'balance-restore-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
