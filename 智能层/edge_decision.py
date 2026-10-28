"""
边缘决策引擎 (Edge Decision Engine)
=====================================
工业4.0去中心化决策核心：本地自主决策，不依赖云端

功能：
1. 规则引擎 — IF-THEN规则自动执行
2. 联锁控制 — 安全联锁逻辑（急停、超限自动停机）
3. 自适应调节 — PID控制回路
4. 决策日志 — 所有自动决策可追溯
"""

import logging
import time
import threading
from datetime import datetime
from typing import Any, Callable
from collections import deque

logger = logging.getLogger(__name__)


class EdgeDecisionEngine:
    """
    边缘决策引擎

    工业4.0要求：关键决策在边缘（本地）完成，不依赖云端

    决策层级：
    1. 安全联锁（最高优先级，毫秒级响应）
    2. 规则引擎（秒级响应）
    3. 自适应调节（分钟级响应）
    """

    def __init__(self, database, config: dict[str, Any] | None = None):
        """
        Args:
            database: Database实例
            config: 配置字典
        """
        self.database = database
        self.config = config or {}

        # 规则库
        self.rules: dict[str, dict[str, Any]] = {}

        # 联锁规则库
        self.interlocks: dict[str, dict[str, Any]] = {}

        # PID控制器库
        self.pid_controllers: dict[str, dict[str, Any]] = {}

        # 决策日志
        self.decision_log: deque[Any] = deque(maxlen=1000)

        # 执行回调（用于实际控制设备）
        self._action_callbacks: dict[str, Callable[..., Any]] = {}

        # round 300：动作执行失败计数（未注册回调 / 回调抛异常）。
        # 此前这类失败只体现在 log_entry['result'] 的一段文字里，
        # 没有任何可聚合的数 —— 运维看不到「这个部署根本没接广播回调」。
        self._action_failures = 0

        # round 300：PID 单拍 dt 上限。单调时钟解决不了「进程被挂起 / 系统休眠」——
        # 那时 dt 是几十分钟，积分项一步就打满限幅。超过这个值视为这一拍不可信，
        # 重新起算而不是硬算（正常采样周期是秒级；采样周期确实更长的部署可调）。
        self._max_pid_dt = float(self.config.get('max_pid_dt_seconds', 60.0))

        # 当前数据快照
        self._data_snapshot: dict[str, float] = {}

        # 锁
        self._lock = threading.Lock()

        # 运行状态
        self._running = False
        self._thread = None

        logger.info("边缘决策引擎初始化完成")

    def start(self):
        """启动决策引擎"""
        if self._running:
            return

        # 加载默认规则（如果规则库为空）
        if not self.rules and not self.interlocks:
            self._load_default_rules()

        self._running = True
        self._thread = threading.Thread(target=self._decision_loop, daemon=True)
        self._thread.start()
        logger.info("边缘决策引擎已启动")

    def stop(self):
        """停止决策引擎"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _load_default_rules(self):
        """加载默认决策规则和安全联锁"""
        # 安全联锁规则
        self.add_interlock(
            'high_temp_interlock',
            condition={
                'type': 'threshold',
                'key': 'siemens_plc_01:temperature',
                'operator': 'gt',
                'value': 150.0
            },
            action={'type': 'set_alarm', 'message': '反应釜温度过高，紧急停机！', 'level': 'critical'},
            name='高温安全联锁',
            enabled=True
        )

        self.add_interlock(
            'high_pressure_interlock',
            condition={
                'type': 'threshold',
                'key': 'siemens_plc_01:pressure',
                'operator': 'gt',
                'value': 1.0
            },
            action={'type': 'set_alarm', 'message': '管道压力超限，紧急泄压！', 'level': 'critical'},
            name='高压安全联锁',
            enabled=True
        )

        # 决策规则
        self.add_rule(
            'temp_control_rule',
            condition={
                'type': 'threshold',
                'key': 'siemens_plc_01:temperature',
                'operator': 'gt',
                'value': 80.0
            },
            action={'type': 'set_alarm', 'message': '反应釜温度偏高，建议检查冷却系统', 'level': 'warning'},
            name='温度预警规则',
            priority=10,
            enabled=True
        )

        self.add_rule(
            'pressure_control_rule',
            condition={
                'type': 'threshold',
                'key': 'siemens_plc_01:pressure',
                'operator': 'gt',
                'value': 0.8
            },
            action={'type': 'set_alarm', 'message': '管道压力偏高，建议检查阀门', 'level': 'warning'},
            name='压力预警规则',
            priority=20,
            enabled=True
        )

        self.add_rule(
            'motor_current_rule',
            condition={
                'type': 'threshold',
                'key': 'siemens_plc_01:motor_current',
                'operator': 'gt',
                'value': 10.0
            },
            action={'type': 'set_alarm', 'message': '电机电流过高，可能过载', 'level': 'warning'},
            name='电机过载预警',
            priority=15,
            enabled=True
        )

        # PID控制器
        self.add_pid_controller(
            'temp_pid',
            input_key='siemens_plc_01:temperature',
            output_key='siemens_plc_01:motor_speed',
            setpoint=75.0,
            kp=2.0, ki=0.5, kd=0.1,
            output_min=0, output_max=1500
        )

        logger.info(f"已加载默认规则: {len(self.rules)}条规则, {len(self.interlocks)}条联锁, {len(self.pid_controllers)}个PID控制器")

    def _decision_loop(self):
        """决策主循环"""
        while self._running:
            try:
                self._execute_cycle()
            except Exception as e:
                logger.error(f"决策引擎异常: {e}", exc_info=True)
            time.sleep(1)  # 1秒决策周期

    def _execute_cycle(self):
        """
        执行一个决策周期

        执行顺序（priority 升序，数字越小优先级越高）：
        1. 安全联锁（默认 priority=0，最高，最先执行）
        2. 决策规则（按 priority 排序 —— 高危规则不得因注册顺序靠后而被低危规则抢先）
        3. PID调节

        规则/联锁列表在锁内取快照后再执行，避免执行期间字典被并发修改，
        同时避免 `_execute_action` 内部再加锁造成死锁。
        """
        with self._lock:
            snapshot = dict(self._data_snapshot)
            ordered_interlocks = sorted(
                self.interlocks.items(), key=lambda kv: kv[1].get('priority', 0)
            )
            ordered_rules = sorted(
                self.rules.items(), key=lambda kv: kv[1].get('priority', 10)
            )

        # 1. 安全联锁（最高优先级）
        for rule_id, rule in ordered_interlocks:
            if not rule.get('enabled', True):
                continue
            try:
                if self._evaluate_condition(rule['condition'], snapshot):
                    self._execute_action(rule['action'], rule_id, 'interlock', snapshot)
            except Exception as e:
                logger.error(f"联锁规则 {rule_id} 执行异常: {e}")

        # 2. 规则引擎（按优先级从高到低）
        for rule_id, rule in ordered_rules:
            if not rule.get('enabled', True):
                continue
            try:
                if self._evaluate_condition(rule['condition'], snapshot):
                    self._execute_action(rule['action'], rule_id, 'rule', snapshot)
            except Exception as e:
                logger.error(f"决策规则 {rule_id} 执行异常: {e}")

        # 3. PID控制
        for ctrl_id, ctrl in self.pid_controllers.items():
            if not ctrl.get('enabled', True):
                continue
            try:
                self._execute_pid(ctrl_id, ctrl, snapshot)
            except Exception as e:
                logger.error(f"PID控制器 {ctrl_id} 异常: {e}")

    # ==================== 数据输入 ====================

    def update_data(self, key: str, value: float):
        """
        更新数据快照

        Args:
            key: 数据键，格式 "device_id:register_name"
            value: 当前值
        """
        with self._lock:
            self._data_snapshot[key] = value

    def register_action(self, action_name: str, callback: Callable[..., Any]):
        """
        注册动作回调

        Args:
            action_name: 动作名称
            callback: 回调函数 callback(device_id, register_name, value)
        """
        self._action_callbacks[action_name] = callback
        logger.info(f"注册决策动作: {action_name}")

    # ==================== 规则管理 ====================

    def add_rule(self, rule_id: str, condition: dict[str, Any], action: dict[str, Any],
                  name: str = '', priority: int = 10, enabled: bool = True):
        """
        添加决策规则

        Args:
            rule_id: 规则ID
            condition: 条件表达式
                {
                    'type': 'threshold'|'range'|'expression',
                    'key': 'device_id:register_name',
                    'operator': 'gt'|'lt'|'eq'|'gte'|'lte'|'between',
                    'value': float,
                    'value2': float,  # 用于between
                }
            action: 动作
                {
                    'type': 'write_register'|'set_alarm'|'callback',
                    'target': 'device_id:register_name',
                    'value': float,
                    'callback_name': str,
                }
            name: 规则名称
            priority: 优先级（数字越小优先级越高）
            enabled: 是否启用
        """
        self.rules[rule_id] = {
            'name': name or rule_id,
            'condition': condition,
            'action': action,
            'priority': priority,
            'enabled': enabled,
            'created_at': datetime.now().isoformat(),
            'last_triggered': None,
            'trigger_count': 0,
        }
        logger.info(f"添加决策规则: {rule_id} - {name}")

    def add_interlock(self, interlock_id: str, condition: dict[str, Any], action: dict[str, Any],
                       name: str = '', enabled: bool = True, priority: int = 0):
        """
        添加安全联锁规则

        联锁规则优先级最高，用于安全关键场景。
        同类联锁之间也支持 priority 排序（数字越小越先执行），用于把
        "急停/烟感" 这类 0 级安全动作排在 "门禁" 这类降级动作之前。
        """
        self.interlocks[interlock_id] = {
            'name': name or interlock_id,
            'condition': condition,
            'action': action,
            'priority': priority,
            'enabled': enabled,
            'created_at': datetime.now().isoformat(),
            'last_triggered': None,
            'trigger_count': 0,
        }
        logger.info(f"添加安全联锁: {interlock_id} - {name} (priority={priority})")

    def add_pid_controller(self, ctrl_id: str, input_key: str, output_key: str,
                            setpoint: float, kp: float = 1.0, ki: float = 0.1,
                            kd: float = 0.05, output_min: float = 0,
                            output_max: float = 100, name: str = '',
                            enabled: bool = True):
        """
        添加PID控制器

        Args:
            ctrl_id: 控制器ID
            input_key: 输入数据键
            output_key: 输出数据键
            setpoint: 设定值
            kp: 比例系数
            ki: 积分系数
            kd: 微分系数
            output_min: 输出下限
            output_max: 输出上限
        """
        self.pid_controllers[ctrl_id] = {
            'name': name or ctrl_id,
            'input_key': input_key,
            'output_key': output_key,
            'setpoint': setpoint,
            'kp': kp,
            'ki': ki,
            'kd': kd,
            'output_min': output_min,
            'output_max': output_max,
            'enabled': enabled,
            'integral': 0,
            'prev_error': 0,
            # round 300：PID 的 dt 必须用**单调时钟**。
            # 原来存 time.time()（墙钟），NTP 校时往前跳 1 小时 → dt=3600
            # → 积分项一秒钟打满限幅、输出直接顶到 output_max；
            # 往后跳则 dt<=0 被 return 吞掉，控制静默丢拍。
            # 这个值只活在本进程内存里，不落盘、不做跨机比较，换单调时钟无副作用。
            'prev_time': time.monotonic(),
            'output': 0,
        }
        logger.info(f"添加PID控制器: {ctrl_id} - {name} (SP={setpoint})")

    def remove_rule(self, rule_id: str):
        """删除规则"""
        self.rules.pop(rule_id, None)

    def remove_interlock(self, interlock_id: str):
        """删除联锁"""
        self.interlocks.pop(interlock_id, None)

    # ==================== 条件评估 ====================

    def _evaluate_condition(self, condition: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        """评估条件表达式"""
        cond_type = condition.get('type', 'threshold')

        if cond_type == 'threshold':
            key = condition['key']
            value = snapshot.get(key)
            if value is None:
                return False

            op = condition.get('operator', 'gt')
            threshold = condition.get('value', 0)

            if op == 'gt':
                return value > threshold
            elif op == 'lt':
                return value < threshold
            elif op == 'eq':
                return abs(value - threshold) < 0.001
            elif op == 'gte':
                return value >= threshold
            elif op == 'lte':
                return value <= threshold
            elif op == 'between':
                value2 = condition.get('value2', threshold)
                return threshold <= value <= value2

        elif cond_type == 'and':
            conditions = condition.get('conditions', [])
            return all(self._evaluate_condition(c, snapshot) for c in conditions)

        elif cond_type == 'or':
            conditions = condition.get('conditions', [])
            return any(self._evaluate_condition(c, snapshot) for c in conditions)

        return False

    # ==================== 动作执行 ====================

    def _execute_action(self, action: dict[str, Any], rule_id: str,
                         rule_type: str, snapshot: dict[str, Any]):
        """执行动作"""
        action_type = action.get('type', 'callback')

        # 记录决策日志
        log_entry = {
            'rule_id': rule_id,
            'rule_type': rule_type,
            'action_type': action_type,
            'timestamp': datetime.now().isoformat(),
            'snapshot_keys': list(snapshot.keys())[:5],
            'ok': True,
        }

        # round 300：**回调没注册也算失败**。
        # 原来三个分支各自 `if callback:` —— write_register 在回调缺失时
        # 连 `result` 都不写（日志里一条没有结果的记录），而 set_alarm /
        # callback 更糟：`result` 是**无条件**写的，日志照样记
        # 「报警: xxx」「回调: xxx」，实际一个函数都没调用；trigger_count 也照涨。
        # 判据：日志里写的结果，必须等于真的发生了什么。
        #
        # 注意：`callback` 类型实际查的是 `callback_name`，不是 action_type 本身 ——
        # 统一按「真正要调用的键」判定，否则这个分支会被误判成已注册。
        callback_key = (action.get('callback_name', '')
                        if action_type == 'callback' else action_type)

        if not self._action_callbacks.get(callback_key):
            log_entry['ok'] = False
            log_entry['result'] = f'失败: 未注册 {callback_key} 回调'
            with self._lock:
                self._action_failures += 1
            logger.error("动作未执行：未注册 %s 回调（规则 %s）", callback_key, rule_id)

        elif action_type == 'write_register':
            target = action.get('target', '')
            value = action.get('value', 0)
            # 入口已保证回调存在；这里仍然整段 try —— 回调本身可能抛，
            # 而且 `target` 格式非法（没有冒号）也算这次动作失败。
            try:
                device_id, register_name = target.split(':', 1)
                self._action_callbacks['write_register'](device_id, register_name, value)
                log_entry['result'] = f'写入 {target} = {value}'
            except Exception as e:      # noqa: BLE001
                log_entry['result'] = f'写入失败: {e}'
                log_entry['ok'] = False
                with self._lock:
                    self._action_failures += 1
                logger.error(f"动作执行失败: {e}")

        elif action_type == 'set_alarm':
            alarm_msg = action.get('message', f'规则 {rule_id} 触发')
            callback = self._action_callbacks['set_alarm']
            # round 300：回调抛异常时不许再写「报警: xxx」——
            # 现场音柱没响、日志说报了警，正是 287 那一族的老毛病。
            try:
                callback(alarm_msg, action.get('level', 'warning'))
                log_entry['result'] = f'报警: {alarm_msg}'
            except Exception as e:      # noqa: BLE001 —— 动作失败不许拖垮决策循环
                log_entry['ok'] = False
                log_entry['result'] = f'报警失败: {e}'
                with self._lock:
                    self._action_failures += 1
                logger.error("set_alarm 回调异常: %s", e)

        elif action_type == 'callback':
            callback_name = action.get('callback_name', '')
            callback = self._action_callbacks[callback_name]
            try:
                callback(snapshot)
                log_entry['result'] = f'回调: {callback_name}'
            except Exception as e:      # noqa: BLE001
                log_entry['ok'] = False
                log_entry['result'] = f'回调失败: {callback_name}: {e}'
                with self._lock:
                    self._action_failures += 1
                logger.error("回调 %s 异常: %s", callback_name, e)

        # 更新规则统计
        with self._lock:
            if rule_id in self.rules:
                self.rules[rule_id]['last_triggered'] = datetime.now().isoformat()
                self.rules[rule_id]['trigger_count'] += 1
            elif rule_id in self.interlocks:
                self.interlocks[rule_id]['last_triggered'] = datetime.now().isoformat()
                self.interlocks[rule_id]['trigger_count'] += 1

        self.decision_log.append(log_entry)

        # round 300：动作没生效时不能再用 info 写一行「成功」样子的日志
        if log_entry.get('ok', True):
            logger.info(f"[{rule_type.upper()}] {rule_id}: {log_entry.get('result', '')}")
        else:
            logger.warning(f"[{rule_type.upper()}] {rule_id}: 动作未生效 — "
                           f"{log_entry.get('result', '')}")

    def _execute_pid(self, ctrl_id: str, ctrl: dict[str, Any], snapshot: dict[str, Any]):
        """执行PID控制计算"""
        input_key = ctrl['input_key']
        process_value = snapshot.get(input_key)

        if process_value is None:
            return

        setpoint = ctrl['setpoint']
        error = setpoint - process_value

        # round 300：与 prev_time 同源，必须都用单调时钟（理由见 add_pid_controller）
        now = time.monotonic()
        dt = now - ctrl['prev_time']

        # 时钟跳变 / 进程被挂起时 dt 可能是几十分钟：直接算会把积分项一步打满
        # 限幅、输出顶到 output_max，相当于一次没有依据的满量程动作。
        # 这一拍不可信 —— 重新起算（不积分、不微分），等下一拍正常算。
        if dt <= 0 or dt > self._max_pid_dt:
            ctrl['prev_time'] = now
            ctrl['prev_error'] = error
            return

        # PID计算
        # 比例项
        p_term = ctrl['kp'] * error

        # 积分项（带抗饱和）
        ctrl['integral'] += error * dt
        # 积分限幅
        max_integral = (ctrl['output_max'] - ctrl['output_min']) / ctrl['ki'] if ctrl['ki'] > 0 else 1e6
        ctrl['integral'] = max(-max_integral, min(max_integral, ctrl['integral']))
        i_term = ctrl['ki'] * ctrl['integral']

        # 微分项
        d_term = ctrl['kd'] * (error - ctrl['prev_error']) / dt if dt > 0 else 0

        # 输出
        output = p_term + i_term + d_term
        output = max(ctrl['output_min'], min(ctrl['output_max'], output))

        ctrl['output'] = output
        ctrl['prev_error'] = error
        ctrl['prev_time'] = now

        # 写入输出
        output_key = ctrl['output_key']
        callback = self._action_callbacks.get('write_register')
        # round 300：PID 算出了输出却没人写 —— 旧代码是 `if callback and ':' in
        # output_key:` 直接静默跳过，而 `get_rules()` 里 output 照样有值、
        # 看板上像是"正在控制"。控制量根本没下发，比不控制更危险。
        if not callback:
            with self._lock:
                self._action_failures += 1
            logger.error("PID 输出未写入：未注册 write_register 回调（%s）", ctrl_id)
            return
        if ':' not in output_key:
            with self._lock:
                self._action_failures += 1
            logger.error("PID 输出未写入：output_key 格式非法 %r（%s）", output_key, ctrl_id)
            return
        device_id, register_name = output_key.split(':', 1)
        try:
            callback(device_id, register_name, output)
        except Exception as e:
            with self._lock:
                self._action_failures += 1
            logger.error(f"PID输出写入失败: {e}")

    # ==================== 查询接口 ====================

    def get_rules(self) -> dict[str, dict[str, Any]]:
        """获取所有规则"""
        with self._lock:
            return {
                'rules': dict(self.rules),
                'interlocks': dict(self.interlocks),
                'pid_controllers': {
                    k: {kk: vv for kk, vv in v.items() if kk != 'integral'}
                    for k, v in self.pid_controllers.items()
                },
            }

    def get_decision_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取决策日志"""
        return list(self.decision_log)[-limit:]

    def get_data_snapshot(self) -> dict[str, float]:
        """获取当前数据快照"""
        with self._lock:
            return dict(self._data_snapshot)

    def get_status(self) -> dict[str, Any]:
        """获取引擎状态"""
        return {
            'running': self._running,
            'rules_count': len(self.rules),
            'interlocks_count': len(self.interlocks),
            'pid_controllers_count': len(self.pid_controllers),
            'data_points': len(self._data_snapshot),
            'decision_log_size': len(self.decision_log),
            'registered_actions': list(self._action_callbacks.keys()),
            # round 300：动作失败次数（未注册回调 / 回调抛异常）。
            # 与 registered_actions 对照即可发现「规则配了但没人执行」。
            'action_failures': self._action_failures,
        }
