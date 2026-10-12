"""把逐轮真实截图和失败存档整理成不依赖外部资源的浏览页。"""
import base64
import html
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
batches = [(OUT, "最新4组验收"), (OUT / "补出口后保留续办受阻", "保留续办受阻的原始记录"),
           (OUT / "明确对象后缺出口-修改前", "明确对象后缺出口的原始记录"), (OUT / "首轮原始结果", "首轮原始记录（含测试断言问题）")]
parts = ["""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>取消退款办理：逐轮对话截图</title><style>
body{max-width:1100px;margin:32px auto;padding:0 20px;font:16px/1.7 system-ui;color:#172331;background:#f4f7fb}
h1{font-size:26px}h2{font-size:20px}section{background:white;margin:20px 0;padding:20px;border-radius:12px}
details{padding:10px;border:1px solid #d7e1ed;margin:10px 0;border-radius:8px}summary{cursor:pointer;font-weight:600}
img{display:block;width:416px;max-width:100%;height:auto;margin:16px auto}pre{white-space:pre-wrap;font:inherit}
a{color:#174ea6}</style><h1>取消退款办理：逐轮对话截图</h1>
<p>每次发送消息和点击按钮后均保存真实页面截图。仅为截图展开聊天滚动区域，未改文字、按钮或状态。展开某一轮即可查看当时的完整对话；失败记录也保留在下方。</p>"""]
index = ["# 真实对话与逐轮截图", "", "每次发送消息及按钮点击后截图。截图仅展开聊天滚动区域，未修改文字和按钮状态。", ""]
count = 0
for batch, title in batches:
    rows = json.loads((batch / "dialogue-live.json").read_text())
    parts.append(f'<details {"open" if batch == OUT else ""}><summary>{html.escape(title)} · {len(rows)}轮截图</summary>')
    index += ["## " + title, ""]
    scenes = list(dict.fromkeys(r["scene"] for r in rows))
    for scene in scenes:
        group = [r for r in rows if r["scene"] == scene]
        parts.append('<section><h2>' + html.escape(scene) + '</h2>')
        index += ["### " + scene, ""]
        for number, row in enumerate(group, 1):
            image = (batch / row["screenshot"]).resolve()
            assert image.exists(), image
            count += 1
            action = row.get("message") or "点击：「" + row["click"] + "」"
            answer = row["response"]["answer"]
            encoded = base64.b64encode(image.read_bytes()).decode()
            parts.append(f'<details {"open" if batch == OUT and number == len(group) else ""}><summary>第{number}轮 · {html.escape(action)}</summary>'
                         f'<pre>客户：{html.escape(action)}\n\n系统：{html.escape(answer)}</pre>'
                         f'<img loading="lazy" alt="{html.escape(scene)}第{number}轮截图" src="data:image/png;base64,{encoded}"></details>')
            index += [f"{number}. 客户：{action}", f"   系统：{answer}", f"   [查看本轮完整截图]({image})", ""]
        parts.append('</section>')
    parts.append('</details>')
parts.append('</html>')
preview = OUT / "预览"
preview.mkdir(exist_ok=True)
(preview / "index.html").write_text("\n".join(parts))
(OUT / "对话记录与截图.md").write_text("\n".join(index) + "\n")
print("已整理", count, "轮截图；最新记录", len(json.loads((OUT / 'dialogue-live.json').read_text())), "轮")
