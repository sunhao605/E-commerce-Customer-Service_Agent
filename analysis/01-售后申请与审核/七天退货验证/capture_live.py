"""只读对比本地服务更新前后的业务行；不读取凭据值、不输出原始用户数据。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

out = Path(__file__).resolve().parent
phase = sys.argv[1]
assert phase in ("before", "after")
tables = ("order_header", "order_item", "balance_account", "balance_transaction", "after_sale_request", "approval_record", "logistics_info", "logistics_event")
previous = json.loads((out / "live-before.json").read_text()) if phase == "after" else None

def sql(statement):
    command = ['docker', 'exec', 'huangye-outdoor-mysql-1', 'sh', '-c',
               'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root --batch --raw --skip-column-names huangye_demo -e "$1"',
               'snapshot', statement]
    return subprocess.check_output(command, text=True)

result = {"mode": "read-only SQL", "tables": {}}
for table in tables:
    columns = previous["tables"][table]["columns"] if previous else sql(
        f"select column_name from information_schema.columns where table_schema='huangye_demo' and table_name='{table}' order by ordinal_position").splitlines()
    assert columns and all(name.replace('_', '').isalnum() for name in columns)
    rows = sql(f"select {','.join('`' + name + '`' for name in columns)} from `{table}` order by id")
    result["tables"][table] = {"columns": columns, "rows": len(rows.splitlines()), "sha256": hashlib.sha256(rows.encode()).hexdigest()}
if previous:
    result["unchanged"] = {table: result["tables"][table] == previous["tables"][table] for table in tables}
    result["passed"] = all(result["unchanged"].values())
(out / f"live-{phase}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"phase": phase, "rows": {table: item['rows'] for table, item in result['tables'].items()},
                  "unchanged": result.get("unchanged")}, ensure_ascii=False))
if previous:
    assert result["passed"], "业务行发生变化，检查 live-after.json；未自动修正数据"
