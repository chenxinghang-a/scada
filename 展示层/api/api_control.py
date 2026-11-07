"""
设备控制API
寄存器写入/线圈写入/紧急停机/安全联锁/操作审计
"""

import logging
import time
from typing import Optional
from functools import wraps
from flask import Blueprint, jsonify, request, current_app

from 用户层.auth import jwt_required, role_required
from ._common import get_auth_manager, api_error_handler, api_error, safe_int, safe_float, clamp_limit
from core.safe_numeric import try_finite_float
from core.service_response import module_unavailable_response

logger = logging.getLogger(__name__)

control_bp = Blueprint('api_control', __name__, url_prefix='/api')

_require_auth = jwt_required
_require_engineer = role_required('admin', 'engineer')

# 幂等性：记录最近的写操作，防止重复提交
_recent_writes: dict[str, float] = {}
_write_lock = __import__('threading').Lock()
IDEMPOTENCY_WINDOW_S = 2  # 2秒内的重复写入视为同一操作
RECENT_WRITES_MAX = 1000  # 幂等性记录最大条数

# 「写入进行中」占位标记。
# 幂等键必须在**下发之前**登记，否则挡不住并发的重复提交；但登记 ≠ 写入成功，
# 所以登记时先放这个哨兵，写入结束后再按结果替换成时间戳 / 撤销。
# 没有它的话：请求 A 登记键后开始下发（网络往返可能几百 ms），此时并发的
# 请求 B 命中幂等分支直接回 `写入成功(幂等)` —— 而 A 可能还没写、甚至写失败，
# B 却已经拿到"成功"。更糟的是 A 失败会撤销登记，B 的成功也就成了假账。
_IN_FLIGHT = object()


def _idempotency_begin(write_key: str):
    """登记幂等键。

    Returns:
        ``None`` —— 已登记占位，调用方可以继续下发；
        否则返回**应当直接回给客户端的响应**（重复提交 409 / 命中窗口 200）。
    """
    with _write_lock:
        last_write = _recent_writes.get(write_key)
        # ⚠️ 必须用单调时钟：墙钟被 NTP 回拨后 `now - last_write` 变负，
        # 会永远 `< IDEMPOTENCY_WINDOW_S` —— 此后所有重试都被判成"已写入"，
        # 实际一次都没下发。
        now = time.monotonic()
        if last_write is _IN_FLIGHT:
            return jsonify({
                'success': False,
                'message': '相同的写入正在执行中，请勿重复提交',
            }), 409
        if isinstance(last_write, float) and now - last_write < IDEMPOTENCY_WINDOW_S:
            return jsonify({'success': True, 'message': '写入成功(幂等)'})
        _recent_writes[write_key] = _IN_FLIGHT
        if len(_recent_writes) > RECENT_WRITES_MAX:
            for k in [k for k, v in _recent_writes.items()
                      if isinstance(v, float) and now - v >= IDEMPOTENCY_WINDOW_S]:
                _recent_writes.pop(k, None)
            if len(_recent_writes) > RECENT_WRITES_MAX:
                _recent_writes.clear()
    return None


def _idempotency_end(write_key: str, success: bool):
    """按真实结果落实幂等键：成功 → 时间戳；失败 → 撤销占位。"""
    with _write_lock:
        if success:
            _recent_writes[write_key] = time.monotonic()
        elif _recent_writes.get(write_key) is _IN_FLIGHT:
            _recent_writes.pop(write_key, None)


# 寄存器地址/值范围常量
REGISTER_ADDRESS_MIN = 0
REGISTER_ADDRESS_MAX = 65535
REGISTER_VALUE_INT16_MIN = -32768
REGISTER_VALUE_INT16_MAX = 65535


# ==================== 设备控制API ====================

# ==================== 自主等级（HOTL / HOOTL，round 378）====================

def _device_exists(device_id: str) -> bool:
    manager = getattr(current_app, 'device_manager', None)
    devices = getattr(manager, 'devices', {}) or {}
    return device_id in devices


@control_bp.route('/devices/<device_id>/autonomy', methods=['GET'])
@_require_auth
def get_device_autonomy(device_id):
    """查看设备当前控制权归属：manual / supervised / automatic。"""
    dc = getattr(current_app, 'device_control', None)
    if dc is None:
        return module_unavailable_response('device_control')
    if not _device_exists(device_id):
        return api_error(f'设备 {device_id} 不存在', 404)
    return jsonify({'success': True, **dc.get_autonomy(device_id)})


@control_bp.route('/devices/<device_id>/autonomy', methods=['POST'])
@_require_engineer
def set_device_autonomy(device_id):
    """切换自主等级（运行期生效，不写回配置文件）。"""
    dc = getattr(current_app, 'device_control', None)
    if dc is None:
        return module_unavailable_response('device_control')
    if not _device_exists(device_id):
        return api_error(f'设备 {device_id} 不存在', 404)

    data = request.get_json() or {}
    mode = data.get('mode')
    operator = request.current_user['username']
    ok, message = dc.set_autonomy_mode(
        device_id, mode, operator, str(data.get('reason') or ''))
    if not ok:
        return api_error(message, 400)

    # 可选：把等级写回 devices*.yaml，否则只是运行期覆盖（重启回到配置值）
    persisted = False
    if data.get('persist'):
        manager = current_app.device_manager
        dev = getattr(manager, 'devices', {}).get(device_id)
        if not isinstance(dev, dict):
            return api_error(f'设备 {device_id} 配置不可写', 400)
        previous = dev.get('autonomy')
        dev['autonomy'] = message
        if not manager._save_config():
            dev['autonomy'] = previous
            logger.error("设备 %s 自主等级已生效但落盘失败", device_id)
            return jsonify({
                'success': False,
                'message': '自主等级已生效，但写入配置文件失败；重启后会恢复原等级',
                'runtime_applied': True,
                'persisted': False,
            }), 500
        persisted = True

    get_auth_manager().log_operation(
        operator, 'set_autonomy', f'设备 {device_id} 自主等级 → {message}')
    return jsonify({'success': True, 'mode': message, 'persisted': persisted,
                    **dc.get_autonomy(device_id)})


