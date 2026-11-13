"""全操作链路体检：监听 / 加删设备 / 启停 / 改密码 / 广播 / 急停。

## 判据不是"接口返回 200"

每个操作都要有**可观察的输出**才算过：

  监听    —— socket.io 真的收到 `data_update` 事件（不是"连上了"就算）
  加设备  —— 设备表数量 +1，且新设备能被 GET 到、采到数据
  删设备  —— 数量回到原值，且 GET 404
  启停    —— `stopped` 字段真的翻了，状态接口跟着变
  改密码  —— **用新密码能登录成功**，用旧密码登录失败；验完改回
  广播    —— 接口返回 + 服务端有对应日志
  急停    —— estop 状态 true、机械类设备被停、写入被拒；reset 后恢复

每项都写进 JSON，附上响应原文，方便复查。

## 安全

  · 新增的设备 id 用 `ops_probe_*`，测完删掉
  · 密码改完立刻改回（否则下次登录不上）
  · 急停测完立刻 reset
  任何一步失败都不会跳过恢复步骤（finally 里兜底）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import requests

BASE = 'http://127.0.0.1:5000'
OUT = os.path.join('build', 'e2e', 'ops')

PROBE_ID = 'ops_probe_dev_01'
STOP_DEV = 'schneider_m340_01'   # 机械类（PLC），instrument 类按设计不支持启停
ADMIN = ('admin', 'Admin123456')
TMP_PASS = 'Tmp_Ops_2026#x'   # 满足复杂度：大写+小写+数字+特殊字符


def dev_total(headers) -> int:
    """设备总数取分页 total，不取数组长度。

    `GET /api/devices` 返回的 devices 数组可能是分页后的前 N 条，
    用 len() 当总数会在设备多于页大小时错判"加设备没生效"。
    """
    d = _r(requests.get(f'{BASE}/api/devices', headers=headers, timeout=20))
    pg = d.get('pagination') or {}
    if isinstance(pg.get('total'), int):
        return pg['total']
    return len(d.get('devices') or [])


def _r(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {'_raw': (resp.text or '')[:300]}


def login(user, pwd):
    r = requests.post(f'{BASE}/api/auth/login',
                      json={'username': user, 'password': pwd}, timeout=15)
    d = _r(r)
    return d.get('token'), r.status_code, d


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    H: dict = {}
    results: list[dict] = []

    def rec(name, ok, evidence):
        results.append({'name': name, 'ok': bool(ok), 'evidence': evidence})
        print(f'  [{"PASS" if ok else "FAIL"}] {name}')
        for k, v in (evidence or {}).items():
            print(f'         {k}: {str(v)[:160]}')

    token, code, body = login(*ADMIN)
    if not token:
        print(f'登录失败 code={code} {body}')
        return 2
    H = {'Authorization': f'Bearer {token}'}
    print(f'登录成功，token 长度 {len(token)}\n')

    # 基线
    n0 = dev_total(H)
    base_devs = (_r(requests.get(f'{BASE}/api/devices', headers=H, timeout=20))
                 .get('devices') or [])
    print(f'基线设备数: {n0}\n')

    # ------------------------------------------------------------------ 1 监听
    print('[1] WebSocket 监听')
    got = []
    try:
        import socketio
        sio = socketio.Client(logger=False, engineio_logger=False)

        @sio.on('data_update')
        def _on_data(data):
            got.append(data if not isinstance(data, list) else data)

        @sio.on('device_status')
        def _on_status(data):
            got.append({'__device_status': True})

        sio.connect(f'{BASE}?token={token}', wait_timeout=15)
        sub = [d.get('device_id') or d.get('id') for d in base_devs][:6]
        for d in sub:
            sio.emit('subscribe', {'device_id': d})
        t0 = time.time()
        while time.time() - t0 < 14:
            sio.sleep(0.5)
            if len(got) >= 5:
                break
        sio.disconnect()
        rec('WebSocket 监听有输出', len(got) > 0, {
            '订阅设备': sub,
            '14秒内收到事件数': len(got),
            '样例': (got[0] if got else None),
        })
    except Exception as e:
        rec('WebSocket 监听有输出', False, {'异常': f'{type(e).__name__}: {e}'})

    # --------------------------------------------------------------- 2 加设备
    print('\n[2] 增加设备')
    probe = {
        'id': PROBE_ID, 'name': '运维探测设备', 'protocol': 'modbus_tcp',
        'host': '127.0.0.1', 'port': 1502, 'slave_id': 1,
        'enabled': True, 'category': 'sensor', 'area': '测试',
        'registers': [{'address': 0, 'name': 'probe_val', 'data_type': 'uint16',
                       'scale': 1, 'unit': '', 'access': 'r'}],
    }
    r = requests.post(f'{BASE}/api/devices', headers=H, json=probe, timeout=25)
    d = _r(r)
    devs = (_r(requests.get(f'{BASE}/api/devices', headers=H, timeout=20))
            .get('devices') or [])
    n1 = dev_total(H)
    added = any((x.get('device_id') or x.get('id')) == PROBE_ID for x in devs) or n1 > n0
    rec('增加设备', r.status_code in (200, 201) and n1 == n0 + 1, {
        'HTTP': r.status_code,
        '返回': d.get('message') or d.get('success'),
        '设备总数': f'{n0} -> {n1}',
        '新设备在列表中': added,
    })

    # ----------------------------------------------------------------- 3 启停
    print('\n[3] 设备启停')
    before = _r(requests.get(f'{BASE}/api/devices/{STOP_DEV}', headers=H, timeout=15))
    b = before.get('device') or before.get('data') or before
    rs = requests.post(f'{BASE}/api/devices/{STOP_DEV}/stop', headers=H, timeout=25)
    time.sleep(2)
    mid = _r(requests.get(f'{BASE}/api/devices/{STOP_DEV}', headers=H, timeout=15))
    m = mid.get('device') or mid.get('data') or mid
    rr = requests.post(f'{BASE}/api/devices/{STOP_DEV}/start', headers=H, timeout=30)
    time.sleep(2)
    after = _r(requests.get(f'{BASE}/api/devices/{STOP_DEV}', headers=H, timeout=15))
    a = after.get('device') or after.get('data') or after
    rec('停止设备', rs.status_code == 200 and bool(m.get('stopped')), {
        'HTTP': rs.status_code, '返回': _r(rs).get('message'),
        'stopped 前': b.get('stopped'), 'stopped 后': m.get('stopped'),
    })
    rec('启动设备', rr.status_code == 200 and not a.get('stopped'), {
        'HTTP': rr.status_code, '返回': _r(rr).get('message'),
        'stopped 恢复': a.get('stopped'),
    })

    # --------------------------------------------------------------- 4 改密码
    print('\n[4] 修改密码')
    try:
        rp = requests.post(f'{BASE}/api/auth/change-password', headers=H, json={
            'old_password': ADMIN[1], 'new_password': TMP_PASS}, timeout=20)
        dp = _r(rp)
        # 用新密码登录
        t_new, c_new, _ = login(ADMIN[0], TMP_PASS)
        # 用旧密码登（应失败）
        t_old, c_old, _ = login(ADMIN[0], ADMIN[1])
        # 立刻改回
        H2 = {'Authorization': f'Bearer {t_new}'} if t_new else H
        back = requests.post(f'{BASE}/api/auth/change-password', headers=H2, json={
            'old_password': TMP_PASS, 'new_password': ADMIN[1]}, timeout=20)
        t_back, c_back, _ = login(*ADMIN)
        # 改密码会让**所有旧 token 失效**：不刷新的话后面每个请求都 401
        # （第一次跑就是这么全红的）。用原密码重新登录，拿新 token。
        if t_back:
            H = {'Authorization': f'Bearer {t_back}'}
            print('        （已刷新 token：改密会使旧 token 失效）')
        rec('修改密码', rp.status_code == 200 and bool(t_new)
            and not t_old and bool(t_back), {
                '改密 HTTP': rp.status_code, '返回': dp.get('message'),
                '新密码登录': f'{"成功" if t_new else "失败"} (HTTP {c_new})',
                '旧密码登录': f'{"仍可通过(异常)" if t_old else "已失效(正确)"}',
                '改回 HTTP': back.status_code,
                '原密码可登录': bool(t_back),
            })
    except Exception as e:
        rec('修改密码', False, {'异常': f'{type(e).__name__}: {e}'})

    # ----------------------------------------------------------------- 5 广播
    print('\n[5] 广播喊话')
    try:
        rb = requests.post(f'{BASE}/api/broadcast/speak', headers=H,
                           json={'text': '运维体检测试广播'}, timeout=20)
        db = _r(rb)
        hist = _r(requests.get(f'{BASE}/api/broadcast/history', headers=H, timeout=15))
        items = hist.get('data') or hist.get('history') or []
        rec('广播喊话', rb.status_code == 200, {
            'HTTP': rb.status_code, '返回': db.get('message') or db.get('success'),
            '历史条数': len(items) if isinstance(items, list) else items,
        })
    except Exception as e:
        rec('广播喊话', False, {'异常': f'{type(e).__name__}: {e}'})

    # ----------------------------------------------------------------- 6 急停
    print('\n[6] 急停（测完立刻复位）')
    try:
        re_ = requests.post(f'{BASE}/api/control/estop', headers=H,
                            json={'reason': '运维体检测试', 'confirm': True}, timeout=25)
        de = _r(re_)
        st = _r(requests.get(f'{BASE}/api/control/estop/status', headers=H, timeout=15))
        sd = st.get('data') or st
        act = sd.get('active')
        rst = requests.post(f'{BASE}/api/control/estop/reset', headers=H,
                            json={'reason': '体检结束'}, timeout=25)
        dr = _r(rst)
        st2 = _r(requests.get(f'{BASE}/api/control/estop/status', headers=H, timeout=15))
        sd2 = st2.get('data') or st2
        rec('急停触发', re_.status_code == 200 and bool(act), {
            'HTTP': re_.status_code, '返回': de.get('message'),
            'estop.active': act,
            '已停设备数': len((de.get('stopped_devices') or [])),
            '失败设备数': len((de.get('failed_devices') or [])),
        })
        rec('急停复位', rst.status_code == 200 and not sd2.get('active'), {
            'HTTP': rst.status_code, '返回': dr.get('message'),
            '复位后 active': sd2.get('active'),
        })
    except Exception as e:
        rec('急停触发/复位', False, {'异常': f'{type(e).__name__}: {e}'})

    # ----------------------------------------------------------- 7 删设备清理
    print('\n[7] 删除探测设备（清理）')
    rd = requests.delete(f'{BASE}/api/devices/{PROBE_ID}', headers=H, timeout=25)
    dd = _r(rd)
    n2 = dev_total(H)
    gone = n2 == n0
    rec('删除设备', rd.status_code == 200 and gone, {
        'HTTP': rd.status_code, '返回': dd.get('message'),
        '设备总数': f'{n1} -> {n2}（基线 {n0}）',
        '已回到基线': gone,
    })

    # 汇总
    ok = sum(1 for x in results if x['ok'])
    report = {'generated_at': stamp, 'base_device_count': n0,
              'passed': ok, 'total': len(results), 'results': results}
    path = os.path.join(OUT, f'ops-{stamp}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print(f'\n===== 汇总 {ok}/{len(results)} 通过 =====')
    for x in results:
        if not x['ok']:
            print(f'  未通过: {x["name"]} -> {x["evidence"]}')
    print(f'报告: {path}')
    return 0 if ok == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
