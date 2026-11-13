"""全量按钮遍历：把每个页面上的每一个可点元素都按一遍。

## 和之前 e2e_web_walkthrough 的区别

那个只点**预定义的安全按钮**（21 个），覆盖面取决于我手写了哪些。
这个脚本反过来：**先枚举页面上所有可点元素**，再按危险度分类逐个点，
目标是"没有漏掉的按钮"，而不是"我想到的都点了"。

## 危险度分类（决定点到哪一步）

  safe    —— 直接点：导航、tab、刷新、查询、筛选、分页、展开/折叠、
             打开弹窗、关闭、取消、语言/主题切换
  modal   —— 只点到**确认框出现**，记录文案后取消/ESC。包括删除、清空、
             批量、强制、急停、重置
  danger  —— 连弹窗都不打开（目前留空，全部归到 modal 处理）

判定顺序：先看按钮的 class/aria（modal 触发、tab、pagination 等）→
再看文本关键词 → 兜底 safe。

## 每个按钮记什么

  · 点击前的 URL / 弹窗数量
  · 点击后：新请求（method + path + status）、DOM 文本是否变化、
    是否出现新弹窗、console 是否报错
  · 判定：ok / no-feedback / error / skipped

"no-feedback" 不算失败但要看：一个按钮点了没有任何请求、没有 DOM 变化、
没有弹窗 —— 要么它是纯前端开关（合法），要么它坏了（真问题）。
脚本把它单独列出来人工过一遍，而不是混进 ok。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime

from playwright.sync_api import sync_playwright

from _click_impl import click_page   # 点击实现（独立模块，便于单改）

BASE = 'http://127.0.0.1:5000'
OUT_DIR = os.path.join('build', 'e2e', 'all_buttons')

PAGES = [
    ('dashboard', '/dashboard'),
    ('devices', '/devices'),
    ('control', '/control'),
    ('history', '/history'),
    ('alarms', '/alarms'),
    ('alarm_output', '/alarm-output'),
    ('industry40', '/industry40'),
    ('config', '/config'),
    ('users', '/users'),
    ('screen', '/screen'),
]

#: 文本关键词 → 危险度
DANGER_WORDS = ('删除', '清空', '重置', '批量', '全部', '强制', '急停', '停机',
                '断开', '停止', '抹除', '销毁', '确认', '应用', '保存', '提交',
                'delete', 'remove', 'clear', 'reset', 'batch', 'estop', 'stop')
SAFE_WORDS = ('刷新', '查询', '搜索', '筛选', '展开', '折叠', '关闭', '取消',
              '上一页', '下一页', '首页', '末页', '导出', '下载', '全屏',
              'refresh', 'search', 'filter', 'close', 'cancel', 'export')

#: 只认**真正的按钮类**控件。
#:
#: 第一版把 `a.nav-link` 和所有 `[onclick]` 都算进来了，结果是：
#:   · 侧边导航 9 个链接被当成 dashboard 的按钮 —— 一点就跳页，
#:     然后脚本再把页面 goto 回来，33 张截图里有 9 张是别的页面；
#:   · 页面上大量无文本的容器/图表 div 被 `[onclick]` 选中，
#:     点了没任何反应，产出 20 多张一模一样的 `notxt` 截图。
#:
#: 导航链接该验的是"点了能到那一页"，那是路由级 E2E 的事，
#: 不该混进"按钮有没有反应"里。
SELECTOR = ('button, a.btn, input[type="submit"], input[type="button"], '
            '[role="button"], .btn-group .btn, .pagination .page-link, '
            '[data-bs-toggle="tab"], [data-bs-toggle="dropdown"], '
            '[data-bs-toggle="modal"], [data-bs-toggle="collapse"], '
            '.dropdown-item')

#: 这些即使命中选择器也不点（导航类，由路由 E2E 覆盖）
SKIP_CLASS = ('nav-link', 'navbar-brand', 'page-item disabled')


def _classify(el) -> str:
    """safe / modal"""
    try:
        text = (el.inner_text() or '').strip()
    except Exception:
        text = ''
    try:
        cls = (el.get_attribute('class') or '').lower()
    except Exception:
        cls = ''
    try:
        toggle = (el.get_attribute('data-bs-toggle') or '').lower()
    except Exception:
        toggle = ''

    # 明确的安全类
    if toggle in ('tab', 'dropdown', 'collapse', 'tooltip', 'popover', 'offcanvas'):
        return 'safe'
    if 'nav-link' in cls or 'page-link' in cls or 'dropdown-item' in cls:
        # dropdown-item 里也可能是"删除"，所以下面还要过关键词
        if not any(w in text.lower() for w in DANGER_WORDS):
            return 'safe'

    low = text.lower()
    if any(w in text for w in DANGER_WORDS) or any(w in low for w in DANGER_WORDS):
        return 'modal'
    if any(w in text for w in SAFE_WORDS) or any(w in low for w in SAFE_WORDS):
        return 'safe'
    # 无文本的图标按钮：按 class 猜
    if any(k in cls for k in ('refresh', 'close', 'toggle', 'filter', 'search')):
        return 'safe'
    return 'safe'


def _collect(page):
    """枚举当前页面的所有可点元素（去重、跳过不可见）。"""
    items = page.evaluate(
        """(sel) => {
            const out = [];
            document.querySelectorAll(sel).forEach((el, i) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (r.width < 2 || r.height < 2) return;
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return;
                if (el.disabled) return;
                const cls = (el.getAttribute('class') || '').toLowerCase();
                if (['nav-link','navbar-brand'].some(k => cls.includes(k))) return;
                const text = (el.innerText || el.value || el.title || el.getAttribute('aria-label') || '')
                              .replace(/\\s+/g, ' ').trim().slice(0, 40);
                // 无文本且不像图标按钮的，一律跳过（图表 canvas / 纯容器）
                if (!text && !/btn|button|icon|close|toggle|switch/.test(cls)) return;
                if (el.tagName.toLowerCase() === 'canvas') return;
                out.push({
                    idx: i,
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    cls: (el.getAttribute('class') || '').slice(0, 60),
                    text: text,
                });
            });
            return out;
        }""", SELECTOR)
    return items


def main(argv=None):
    only_enum = '--enumerate' in (argv or sys.argv[1:])
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    state = os.path.join('build', 'e2e', '.session.json')
    if not os.path.isfile(state):
        print('缺少登录态 build/e2e/.session.json，先跑一次带登录的 E2E')
        return 2

    report = {'generated_at': stamp, 'pages': [], 'summary': {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        ctx = browser.new_context(viewport={'width': 1680, 'height': 1050},
                                  storage_state=state)
        page = ctx.new_page()

        total = {'safe': 0, 'modal': 0}

        for name, path in PAGES:
            t0 = time.monotonic()
            # `load` 要等全部资源（含图表库/字体）——服务刚启动、采集线程
            # 满负荷时容易超 30s。用 domcontentloaded + 固定等待更稳：
            # 我们要的是"按钮已经渲染出来"，不是"所有图片都下完"。
            try:
                page.goto(BASE + path, wait_until='domcontentloaded', timeout=60000)
                page.wait_for_timeout(4000)
                items = _collect(page)
            except Exception as e:
                print(f'{name:14} {path:16} !! 加载失败 {type(e).__name__}: {str(e)[:70]}',
                      flush=True)
                report['pages'].append({'name': name, 'path': path,
                                        'error': f'{type(e).__name__}: {e}',
                                        'count': 0, 'items': []})
                continue

            seen, uniq = set(), []
            for it in items:
                key = (it['tag'], it['text'], it['cls'][:30])
                if key in seen:
                    continue
                seen.add(key)
                it['level'] = _classify_it(it)
                uniq.append(it)
                total[it['level']] += 1

            report['pages'].append({'name': name, 'path': path,
                                    'count': len(uniq), 'items': uniq})
            print(f'{name:14} {path:16} 可点元素 {len(uniq):3}  '
                  f'(safe {sum(1 for x in uniq if x["level"]=="safe")}, '
                  f'modal {sum(1 for x in uniq if x["level"]=="modal")})  '
                  f'{time.monotonic() - t0:.1f}s', flush=True)

        report['summary'] = {'pages': len(PAGES), 'elements': total['safe'] + total['modal'],
                             **total}
        print(f'\n合计：{len(PAGES)} 页 / {total["safe"] + total["modal"]} 个可点元素 '
              f'(safe {total["safe"]}, modal {total["modal"]})')

        with open(os.path.join(OUT_DIR, f'enumerate-{stamp}.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=1)

        if not only_enum:
            print('\n开始逐个点击（modal 类只点到确认框，原生 confirm 一律取消）…',
                  flush=True)
            all_res = []
            for pg in report['pages']:
                if pg.get('error'):
                    continue
                print(f'  [{pg["name"]}] {pg["count"]} 个…', flush=True)
                try:
                    page.goto(BASE + pg['path'], wait_until='domcontentloaded', timeout=45000)
                    page.wait_for_timeout(3000)
                except Exception as e:
                    print(f'    !! 重新加载失败 {type(e).__name__}', flush=True)
                    continue
                pg['click_results'] = click_page(page, pg['name'], pg['path'],
                                                  pg['items'], stamp)
                all_res.extend(pg['click_results'])

            from collections import Counter
            stat = Counter(r.get('status') for r in all_res)
            by_level = Counter((r['level'], r.get('status')) for r in all_res)
            report['click_summary'] = {
                'total': len(all_res),
                'by_status': dict(stat),
                'by_level': {f'{k[0]}:{k[1]}': v for k, v in by_level.items()},
            }
            print('\n===== 点击汇总 =====')
            print(f'共点 {len(all_res)} 个：' + '  '.join(f'{k}={v}' for k, v in stat.items()))
            for lv in ('safe', 'modal'):
                sub = [r for r in all_res if r['level'] == lv]
                s2 = Counter(r.get('status') for r in sub)
                print(f'  {lv:5} {len(sub):3} 个：' + '  '.join(f'{k}={v}' for k, v in s2.items()))

            with open(os.path.join(OUT_DIR, f'click-{stamp}.json'), 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            print(f'\n报告：{OUT_DIR}/click-{stamp}.json')

        browser.close()
    return 0


def _classify_it(it: dict) -> str:
    """按枚举结果里的字段做分类（不依赖 DOM 句柄）。"""
    text = (it.get('text') or '').strip()
    cls = (it.get('cls') or '').lower()
    low = text.lower()
    if 'nav-link' in cls or 'page-link' in cls:
        if not any(w in low for w in DANGER_WORDS):
            return 'safe'
    if any(w in text for w in DANGER_WORDS) or any(w in low for w in DANGER_WORDS):
        return 'modal'
    return 'safe'




# ===========================================================================
# 实际点击
# ===========================================================================

if __name__ == '__main__':
    raise SystemExit(main())