@control_bp.route('/devices/<device_id>/autonomy/arm', methods=['POST'])
@_require_engineer
def arm_device_autonomy(device_id):
    """人在环上（supervised）：授权一段时间内的自动写入。"""
    dc = getattr(current_app, 'device_control', None)
    if dc is None:
        return module_unavailable_response('device_control')
    if not _device_exists(device_id):
        return api_error(f'设备 {device_id} 不存在', 404)

    data = request.get_json() or {}
    try:
        minutes = safe_int(data.get('minutes', 60), 'minutes')
    except ValueError as e:
        return api_error(str(e), 400)

    operator = request.current_user['username']
    ok, message = dc.arm_autonomy(device_id, operator, minutes)
    if not ok:
        return api_error(message, 400)

    get_auth_manager().log_operation(
        operator, 'arm_autonomy', f'设备 {device_id} 自动控制武装 {minutes} 分钟')
    return jsonify({'success': True, 'message': message,
                    **dc.get_autonomy(device_id)})


@control_bp.route('/devices/<device_id>/autonomy/disarm', methods=['POST'])
@_require_engineer
def disarm_device_autonomy(device_id):
    """人在环上（supervised）：人工接管，立即撤销自动写入授权。"""
    dc = getattr(current_app, 'device_control', None)
    if dc is None:
        return module_unavailable_response('device_control')
    if not _device_exists(device_id):
        return api_error(f'设备 {device_id} 不存在', 404)

    operator = request.current_user['username']
    existed = dc.disarm_autonomy(device_id, operator)
    if existed:
        get_auth_manager().log_operation(
            operator, 'disarm_autonomy', f'设备 {device_id} 自动控制解除武装')
    return jsonify({'success': True, 'was_armed': existed,
                    **dc.get_autonomy(device_id)})


# ==================== HITL 待审批动作（round 378）====================

@control_bp.route('/control/pending-actions', methods=['GET'])
@_require_auth
def list_pending_actions():
    """列出等待人工审批的自动写入提案（可按设备过滤）。"""
    dc = getattr(current_app, 'device_control', None)
    if dc is None:
        return module_unavailable_response('device_control')
    device_id = request.args.get('device_id')
    return jsonify({
        'success': True,
        'actions': dc.get_pending_actions(device_id=device_id),
    })


@control_bp.route('/control/pending-actions/<action_id>/approve', methods=['POST'])
@_require_engineer
def approve_pending_action(action_id):
    """批准一条提案并执行；可传 `value` 修改后再执行。"""
    dc = getattr(current_app, 'device_control', None)
    if dc is None:
        return module_unavailable_response('device_control')

    data = request.get_json(silent=True) or {}
    value = data.get('value')
    if value is not None:
        try:
            value = safe_float(value, 'value')
        except ValueError as e:
            return api_error(str(e), 400)

    operator = request.current_user['username']
    handled, result = dc.approve_pending(action_id, operator, value)
    if not handled:
        return api_error(str(result), 404)

    get_auth_manager().log_operation(
        operator, 'approve_pending_action', f'批准自动动作 {action_id}')
    if isinstance(result, dict) and result.get('success'):
        return jsonify(result)
    return jsonify(result if isinstance(result, dict)
                   else {'success': False, 'message': str(result)}), 400


@control_bp.route('/control/pending-actions/<action_id>/reject', methods=['POST'])
@_require_engineer
def reject_pending_action(action_id):
    """驳回一条自动写入提案。"""
    dc = getattr(current_app, 'device_control', None)
    if dc is None:
        return module_unavailable_response('device_control')

    data = request.get_json(silent=True) or {}
    operator = request.current_user['username']
    ok = dc.reject_pending(action_id, operator, str(data.get('reason') or ''))
    if not ok:
        return api_error('待审批动作不存在或已处理', 404)

    get_auth_manager().log_operation(
        operator, 'reject_pending_action', f'驳回自动动作 {action_id}')
    return jsonify({'success': True, 'action_id': action_id})


