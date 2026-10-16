"""只读对比日常 MySQL 业务行和商品价格库存。不输出原始行，不改数据。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

out = Path('/private/tmp/product-rag-load-20261012')
phase = sys.argv[1]
assert phase in ("before", "after")
tables = ("order_header", "order_item", "balance_account", "balance_transaction", "after_sale_request", "approval_record", "logistics_info", "logistics_event")
previous = json.loads((out / "live-before.json").read_text()) if phase == "after" else None

def sql(statement):
    command = ["/Users/sunhao/.local/bin/docker", "exec", "huangye-outdoor-mysql-1", "sh", "-c",
               'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root --batch --raw --skip-column-names huangye_demo -e "$1"',
               "snapshot", statement]
    return subprocess.check_output(command, text=True)

result = {"mode": "read-only SQL", "tables": {}}
for table in tables:
    columns = previous["tables"][table]["columns"] if previous else sql(
        f"select column_name from information_schema.columns where table_schema='huangye_demo' and table_name='{table}' order by ordinal_position").splitlines()
    assert columns and all(name.replace("_", "").isalnum() for name in columns)
    rows = sql(f"select {','.join('`' + name + '`' for name in columns)} from `{table}` order by id")
    result["tables"][table] = {"columns": columns, "rows": len(rows.splitlines()), "sha256": hashlib.sha256(rows.encode()).hexdigest()}

product_columns = [name for name in sql(
    "select column_name from information_schema.columns where table_schema='huangye_demo' and table_name='product' order by ordinal_position").splitlines()
    if name in {"id", "code", "name", "price", "stock", "status", "active", "on_sale", "sale_status"}]
assert {"id", "name", "price", "stock"} <= set(product_columns)
product_rows = sql(f"select {','.join('`' + name + '`' for name in product_columns)} from `product` order by id")
result["product_facts"] = {"columns": product_columns, "rows": len(product_rows.splitlines()), "sha256": hashlib.sha256(product_rows.encode()).hexdigest()}

if previous:
    result["unchanged"] = {table: result["tables"][table] == previous["tables"][table] for table in tables}
    result["product_facts_unchanged"] = result["product_facts"] == previous["product_facts"]
    result["passed"] = all(result["unchanged"].values()) and result["product_facts_unchanged"]
(out / f"live-{phase}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"phase": phase, "rows": {table: item["rows"] for table, item in result["tables"].items()},
                  "products": result["product_facts"]["rows"], "unchanged": result.get("unchanged"),
                  "product_facts_unchanged": result.get("product_facts_unchanged")}, ensure_ascii=False))
if previous:
    assert result["passed"], "业务行或商品价格库存发生变化；未自动修正数据"
