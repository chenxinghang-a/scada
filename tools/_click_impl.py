"""全量按钮点击的实现（被 tools/e2e_all_buttons.py 调用）。

拆成独立模块的原因很实际：直接在主文件里做几百行的字符串替换，
heredoc 会被截断、把文件写坏过一次。单独一个文件，改起来只有这一处。

## 核心策略：每个按钮前都重载一次页面

早期版本用"每次点前重新枚举 + 已点集合"，看起来聪明，实际被动态 DOM 搞死：
devices 页点「添加」会弹出 modal，modal 里也有个「添加」（提交按钮），
签名与页面上那个一模一样 → "发现新元素 → 点 → 又出现 → 再点"，
实测连点 35 次不收敛。

枚举阶段已经拿到了稳定清单，点击阶段对清单里每一项都以**干净页面**为起点。
状态不累积，才能保证"点满清单"而不是"点什么算什么"。
代价是每项多 2.4s 加载。
"""

from __future__ import annotations

import os
import sys

BASE = 'http://127.0.0.1:5000'
OUT_DIR = os.path.join('build', 'e2e', 'all_buttons')

SELECTOR = ('button, a.btn, input[type="submit"], input[type="button"], '
            '[role="button"], .btn-group .btn, .pagination .page-link, '
            '[data-bs-toggle="tab"], [data-bs-toggle="dropdown"], '
            '[data-bs-toggle="modal"], [data-bs-toggle="collapse"], '
            '.dropdown-item')


def _visible(el) -> bool:
    try:
        return el.is_visible() and el.is_enabled()
    except Exception:
        return False


def _sig(el):
    try:
        return (el.evaluate('e => e.tagName.toLowerCase()'),
                (el.inner_text() or '').strip()[:40],
                (el.get_attribute('class') or '')[:30])
    except Exception:
        return None


def _find(page, sig, nth: int = 0):
    """按签名定位；同名元素取第 nth 个。

    设备卡片上 12 个「详情」按钮 tag/text/class 完全一样，只按签名找
    永远命中第一个 —— 另外 11 个就被漏掉了。
    """
    hit = 0
    for el in page.query_selector_all(SELECTOR):
        if not _visible(el):
            continue
        if _sig(el) == sig:
            if hit == nth:
                return el
            hit += 1
    return None


def click_page(page, name, path, items, out_dir: str = OUT_DIR):
    results = []
    reqs_all: list = []
    errs_all: list = []
    dialogs: list = []

    def _on_request(r):
        reqs_all.append((r.method, r.url))

    def _on_console(m):
        if m.type == 'error':
            errs_all.append(m.text[:120])

    def _on_dialog(d):
        # 原生 confirm/alert/prompt 一律取消：点了按钮就算"按过"，
        # 真删/真清空不在本次验收范围 —— 要看的是确认框出现与文案。
        try:
            dialogs.append({'type': d.type, 'message': (d.message or '')[:120]})
        except Exception as exc:
            print(f"[warn] 读取对话框信息失败: {exc}", file=sys.stderr)
        try:
            d.dismiss()
        except Exception as exc:
            print(f"[warn] 关闭对话框失败: {exc}", file=sys.stderr)

    page.on('request', _on_request)
    page.on('console', _on_console)
    page.on('dialog', _on_dialog)

    # 同签名序号：同签名的第 k 个 = 第 k 个目标
    cnt: dict = {}
    plan = []
    for it in items:
        sg = (it['tag'], it['text'], it['cls'][:30])
        k = cnt.get(sg, 0)
        cnt[sg] = k + 1
        plan.append((it, sg, k))

    try:
        for idx, (item, sig, nth) in enumerate(plan):
            rec = {'page': name, 'i': idx, 'level': item.get('level', 'safe'),
                   'tag': item['tag'], 'text': item['text'], 'cls': item['cls']}

            try:
                page.goto(BASE + path, wait_until='domcontentloaded', timeout=45000)
                page.wait_for_timeout(2400)
            except Exception as e:
                rec['status'] = 'page-load-failed'
                rec['error'] = type(e).__name__
                results.append(rec)
                continue

            el = _find(page, sig, nth)
            if el is None:
                rec['status'] = 'not-found'
                results.append(rec)
                continue

            r0, e0, d0 = len(reqs_all), len(errs_all), len(dialogs)
            before_url = page.url
            before_modals = page.locator('.modal.show').count()
            try:
                before_len = page.evaluate('() => document.body.innerText.length')
            except Exception:
                before_len = 0

            try:
                el.click(timeout=6000)
            except Exception as e:
                rec['status'] = 'click-failed'
                rec['error'] = f'{type(e).__name__}: {str(e)[:70]}'
                results.append(rec)
                continue

            page.wait_for_timeout(1000)

            # 截图 —— 这就是验收证据
            shot_rel = ''
            try:
                safe_txt = ''.join(ch if ch.isalnum() or ch in '-_' else ''
                                   for ch in (item['text'] or 'notxt'))[:16] or 'notxt'
                shot_rel = os.path.join('shots', f'{name}_{idx:03d}_{safe_txt}.png')
                page.screenshot(path=os.path.join(out_dir, shot_rel), full_page=False)
            except Exception as e:
                rec['shot_error'] = type(e).__name__

            api_reqs = [r for r in reqs_all[r0:] if '/api/' in r[1]]
            new_errs = errs_all[e0:]
            new_dialogs = dialogs[d0:]
            try:
                after_len = page.evaluate('() => document.body.innerText.length')
            except Exception:
                after_len = before_len
            after_modals = page.locator('.modal.show').count()
            after_url = page.url

            rec.update({
                'api_requests': [f'{m} {u.split("127.0.0.1:5000")[-1]}' for m, u in api_reqs][:4],
                'modal_shown': after_modals > before_modals,
                'url_changed': after_url != before_url,
                'dom_len_delta': after_len - before_len,
                'dialogs': new_dialogs[:1],
                'console_errors': new_errs[:2],
                'shot': shot_rel,
            })

            if new_errs:
                rec['status'] = 'console-error'
            elif api_reqs or rec['modal_shown'] or rec['url_changed'] or new_dialogs:
                rec['status'] = 'ok'
            elif abs(rec['dom_len_delta']) > 20:
                rec['status'] = 'ok-dom'
            else:
                rec['status'] = 'no-feedback'

            results.append(rec)
            if len(results) % 10 == 0:
                print(f'    {name}: {len(results)}/{len(plan)} …', flush=True)
    finally:
        page.remove_listener('request', _on_request)
        page.remove_listener('console', _on_console)
        page.remove_listener('dialog', _on_dialog)

    return results