@control_bp.route('/devices/<device_id>/write-register', methods=['POST'])
@_require_engineer
def write_register(device_id):
    """写入寄存器（带安全校验）"""
    data = request.get_json()
    if not data:
        return api_error('请提供写入参数')

    address = data.get('address')
    value = data.get('value')
    if address is None or value is None:
        return api_error('缺少address或value参数')

    operator = request.current_user['username']

    try:
        address = safe_int(address, 'address')
        # 支持整数和浮点值写入（float32寄存器需要浮点支持）
        value = safe_float(value, 'value')
    except ValueError as e:
        return api_error(str(e))

    # ---- 请求级校验（400/404 语义，与"被安全层拒绝"的 403 区分开）----
    #
    # round 322：这些检查原本只写在"无安全模块"的降级分支里，删降级分支时
    # 一起被带走了。它们属于**请求合法性**，不是安全决策，必须在最前面：
    #   · 400 —— 调用方传错了（地址/值不合法、设备未连接）
    #   · 404 —— 设备不存在
    #   · 403 —— 请求合法，但被安全层拒绝（急停/联锁/量程）
    #   · 503 —— 安全层本身不可用（fail-closed，见下）
    if address < REGISTER_ADDRESS_MIN or address > REGISTER_ADDRESS_MAX:
        return api_error(f'寄存器地址超出范围 ({REGISTER_ADDRESS_MIN}-{REGISTER_ADDRESS_MAX})')
    if not (REGISTER_VALUE_INT16_MIN <= value <= REGISTER_VALUE_INT16_MAX):
        return api_error(f'写入值 {value} 超出安全范围 '
                         f'({REGISTER_VALUE_INT16_MIN}~{REGISTER_VALUE_INT16_MAX})')

    client = current_app.device_manager.get_client(device_id)
    if not client:
        return api_error(f'设备 {device_id} 不存在', 404)
    if not getattr(client, 'connected', False):
        return api_error(f'设备 {device_id} 未连接')

    # 幂等性检查（**在主路径上**，round 322 从已删除的降级分支搬上来）
    #
    # 为什么必须有：设定值写入是**非幂等**的物理动作，重复下发可能让设备
    # 连续两次改变状态（例如"启动→停止"被点了两下）。原来这段保护只写在
    # "无安全模块"的降级分支里 —— 也就是说**保护只存在于那条不可达的路径**，
    # 真正在跑的这条完全没有防重复。
    write_key = f"{device_id}:{address}:{value}"
    early = _idempotency_begin(write_key)
    if early is not None:
        return early

    # 工厂级安全校验
    device_control = getattr(current_app, 'device_control', None)
    if device_control is None:
        # 无安全模块 → **拒绝写入**（fail-closed）
        #
        # round 322：原来这里"降级"成基础校验（只查 INT16 范围 + 地址范围），
        # 然后直接下发。也就是说：**安全模块不可用时，急停、联锁、旁路、
        # 二次确认、回读验证全部消失，写入照样成功** ——
        # 而调用方看到的是同一个 200，没有任何区别。
        # 安全层的正确降级方向是"拒绝"，不是"用弱校验继续干"。
        _idempotency_end(write_key, success=False)
        logger.error(
            "拒绝写入：安全模块(device_control)不可用 —— 无法做急停/联锁/量程/回读校验。"
            " device=%s address=%s value=%s operator=%s",
            device_id, address, value, operator)
        return jsonify({
            'success': False,
            'message': '安全模块不可用，已拒绝写入（无法执行急停/联锁/量程/回读校验）',
        }), 503

    success = False
    try:
        result = device_control.write_with_verification(
            device_id, address, value, operator)
        success = bool(result.get('success'))
        if not success:
            return jsonify(result), 403
        return jsonify(result)
    finally:
        # 按真实结果落实幂等键：失败/异常一律撤销占位，让重试能真正下发。
        _idempotency_end(write_key, success)


@control_bp.route('/devices/<device_id>/write-coil', methods=['POST'])
@_require_engineer
def write_coil(device_id):
    """写入线圈（带安全校验）"""
    data = request.get_json()
    if not data:
        return api_error('请提供写入参数')

    address = data.get('address')
    value = data.get('value')
    if address is None or value is None:
        return api_error('缺少address或value参数')

    operator = request.current_user['username']

    try:
        address = safe_int(address, 'address')
    except ValueError as e:
        return api_error(str(e))

    # 统一布尔值转换（字符串 "false"/"0" 也应视为 False）
    if isinstance(value, str):
        bool_val = value.lower() not in ('false', '0', 'no', '')
    else:
        bool_val = bool(value)

    # 幂等性检查（round 369）：线圈同样是**非幂等**的物理动作，
    # 重复下发会让启停位、阀门位抖动。原来只有 write-register 做了防重。
    write_key = f"{device_id}:coil:{address}:{bool_val}"
    early = _idempotency_begin(write_key)
    if early is not None:
        return early

    # 工厂级安全校验
    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        # ⚠️ 必须显式传 kind='coil'。
        # 不传时 `write_with_verification` 默认按寄存器走，而 Modbus 客户端
        # 同时具备 write_single_register / write_single_coil —— 于是「写线圈」
        # 实际写进了**同一地址的保持寄存器**（数据字 vs 开关位，现场是两个对象）。
        success = False
        try:
            result = device_control.write_with_verification(
                device_id, address, 1 if bool_val else 0, operator, kind='coil')
            success = bool(result.get('success'))
            if not success:
                return jsonify(result), 403
            return jsonify(result)
        finally:
            _idempotency_end(write_key, success)

    # round 338：**无安全模块 → 拒绝写入**，与 write-register 的口径对齐。
    #
    # 原来这里「降级」成直接写 PLC：安全模块不可用时，急停、联锁、旁路、
    # 回读验证**全部消失**，线圈照写 —— 而 write-register（117-135 行）
    # 早在 round 322 就改成 503 拒绝了。
    #
    # 同一族的两条路径对「安全模块缺失」的处理**必须等价**，
    # 否则等于「挑一条闸门少的路径去写」。这正是本轮「写路径收敛」要抓的形状。
    logger.error(
        "拒绝线圈写入：安全模块(device_control)不可用 —— 无法做急停/联锁/回读校验。"
        " device=%s address=%s value=%s operator=%s",
        device_id, address, value, operator)
    _idempotency_end(write_key, success=False)
    return jsonify({
        'success': False,
        'message': '安全模块不可用，已拒绝写入（无法执行急停/联锁/回读校验）',
    }), 503


