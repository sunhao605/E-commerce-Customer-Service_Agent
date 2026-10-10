"""实际后台表格布局检查，只读演示数据，不执行审批。"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parent
results = []
errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('http://127.0.0.1:5174/admin/')
    page.get_by_label('管理员账号', exact=True).fill('admin')
    page.get_by_label('密码', exact=True).fill('admin123456')
    page.get_by_role('button', name='登录后台', exact=True).click()
    for width in (2738, 1440, 1101, 1024, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        for name in ('订单管理', '售后审批'):
            page.get_by_role('button', name=name, exact=True).click()
            page.locator('tbody tr').first.wait_for()
            page.wait_for_load_state('networkidle')
            ids = page.locator('tbody tr td:first-child').all_text_contents()
            overflow = page.locator('td').evaluate_all('''cells => cells.flatMap(cell => {
                const bounds = cell.getBoundingClientRect();
                const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
                let node;
                while (node = walker.nextNode()) {
                    if (!node.textContent.trim()) continue;
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    if ([...range.getClientRects()].some(r => r.left < bounds.left - 1 || r.right > bounds.right + 1))
                        return [cell.textContent];
                }
                return cell.scrollWidth > cell.clientWidth + 1 ? [cell.textContent] : [];
            })''')
            assert not overflow, (width, name, overflow)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), (width, name)
            first_id = ids[0]
            page.locator('tbody tr').first.click()
            page.locator('.kv strong').filter(has_text=first_id).wait_for()
            assert page.locator('tbody tr td:first-child').all_text_contents() == ids
            assert page.locator('td').evaluate_all('(cells) => cells.every(c => getComputedStyle(c).textOverflow !== "ellipsis")')
            page.screenshot(path=str(out/f'{name}-{width}.png'), full_page=True)
            results.append({'width': width, 'page': name, 'rows': len(ids), 'overflow': overflow, 'detail_opens': True})
    assert not errors, errors
    browser.close()
(out/'result.json').write_text(json.dumps({'results': results, 'page_errors': errors}, ensure_ascii=False, indent=2))
print('订单/售后表格：5 种宽度无单元格文字溢出，完整编号保留，详情点击通过。')
