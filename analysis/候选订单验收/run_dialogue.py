"""顺序对话验收；合成 Runtime Context，只调用 /chat，不提交或批准。"""
import hashlib
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

root = Path(__file__).resolve().parents[2]
url = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8000'
run = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')
out = Path(__file__).resolve().parent / run
out.mkdir()
orders = [dict(orderNo=f'ORD980{i}', userId='m06-isolated', paymentStatus='PAID', fulfillmentStatus='UNSHIPPED', totalAmount=200+i,
               items=[dict(productName=f'验收露营椅{i}')]) for i in (1, 2)]

def save(name, value):
    (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def call(path, body=None):
    req = urllib.request.Request(url + path, data=json.dumps(body, ensure_ascii=False).encode() if body else None,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.load(response)

save('frozen.json', dict(initial='全新同一会话，两笔本人合成未发货订单；无历史、无页面订单。',
     inputs=['帮我退款。', '第一笔。'], expected=['展示 ORD9801、ORD9802，顺序固定', '选择 ORD9801，工具与工作流对象一致'],
     forbidden=['选择 ORD9802', '再次追问订单号', '声称已提交或已退款'], entry=url,
     boundary='合成 Runtime Context 不是 MySQL 实付事实；此处仅验 Python 对象绑定，不证明商城确认卡。'))
save('manifest.json', {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'backend').rglob('*.py')) + sorted((root/'backend/resources').rglob('*.md'))})
results=[]
for turn, message in enumerate(['帮我退款。', '第一笔。'], 1):
    body=dict(session_id=f'm06-{run}', runtime_user_id='m06-isolated', user_message=message, runtime_context={'currentUserOrders': orders})
    save(f'{turn}-request.json', body)
    reply=call('/chat', body)
    save(f'{turn}-response.json', reply)
    state=reply.get('session_state', {})
    result=dict(turn=turn, input=message, answer=reply.get('answer'), intent=state.get('intent'), clarification=reply.get('clarification'),
                tools=reply.get('tool_calls'), workflow=state.get('workflow'), memory=state.get('memory'), next_action=state.get('next_action'))
    results.append(result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
save('trace.json', call(f'/sessions/m06-{run}/trace'))
first=[c['value'] for c in (results[0]['clarification'] or {}).get('candidates', [])]
second=results[1]
passed=(first == ['ORD9801', 'ORD9802'] and second['clarification'] is None and any(t.get('arguments', {}).get('order_id') == 'ORD9801' for t in second['tools']))
save('result.json', dict(passed=passed, results=results))
print(f'Saved: {out}; passed={passed}', flush=True)
assert len(results) == 2