@control_bp.route('/devices/<device_id>/adjust', methods=['POST'])
@_require_engineer
def adjust_device(device_id):
    """调节设备参数（写入指定寄存器值，如温度设定值、速度等）"""
    data = request.get_json()
    if not data:
        return api_error('请提供调节参数')

    register_name = data.get('register_name')
    value = data.get('value')
    if register_name is None or value is None:
        return api_error('缺少 register_name 或 value 参数')

    try:
        value = safe_float(value, 'value')
    except ValueError as e:
        return api_error(str(e))

    # round 341：**改走安全层**，不再直接调 device_manager.adjust_device。
    #
    # 原实现绕过的是：急停检查、联锁检查、量程校验、审计、回读验证，
    # 而且内部 `int(value)` 会把 1.9 静默截成 1（PLC 收到 1，界面显示 1.9）。
    # `/write-register` 走的是 write_with_verification（这些都有），
    # `/adjust` 却走另一条 —— 同一件事两条路、闸门不同。
    operator = request.current_user['username']

    device_control = getattr(current_app, 'device_control', None)
    if device_control is None:
        return jsonify({
            'success': False,
            'message': '安全模块不可用，已拒绝调节（无法执行急停/联锁/量程/回读校验）',
        }), 503

    address = device_control._resolve_register_address(device_id, register_name)
    if address is None:
        return jsonify({
            'success': False,
            'message': f'设备 {device_id} 没有点位 {register_name!r}，拒绝写入'
                       f'（地址未知就无法做量程与联锁校验）',
        }), 400

    # 幂等性检查（round 369）：调节也是物理写入，重复提交会连续改设定值。
    write_key = f"{device_id}:adjust:{register_name}:{value}"
    early = _idempotency_begin(write_key)
    if early is not None:
        return early

    success = False
    try:
        # round 370（BUG-052）：`/adjust` 传的是**工程值**（50.0°C），
        # 必须让安全层按 scale/offset 换算并按 data_type 决定单/双寄存器。
        # 原来按原始整数写：小数直接被拒（403），整数则写了未缩放的值。
        result = device_control.write_with_verification(
            device_id, address, value, operator, kind='register',
            engineering=True) or {}
        success = bool(result.get('success'))
        payload = {
            'success': success,
            'message': result.get('message') or (
                '调节成功' if success else '调节失败'),
            # 把回读结论透传出去：前端要能区分「写成功且回读一致」
            # 与「写成功但没读到回读值」（通信断时 verified=None）
            'verified': result.get('verified'),
            'value': value,
            'address': address,
        }
        # ⚠️ round 369：失败必须回 4xx，不能回 200。
        # 原来无论 result.success 真假都 return jsonify(...) ——
        # 急停中/联锁拦截/回读不一致/超量程时前端收到的是 HTTP 200，
        # 只看状态码的调用方会把"没写进去"当成"已经调好了"。
        if not success:
            return jsonify(payload), 403
        return jsonify(payload)
    finally:
        _idempotency_end(write_key, success)


@control_bp.route('/devices/<device_id>/write-endpoint', methods=['POST'])
@_require_engineer
def write_endpoint(device_id):
    """写入REST端点（支持POST/PUT方法）"""
    data = request.get_json() or {}
    endpoint_name = data.get('endpoint')
    value = data.get('value')
    method = data.get('method', 'PUT').upper()

    if not endpoint_name:
        return api_error('请指定端点名称')

    if method not in ('POST', 'PUT'):
        return api_error('不支持的HTTP方法，仅支持POST/PUT')

    operator = request.current_user['username']

    # ==================== 安全闸门（round 331）====================
    #
    # 这是 REST 型设备**唯一的写通道**，但原来**完全绕过安全层**：
    # 直接 `device_manager.get_client()` 拿客户端就写 —— 急停中可写、
    # 联锁触发中可写、安全审计链里没有这条记录、无回读确认。
    # 同文件的 write-register / write-coil 都走 device_control，唯独这条漏了。
    #
    # 为什么不直接复用 `write_with_verification`：那个接口的语义是
    # 「寄存器地址 + 量程 + 回读比对」，而端点写入是「HTTP path + body」，
    # 没有寄存器地址可比对。所以这里做**同等强度的三项前置闸门**：
    # 安全模块存在性、急停状态、目标设备的已触发联锁，以及审计落盘。
    device_control = getattr(current_app, 'device_control', None)
    if device_control is None:
        logger.error(
            "拒绝端点写入：安全模块(device_control)不可用 —— 无法做急停/联锁校验。"
            " device=%s endpoint=%s operator=%s", device_id, endpoint_name, operator)
        return jsonify({
            'success': False,
            'message': '安全模块不可用，已拒绝写入（无法执行急停/联锁校验）',
        }), 503

    # ⚠️ round 333：**不碰私有属性**。
    #
    # 初版（round 331）写的是
    #   getattr(dc, '_estop_active', False) / '_interlock_rules' / '_interlock_bypass'
    # —— 三处默认值**全部指向「放行」**。属性改名或缺失时（DI 替换、测试桩、
    # 将来重构），急停中仍然可以下发端点写入，**而且没有任何告警**。
    # 这正是本项目 330 轮反复批判的 fail-open 形状，不能自己再犯一次。
    #
    # 改用公共方法 `get_estop_status()` / `get_interlock_status()`；
    # **取不到、或返回结构不符，一律 503** —— 不默认 False / 不默认空集合。
    try:
        estop = device_control.get_estop_status()
    except Exception as e:      # noqa: BLE001 —— 读不到状态就不许写
        logger.error("读取急停状态失败，拒绝端点写入: %s", e, exc_info=True)
        return jsonify({
            'success': False,
            'message': '无法读取急停状态，已拒绝写入（安全状态未知不允许下发）',
        }), 503
    if not isinstance(estop, dict) or 'active' not in estop:
        logger.error("急停状态接口返回格式不符: %r", estop)
        return jsonify({
            'success': False,
            'message': '急停状态接口返回格式不符，已拒绝写入',
        }), 503
    if estop['active']:
        logger.warning("急停状态下拒绝端点写入: device=%s endpoint=%s operator=%s",
                       device_id, endpoint_name, operator)
        return jsonify({
            'success': False,
            'message': '紧急停机状态中，禁止下发端点写入（请先解除急停）',
        }), 403

    try:
        interlocks = device_control.get_interlock_status()
    except Exception as e:      # noqa: BLE001
        logger.error("读取联锁状态失败，拒绝端点写入: %s", e, exc_info=True)
        return jsonify({
            'success': False,
            'message': '无法读取联锁状态，已拒绝写入（安全状态未知不允许下发）',
        }), 503
    if not isinstance(interlocks, dict):
        logger.error("联锁状态接口返回格式不符: %r", type(interlocks))
        return jsonify({
            'success': False,
            'message': '联锁状态接口返回格式不符，已拒绝写入',
        }), 503

    for _rule_id, _st in interlocks.items():
        if not isinstance(_st, dict):
            continue
        if not _st.get('triggered'):
            continue
        if _st.get('bypassed'):
            continue
        # round 333：**同时匹配 condition.device_id 与 action.device_id**。
        # 原来只看 action.device_id —— 将来配一条
        # `action: {type: emergency_stop}`（没有 device_id）的规则时，
        # 它触发后该设备的端点写入仍能过。
        _act = _st.get('action') or {}
        _cond = _st.get('condition') or {}
        _targets = {_act.get('device_id'), _cond.get('device_id')}
        if device_id in _targets:
            logger.warning("联锁触发中拒绝端点写入: device=%s rule=%s operator=%s",
                           device_id, _rule_id, operator)
            return jsonify({
                'success': False,
                'message': f'联锁 {_rule_id} 正在触发，禁止对该设备下发端点写入',
            }), 403

    # 审计前置：写不进磁盘就不下发（round 322 的 fail-closed 原则）
    if not device_control._audit(
            f'write_endpoint_{method.lower()}_attempt', operator,
            f'设备 {device_id} {method}端点 {endpoint_name}={value}'):
        return jsonify({
            'success': False,
            'message': '审计写入失败，拒绝下发（无法自证的操作不允许执行）',
        }), 503

    # round 333：审计要**三态都记**（attempt / ok / failed）。
    # 原来只记了 attempt —— 成功只写通用操作日志、失败（400/500/501）一条
    # `_audit` 都没有，出事时无法自证「到底下发了没有、结果是什么」。
    # 对照 `write_with_verification` 的口径：attempt + failed + ok 三态齐全。
    def _audit_result(ok: bool, detail: str) -> None:
        try:
            device_control._audit(
                f'write_endpoint_{method.lower()}_{"ok" if ok else "failed"}',
                operator,
                f'设备 {device_id} {method}端点 {endpoint_name}={value} {detail}')
        except Exception as e:      # noqa: BLE001 —— 审计失败不该改写已发生的写入结果
            logger.error("端点写入结果审计失败: %s", e)

    device_manager = current_app.device_manager
    client = device_manager.get_client(device_id)
    if not client:
        _audit_result(False, '设备不存在')
        return api_error(f'设备 {device_id} 不存在', 404)

    # 查找端点配置
    device_config = device_manager.devices.get(device_id, {})
    endpoints = device_config.get('endpoints', [])
    endpoint_config = next((ep for ep in endpoints if ep.get('name') == endpoint_name), None)

    if not endpoint_config:
        _audit_result(False, '端点配置不存在')
        return api_error(f'端点 {endpoint_name} 不存在', 404)

    # 执行写入
    #
    # round 331：包一层 —— 原来这里裸调，任何异常（典型是客户端方法签名
    # 不匹配的 TypeError）都直穿到 app.errorhandler(500)，客户端只看到
    # `{"error": "Internal server error"}`，运维无从判断是配置错、连不上、
    # 还是代码 bug。
    success = False
    try:
        if method == 'PUT' and hasattr(client, 'put_endpoint'):
            result = client.put_endpoint(endpoint_config, {'value': value})
            success = (result or {}).get('success', False)
        elif method == 'POST' and hasattr(client, 'post_endpoint'):
            result = client.post_endpoint(endpoint_config, {'value': value})
            success = (result or {}).get('success', False)
        elif hasattr(client, 'write_endpoint'):
            # round 331：**按签名探测**，不硬编码调用形式。
            # 内置的三个客户端现在都有 `method` 形参，但外部插件、旧实现、
            # 或者将来新增的客户端可能只有 2 参 —— 用关键字硬传会让它们
            # 直接 TypeError（这正是本轮修的那个「真实设备必然 500」）。
            # 不用 try/except TypeError 猜：客户端内部的 TypeError 会被一起吞掉。
            import inspect as _inspect
            try:
                _params = _inspect.signature(client.write_endpoint).parameters
                _accepts_method = (
                    'method' in _params
                    or any(p.kind == _inspect.Parameter.VAR_KEYWORD
                           for p in _params.values()))
            except (TypeError, ValueError):
                _accepts_method = False
            if _accepts_method:
                success = client.write_endpoint(endpoint_config, value, method=method)
            else:
                success = client.write_endpoint(endpoint_config, value)
        else:
            _audit_result(False, '客户端不支持端点写入')
            return jsonify({
                'success': False,
                'message': f'设备客户端 {type(client).__name__} 不支持端点写入',
            }), 501
    except Exception as e:      # noqa: BLE001 —— 必须给出可读错误，不能只留 500
        logger.error("端点写入异常 device=%s endpoint=%s method=%s: %s",
                     device_id, endpoint_name, method, e, exc_info=True)
        _audit_result(False, f'异常: {type(e).__name__}: {e}')
        return jsonify({
            'success': False,
            'message': f'端点写入异常: {type(e).__name__}: {e}',
        }), 500

    if success:
        _audit_result(True, '成功')
        get_auth_manager().log_operation(
            operator, f'write_endpoint_{method.lower()}',
            f'设备 {device_id} {method}端点 {endpoint_name}={value}')
        return jsonify({'success': True, 'message': f'{method}写入成功: {endpoint_name}={value}'})
    _audit_result(False, '客户端返回失败')
    return jsonify({'success': False, 'message': f'{method}写入失败，请检查设备连接'}), 400


@control_bp.route('/control/logs', methods=['GET'])
@_require_auth
def get_control_logs():
    """获取控制操作日志（含安全审计）"""
    limit = clamp_limit(request.args.get('limit', 50, type=int), default=50, maximum=500)
    logs = get_auth_manager().get_operation_logs(limit=limit)
    control_logs = [log for log in logs if log.get('action') in (
        'write_register', 'write_coil', 'stop_device', 'start_device',
        'batch_stop', 'batch_start', 'batch_reset',
        'emergency_stop', 'estop_reset',
    )]

    # 合并安全审计日志
    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        audit_logs = device_control.get_audit_log(limit=limit)
        control_logs.extend(audit_logs)

    # 按时间排序
    control_logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return jsonify({'logs': control_logs[:limit]})


# ==================== 工厂级设备控制安全API ====================

@control_bp.route('/control/estop', methods=['POST'])
@_require_auth
def trigger_estop():
    """紧急停机（最高优先级，任何角色均可触发）"""
    data = request.get_json() or {}
    reason = data.get('reason', '操作员手动触发紧急停机')
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return api_error('设备控制安全模块未启用', 503)
    result = device_control.trigger_emergency_stop(reason)
    get_auth_manager().log_operation(
        request.current_user['username'], 'emergency_stop', f'紧急停机: {reason}')
    return jsonify(result)


@control_bp.route('/control/estop/reset', methods=['POST'])
@_require_engineer
def reset_estop():
    """解除紧急停机（需工程师权限）"""
    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return api_error('设备控制安全模块未启用', 503)
    result = device_control.reset_emergency_stop(operator)
    # round 369：解除失败不能记成"已解除"，也不能回 200。
    # 原来这里无条件 `log_operation(..., '解除紧急停机')` + `jsonify(result)`：
    # 底层明明有设备没起来（success=False），运维日志里却是一条成功的解除记录，
    # 前端只看 HTTP 状态也当成功。
    ok = bool(result.get('success'))
    get_auth_manager().log_operation(
        operator, 'estop_reset',
        '解除紧急停机' if ok else f'解除紧急停机未完全成功: {result.get("message", "")}')
    if not ok:
        return jsonify(result), 403
    return jsonify(result)


@control_bp.route('/control/estop/status', methods=['GET'])
@_require_auth
def get_estop_status():
    """获取紧急停机状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify(device_control.get_estop_status())


@control_bp.route('/control/interlocks', methods=['GET'])
@_require_auth
def get_interlocks():
    """获取所有安全联锁状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify(device_control.get_interlock_status())


@control_bp.route('/control/interlocks/load-summary', methods=['GET'])
@_require_auth
def get_interlock_load_summary():
    """联锁**加载摘要**（round 308）：声明几条 / 生效几条 / 丢弃几条。

    为什么需要单独一个端点：`GET /control/interlocks` 的返回是
    `{rule_id: 状态}`，只看它**无法区分**
    「配了 12 条、5 条写错被丢弃」和「本来就只有 7 条」。
    被丢弃的规则不会保护任何设备，而界面上一切正常。

    **刻意不接前端 UI**：这是运维排障与交接核查用的只读视图
    （同 `/api/devices/reload` 的处理方式），挂在联锁页上对操作员无意义。
    """
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    summary = device_control.get_interlock_load_summary()
    degraded = bool(summary.get('rejected'))
    return jsonify({
        'success': True,
        'degraded': degraded,
        'summary': summary,
        'message': (
            f"联锁部分丢弃：声明 {summary.get('declared')} 条，"
            f"生效 {summary.get('loaded')} 条，丢弃 {summary.get('rejected')} 条"
            if degraded else
            f"联锁加载正常：生效 {summary.get('loaded')} 条"
        ),
    })


@control_bp.route('/control/interlocks/<rule_id>/bypass', methods=['POST'])
@_require_engineer
def bypass_interlock(rule_id):
    """旁路联锁（维护用）—— round 306 起改走**双人审批**流程。

    原来直接调 `device_control.bypass_interlock()`，而那个方法自己的 docstring
    就写着「[已废弃] 单人旁路违反 IEC 61511 职责分离要求」，且这条路径
    **没有超时自动恢复**（双人审批的 `request_bypass` 会起 Timer）——
    任意 engineer 账号即可永久旁路超温/超压联锁，事后审计里只有一行
    `interlock_bypass_deprecated`，很难被认定成未授权操作。

    现在转调 `request_bypass()`：创建审批单 → 第二人批准 → 超时自动恢复。
    接口形状保持不变（仍回 `success`），但 `applied` 为 False 并带回
    `request_id` —— 前端据此显示「已提交审批」而不是「已旁路」。
    """
    data = request.get_json() or {}
    reason = data.get('reason', '维护')
    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return api_error('设备控制安全模块未启用', 503)

    # 参数校验：`request_bypass` 内部直接 `time.time() + timeout_minutes * 60`，
    # 传字符串会 TypeError、传负数会让审批单**一创建就过期**却回「已创建」。
    # round 306：用 `try_finite_float` 而不是裸 `float()` ——
    # 裸 float 的 `int(float("inf"))` 抛 OverflowError（漏成 500），
    # 而 `float("nan")` 根本不抛、让后面的范围比较对 NaN 失效
    # （`1 <= nan <= 480` 恒 False 看起来能挡，但它挡的是"范围"，不是"无效值"）。
    timeout_minutes = try_finite_float(data.get('timeout_minutes', 30))
    if timeout_minutes is None:
        return api_error('timeout_minutes 必须是有限数值', 400)
    if not (1 <= timeout_minutes <= 480):
        return api_error('timeout_minutes 必须在 1~480 分钟之间', 400)

    # `request_bypass` 不校验联锁是否存在 —— 给不存在的 ID 建审批单，
    # 批准后 `_activate_bypass` 会往旁路集合里塞一个孤儿 ID。
    if rule_id not in device_control.get_interlock_status():
        return api_error(f'联锁 {rule_id} 不存在', 404)

    try:
        request_id = device_control.request_bypass(
            rule_id, operator, reason, int(timeout_minutes))
    except Exception as e:      # noqa: BLE001 —— 审批单创建失败要如实回给操作员
        logger.error("创建旁路审批单失败: %s", e, exc_info=True)
        return api_error(f'旁路申请创建失败: {e}', 500)

    if not request_id:
        return api_error('旁路申请创建失败', 500)

    logger.warning("联锁旁路申请已创建（待第二人批准）: %s by %s, %s分钟",
                   rule_id, operator, timeout_minutes)
    return jsonify({
        'success': True,
        'applied': False,
        'request_id': request_id,
        'message': f'联锁 {rule_id} 旁路申请已提交（单号 {request_id}），'
                   f'需第二人批准后生效，最长 {timeout_minutes:.0f} 分钟自动恢复',
    })


@control_bp.route('/control/interlocks/<rule_id>/restore', methods=['POST'])
@_require_engineer
def restore_interlock(rule_id):
    """恢复联锁"""
    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return api_error('设备控制安全模块未启用', 503)
    success = device_control.restore_interlock(rule_id, operator)
    return jsonify({'success': success, 'message': f'联锁 {rule_id} 已恢复' if success else '恢复失败'})


@control_bp.route('/control/interlocks/bypass-request', methods=['POST'])
@_require_engineer
def create_bypass_request():
    """创建联锁旁路请求（需多人审批）"""
    data = request.get_json() or {}
    interlock_id = data.get('interlock_id')
    reason = data.get('reason', '')

    if not interlock_id:
        return api_error('缺少 interlock_id 参数')

    # BUG-053：老接口 `bypass_interlock` 有三道校验，这个新接口一道都没有 ——
    # 负数/NaN 超时会让旁路窗口瞬间过期或永不过期；
    # 不存在的 interlock_id 会在审批通过后才暴露（更糟：审批白做）。
    timeout_minutes = try_finite_float(data.get('timeout_minutes', 30))
    if timeout_minutes is None:
        return api_error('timeout_minutes 必须是有限数值', 400)
    if not (1 <= timeout_minutes <= 480):
        return api_error('timeout_minutes 必须在 1~480 分钟之间', 400)

    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return api_error('设备控制安全模块未启用', 503)

    if interlock_id not in device_control.get_interlock_status():
        return api_error(f'联锁 {interlock_id} 不存在', 404)

    request_id = device_control.request_bypass(
        interlock_id, operator, reason, int(timeout_minutes))
    return jsonify({
        'success': True,
        'request_id': request_id,
        'message': f'旁路请求已创建，等待2人审批，{timeout_minutes}分钟内有效'
    })


@control_bp.route('/control/interlocks/bypass-approve', methods=['POST'])
@_require_engineer
def approve_bypass_request():
    """审批联锁旁路请求"""
    data = request.get_json() or {}
    request_id = data.get('request_id')

    if not request_id:
        return api_error('缺少 request_id 参数')

    approver = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return api_error('设备控制安全模块未启用', 503)

    success, message = device_control.approve_bypass(request_id, approver)
    return jsonify({'success': success, 'message': message}), (200 if success else 400)


@control_bp.route('/control/interlocks/bypass-reject', methods=['POST'])
@_require_engineer
def reject_bypass_request():
    """拒绝联锁旁路请求"""
    data = request.get_json() or {}
    request_id = data.get('request_id')
    reason = data.get('reason', '')

    if not request_id:
        return api_error('缺少 request_id 参数')

    rejector = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return api_error('设备控制安全模块未启用', 503)

    success, message = device_control.reject_bypass(request_id, rejector, reason)
    return jsonify({'success': success, 'message': message}), (200 if success else 400)


@control_bp.route('/control/interlocks/bypass-pending', methods=['GET'])
@_require_auth
def get_pending_bypasses():
    """获取待审批的旁路请求"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return api_error('设备控制安全模块未启用', 503)

    pending = device_control.get_pending_bypasses()
    return jsonify({'pending': pending, 'count': len(pending)})


@control_bp.route('/control/health', methods=['GET'])
@_require_auth
def get_device_health():
    """获取所有设备健康状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify(device_control.get_device_health_summary())


@control_bp.route('/control/batch', methods=['POST'])
@_require_engineer
def batch_control():
    """批量控制设备（启动/停止/复位），支持按设备ID列表过滤"""
    data = request.get_json() or {}
    action = data.get('action')
    device_ids = data.get('device_ids')  # 可选：指定设备ID列表

    if action not in ('start', 'stop', 'reset'):
        return api_error('无效的操作类型，支持: start, stop, reset')

    operator = request.current_user['username']

    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        result = device_control.batch_control(action, operator, device_ids=device_ids)
        target_desc = f'{len(device_ids)}台设备' if device_ids else '所有设备'
        get_auth_manager().log_operation(
            operator, f'batch_{action}', f'批量{action}{target_desc}')
        return jsonify(result)

    # round 338：**无安全模块 → 拒绝批量控制**，与 write-register / write-coil 对齐。
    #
    # 原来这里降级成「硬编码地址直写」：线圈 0 / 寄存器 100，
    # 既没有急停/联锁/量程/审计，地址还是写死的 —— 而 device_control.batch_control
    # 那套是按**配置点位**走的。同一个功能两套实现、挑闸门少的那条用，
    # 等于绕过安全层。
    logger.error(
        "拒绝批量控制：安全模块(device_control)不可用。action=%s operator=%s",
        action, operator)
    return jsonify({
        'success': False,
        'message': '安全模块不可用，已拒绝批量控制（无法执行急停/联锁校验）',
    }), 503


@control_bp.route('/control/audit', methods=['GET'])
@_require_auth
def get_audit_log():
    """获取操作审计日志"""
    limit = clamp_limit(request.args.get('limit', 100, type=int), default=100, maximum=500)
    action_filter = request.args.get('action')
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify({'logs': device_control.get_audit_log(limit, action_filter)})


@control_bp.route('/control/status', methods=['GET'])
@_require_auth
def get_control_safety_status():
    """获取设备控制安全系统完整状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify({'enabled': True, **device_control.get_full_status()})


# ==================== 配方/批量过程控制API ====================

@control_bp.route('/control/recipe/start', methods=['POST'])
@_require_engineer
def start_recipe():
    """启动配方"""
    data = request.get_json() or {}
    recipe_name = data.get('recipe_name')
    device_id = data.get('device_id')

    if not recipe_name:
        return api_error('缺少 recipe_name 参数')

    from 采集层.recipe_simulator import RecipeSimulator
    if recipe_name not in RecipeSimulator.RECIPES:
        return jsonify({
            'error': f'配方 "{recipe_name}" 不存在',
            'available': list(RecipeSimulator.RECIPES.keys())
        }), 404

    # 查找目标设备的行为模拟器
    behavior_sim = _get_behavior_simulator(device_id)
    if not behavior_sim:
        return api_error('未找到可用的设备行为模拟器', 404)

    success = behavior_sim.set_recipe(recipe_name)
    if success:
        operator = request.current_user['username']
        get_auth_manager().log_operation(
            operator, 'start_recipe',
            f'设备 {behavior_sim.device_name} 启动配方: {recipe_name}')
        return jsonify({
            'success': True,
            'message': f'配方 "{recipe_name}" 已启动',
            'device': behavior_sim.device_name,
            'status': behavior_sim.get_recipe_status()
        })
    return jsonify({'success': False, 'message': '配方启动失败'}), 400


@control_bp.route('/control/recipe/stop', methods=['POST'])
@_require_engineer
def stop_recipe():
    """停止配方"""
    data = request.get_json() or {}
    device_id = data.get('device_id')

    behavior_sim = _get_behavior_simulator(device_id)
    if not behavior_sim:
        return api_error('未找到可用的设备行为模拟器', 404)

    status = behavior_sim.get_recipe_status()
    if not status:
        return api_error('该设备没有正在运行的配方')

    behavior_sim.stop_recipe()
    operator = request.current_user['username']
    get_auth_manager().log_operation(
        operator, 'stop_recipe',
        f'设备 {behavior_sim.device_name} 停止配方: {status.get("recipe", "")}')
    return jsonify({
        'success': True,
        'message': '配方已停止',
        'device': behavior_sim.device_name
    })


@control_bp.route('/control/recipe/status', methods=['GET'])
@_require_auth
def get_recipe_status():
    """获取配方状态"""
    device_id = request.args.get('device_id')

    behavior_sim = _get_behavior_simulator(device_id)
    if not behavior_sim:
        return api_error('未找到可用的设备行为模拟器', 404)

    status = behavior_sim.get_recipe_status()
    if not status:
        return jsonify({'running': False, 'message': '没有正在运行的配方'})
    return jsonify(status)


@control_bp.route('/control/recipe/list', methods=['GET'])
@_require_auth
def list_recipes():
    """获取可用配方列表"""
    from 采集层.recipe_simulator import RecipeSimulator
    recipes = {}
    for key, recipe in RecipeSimulator.RECIPES.items():
        recipes[key] = {
            'name': recipe.name,
            'version': recipe.version,
            'phase_count': len(recipe.phases),
            'phases': [p.name for p in recipe.phases],
            'total_duration': sum(p.duration for p in recipe.phases),
            'parameters': recipe.parameters,
        }
    return jsonify({'recipes': recipes, 'count': len(recipes)})


def _get_behavior_simulator(device_id: Optional[str]):
    """获取设备行为模拟器实例"""
    device_manager = getattr(current_app, 'device_manager', None)
    if not device_manager:
        return None

    # 模拟模式下查找行为模拟器
    simulators = getattr(current_app, '_behavior_simulators', {})
    if device_id and device_id in simulators:
        return simulators[device_id]

    # 尝试从设备管理器获取
    if device_id:
        client = device_manager.get_client(device_id)
        if client and hasattr(client, 'behavior_simulator'):
            return client.behavior_simulator

    # 仅在**未指定设备**（单机演示）时才回退。显式传了 device_id 却查不到，
    # 必须返回 None → 上层 404，不能把配方下发到任意设备上。
    if not device_id and simulators:
        return next(iter(simulators.values()))

    return None
