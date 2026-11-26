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
import math
import time
import threading
from datetime import datetime
from typing import Any, Callable
from collections import deque

from core.action_result import is_failure
from core.safe_numeric import try_finite_float

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
        _pid_dt = try_finite_float(self.config.get('max_pid_dt_seconds', 60.0))
        self._max_pid_dt = (
            60.0 if _pid_dt is None or _pid_dt <= 0 else float(_pid_dt)
        )
        _data_age = try_finite_float(self.config.get('max_data_age_seconds', 10.0))
        self._max_data_age = (
            10.0 if _data_age is None or _data_age < 0 else float(_data_age)
        )

        # 当前数据快照
        self._data_snapshot: dict[str, float] = {}
        self._data_quality: dict[str, tuple[str, float]] = {}

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

    def _load_default_rules(self) -> bool:
        """加载默认决策规则 / 安全联锁 / PID（round 309：不再硬编码设备名）

        ⚠️ 历史实现把 3 条规则 + 2 条联锁 + 1 个 PID **写死在代码里**，全部绑定
        `siemens_plc_01` —— 而设备清单里**没有这个设备**（真实的是
        `siemens_1500_01` / `siemens_web_01` 等）。`update_data()` 以
        `device_id:register_name` 为 key，所以这 6 条**永远匹配不上**：
        引擎启动日志写着「已加载默认规则: 3条规则, 2条联锁, 1个PID控制器」，
        界面上有规则在跑，实际一条都不会触发 ——
        与 round 246 在 `device_control` 里删掉的那批"预置联锁"是同一个病
        （**看似在保护、实际不保护**）。

        返回值此前也没有：`start()` 里调用它，无法知道到底加载到了没有。

        Returns:
            bool: 是否加载到了默认规则（False = 没有配置，引擎空转）。

        来源（按优先级）：
          1. `config['default_rules']` —— 显式注入，形如
             `{'interlocks': [...], 'rules': [...], 'pid_controllers': [...]}`，
             每项字段与 `add_interlock()` / `add_rule()` / `add_pid_controller()`
             的参数同名（`id` 对应各方法的第一个位置参数）；
          2. 没有就**什么都不加载**并 WARNING —— 与 `device_control` 的口径一致：
             「没有配置就没有规则」，绝不用不存在的设备名凑数。
        """
        spec = self.config.get('default_rules')
        if not isinstance(spec, dict):
            spec = {}

        interlocks = spec.get('interlocks') or []
        rules = spec.get('rules') or []
        pids = spec.get('pid_controllers') or []

        if not (interlocks or rules or pids):
            logger.warning(
                "边缘决策引擎**没有加载任何默认规则**：请在构造配置里提供 "
                "default_rules（interlocks / rules / pid_controllers），"
                "或运行期用 add_rule()/add_interlock()/add_pid_controller() 注入。"
                "此前写死的 siemens_plc_01 那批规则已移除 —— 设备清单里没有该设备，"
                "条件永不匹配，属于「看似在保护、实际不保护」的死规则。")
            return False

        loaded = {'interlocks': 0, 'rules': 0, 'pid_controllers': 0}
        skipped = []

        for item in interlocks:
            rid = item.get('id')
            if not rid or 'condition' not in item or 'action' not in item:
                skipped.append(f'interlock:{rid or "<无 id>"}')
                continue
            self.add_interlock(
                rid, condition=item['condition'], action=item['action'],
                name=item.get('name', ''), enabled=item.get('enabled', True),
                priority=item.get('priority', 0))
            loaded['interlocks'] += 1

        for item in rules:
            rid = item.get('id')
            if not rid or 'condition' not in item or 'action' not in item:
                skipped.append(f'rule:{rid or "<无 id>"}')
                continue
            self.add_rule(
                rid, condition=item['condition'], action=item['action'],
                name=item.get('name', ''), priority=item.get('priority', 10),
                enabled=item.get('enabled', True))
            loaded['rules'] += 1

        for item in pids:
            rid = item.get('id')
            if not rid or 'input_key' not in item or 'output_key' not in item:
                skipped.append(f'pid:{rid or "<无 id>"}')
                continue
            self.add_pid_controller(
                rid, input_key=item['input_key'], output_key=item['output_key'],
                setpoint=item.get('setpoint', 0.0), kp=item.get('kp', 1.0),
                ki=item.get('ki', 0.1), kd=item.get('kd', 0.05),
                output_min=item.get('output_min', 0),
                output_max=item.get('output_max', 100),
                name=item.get('name', ''), enabled=item.get('enabled', True))
            loaded['pid_controllers'] += 1

        # round 309：**部分丢弃必须汇总**（与联锁加载同口径）——
        # 少注册一条规则不会有任何报错，只表现为"某条保护没生效"。
        if skipped:
            logger.warning(
                "边缘决策默认规则**部分丢弃**：%d 条缺 id/condition/action/input_key，"
                "未注册（%s）—— 它们不会执行任何动作",
                len(skipped), ', '.join(skipped))

        logger.info("已加载默认规则: %d条规则, %d条联锁, %d个PID控制器",
                    loaded['rules'], loaded['interlocks'], loaded['pid_controllers'])
        return True

    def _decision_loop(self):
        """决策主循环"""
        while self._running:
            try:
                self._execute_cycle()
            except Exception as e:
                logger.error(f"决策引擎异常: {e}", exc_info=True)
            time.sleep(1)  # 1秒决策周期

    @staticmethod
    def _finite_non_bool(value: Any) -> float | None:
        """解析有限浮点数，并显式拒绝 bool（bool 是 int 子类）。"""
        if isinstance(value, bool):
            return None
        return try_finite_float(value)

    @staticmethod
    def _is_good_quality(quality: Any) -> bool:
        """校验数据质量码：接受数值 192 (OPC UA GOOD) 及历史单测默认字符串 'GOOD'。"""
        if quality is None or isinstance(quality, bool):
            return False
        if isinstance(quality, (int, float)):
            num = try_finite_float(quality)
            return num is not None and num == 192.0
        if isinstance(quality, str):
            text = quality.strip().upper()
            return text in ('GOOD', '192')
        return False

    @staticmethod
    def _normalize_ts(ts: Any) -> float | None:
        """将输入时间戳归一化到 monotonic 秒；非有限或布尔值返回 None。"""
        if ts is None:
            return time.monotonic()
        if isinstance(ts, bool):
            return None
        if isinstance(ts, datetime):
            try:
                return time.monotonic() - (time.time() - ts.timestamp())
            except (OverflowError, OSError, ValueError):
                return None
        ts_num = try_finite_float(ts)
        if ts_num is None:
            return None
        if ts_num > 1e9:
            return time.monotonic() - (time.time() - ts_num)
        return ts_num

    def _fresh_snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        fresh: dict[str, float] = {}
        dropped: list[str] = []
        with self._lock:
            for k, v in self._data_snapshot.items():
                num_v = None if isinstance(v, bool) else try_finite_float(v)
                if num_v is None:
                    dropped.append(f"{k}(non_finite={v!r})")
                    continue
                meta = self._data_quality.get(k)
                if meta is None:
                    fresh[k] = num_v
                    continue
                quality, ts = meta
                ts_num = None if isinstance(ts, bool) else try_finite_float(ts)
                if ts_num is None:
                    dropped.append(f"{k}(quality={quality},invalid_ts={ts!r})")
                    continue
                age = now - ts_num
                if self._is_good_quality(quality) and -1.0 <= age <= self._max_data_age:
                    fresh[k] = num_v
                else:
                    dropped.append(f"{k}(quality={quality},age={age:.1f}s)")
        if dropped:
            logger.error("数据快照剔除坏质量/过期点位（断线或采集异常）: %s", ", ".join(dropped))
        return fresh

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
        snapshot = self._fresh_snapshot()
        with self._lock:
            ordered_interlocks = sorted(
                self.interlocks.items(), key=lambda kv: kv[1].get('priority', 0)
            )
            ordered_rules = sorted(
                self.rules.items(), key=lambda kv: kv[1].get('priority', 10)
            )
            # BUG-050：PID 也要在锁内取快照。原来这一句是锁外裸遍历
            # `self.pid_controllers.items()`，而 add/remove_pid 不持锁 →
            # 并发时抛 `RuntimeError: dictionary changed size during iteration`，
            # 该异常从锁内抛出后被 `_decision_loop` 整轮捕获 ——
            # 连着已经算好的联锁一起跳过（含 0 级急停），不只是丢 PID。
            ordered_pids = list(self.pid_controllers.items())

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

        # 3. PID控制（遍历锁内快照，见上）
        for ctrl_id, ctrl in ordered_pids:
            if not ctrl.get('enabled', True):
                continue
            try:
                self._execute_pid(ctrl_id, ctrl, snapshot)
            except Exception as e:
                logger.error(f"PID控制器 {ctrl_id} 异常: {e}")

    # ==================== 数据输入 ====================

    def update_data(self, key: str, value: float, quality: Any = 'GOOD', ts: float | None = None):
        """
        更新数据快照

        Args:
            key: 数据键，格式 "device_id:register_name"
            value: 当前值
            quality: 数据质量码，默认 'GOOD'（也接受数值 192）
            ts: 单调时钟时间戳（秒），默认当前时间
        """
        # 裸 float() 只捕 TypeError/ValueError 挡不住 NaN/Inf（`float("nan")` 不抛、
        # `float("1e400")` 得到 inf 也不抛），也挡不住 int 路径的 OverflowError。
        # 统一走项目里的 try_finite_float：解析失败与非有限值都返回 None；同时拒绝 bool。
        if isinstance(value, bool):
            logger.error("数据快照拒绝布尔输入: key=%s, value=%r", key, value)
            return
        numeric_value = try_finite_float(value)
        if numeric_value is None:
            logger.error("数据快照拒绝非数值/非有限输入: key=%s, value=%r", key, value)
            return
        ts_val = self._normalize_ts(ts)
        if ts_val is None:
            logger.error("数据快照拒绝非法时间戳: key=%s, ts=%r", key, ts)
            return
        with self._lock:
            self._data_snapshot[key] = numeric_value
            self._data_quality[key] = (quality, ts_val)

    def forget_device(self, device_id: str) -> bool:
        """设备删除后清理该设备在边缘决策引擎中的缓存状态。"""
        if not device_id:
            return False
        prefix = f"{device_id}:"
        removed = False
        with self._lock:
            for container in (self._data_snapshot, self._data_quality):
                stale_keys = [
                    k for k in list(container.keys())
                    if k == device_id or k.startswith(prefix)
                ]
                for k in stale_keys:
                    container.pop(k, None)
                    removed = True
        if removed:
            logger.info("边缘决策引擎已清理已删除设备的缓存数据: %s", device_id)
        return removed

    def register_action(self, action_name: str, callback: Callable[..., Any]):
        """
        注册动作回调

        Args:
            action_name: 动作名称
            callback: 回调函数 callback(device_id, register_name, value)
        """
        with self._lock:
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
        # BUG-050：写方必须持锁。`_execute_cycle` 在锁内 `sorted(self.rules.items())`，
        # 但写方不持锁 → 那把锁形同虚设，并发时 `RuntimeError` 会让整轮
        # （含安全联锁）被跳过。
        with self._lock:
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
        # BUG-050：与 add_rule 同口径 —— 联锁字典同样是工作线程的遍历对象。
        with self._lock:
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
        with self._lock:
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
        # BUG-050：与 add_rule 同锁 —— 删除同样会改变字典大小。
        with self._lock:
            self.rules.pop(rule_id, None)

    def remove_interlock(self, interlock_id: str):
        """删除联锁"""
        with self._lock:
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
            _raw_value, _raw_threshold = value, threshold
            value = self._finite_non_bool(value)
            threshold = self._finite_non_bool(threshold)
            if value is None or threshold is None:
                logger.warning(
                    "条件评估忽略非数值/非有限输入: key=%s, value=%r, threshold=%r",
                    key, _raw_value, _raw_threshold)
                return False

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
                value2 = self._finite_non_bool(condition.get('value2', threshold))
                if value2 is None:
                    logger.warning("条件评估忽略非数值/非有限上界: key=%s, value2=%r",
                                   key, condition.get('value2'))
                    return False
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

        with self._lock:
            callback = self._action_callbacks.get(callback_key)

        if not callback:
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
                # round 369：**必须看返回值**。SCADA 内所有写路径
                # （DeviceManager.write_register / ModbusClient /
                # DeviceControlSafety）失败时都是**返回 False 或
                # {'success': False}**，不抛异常 —— 只看异常会把
                # "PLC 拒写"记成 ok=True，触发计数照涨，看板上像是闭环生效了。
                res = callback(device_id, register_name, value)
                if self._action_failed(res):
                    log_entry['ok'] = False
                    log_entry['result'] = (
                        f'写入未生效: {target} = {value}（底层返回 {res!r}）')
                    with self._lock:
                        self._action_failures += 1
                    logger.error("动作写入未生效: %s = %s（底层返回 %r）",
                                 target, value, res)
                else:
                    log_entry['result'] = f'写入 {target} = {value}'
            except Exception as e:      # noqa: BLE001
                log_entry['result'] = f'写入失败: {e}'
                log_entry['ok'] = False
                with self._lock:
                    self._action_failures += 1
                logger.error(f"动作执行失败: {e}")

        elif action_type == 'set_alarm':
            alarm_msg = action.get('message', f'规则 {rule_id} 触发')
            # round 300：回调抛异常时不许再写「报警: xxx」——
            # 现场音柱没响、日志说报了警，正是 287 那一族的老毛病。
            try:
                # BUG-050：`set_alarm` 原来不看返回值 —— 音柱/广播链路返回
                # False 时仍然记 `ok=True`、`trigger_count` 照涨、失败数不涨，
                # 看板显示「联锁已触发」，现场一声没响。
                res = callback(alarm_msg, action.get('level', 'warning'))
                if self._action_failed(res):
                    log_entry['ok'] = False
                    log_entry['result'] = f'报警未生效: {alarm_msg}（底层返回 {res!r}）'
                    with self._lock:
                        self._action_failures += 1
                    logger.error("set_alarm 回调返回失败: %r", res)
                else:
                    log_entry['result'] = f'报警: {alarm_msg}'
            except Exception as e:      # noqa: BLE001 —— 动作失败不许拖垮决策循环
                log_entry['ok'] = False
                log_entry['result'] = f'报警失败: {e}'
                with self._lock:
                    self._action_failures += 1
                logger.error("set_alarm 回调异常: %s", e)

        elif action_type == 'callback':
            callback_name = action.get('callback_name', '')
            try:
                # BUG-050：同上，回调返回 False 也算没生效。
                res = callback(snapshot)
                if self._action_failed(res):
                    log_entry['ok'] = False
                    log_entry['result'] = (
                        f'回调未生效: {callback_name}（底层返回 {res!r}）')
                    with self._lock:
                        self._action_failures += 1
                    logger.error("回调 %s 返回失败: %r", callback_name, res)
                else:
                    log_entry['result'] = f'回调: {callback_name}'
            except Exception as e:      # noqa: BLE001
                log_entry['ok'] = False
                log_entry['result'] = f'回调失败: {callback_name}: {e}'
                with self._lock:
                    self._action_failures += 1
                logger.error("回调 %s 异常: %s", callback_name, e)

        # 更新规则统计与决策日志（同一把锁内按 rule_type 精确查找，避免 rule/interlock 同名冲突）
        #
        # round 369：**动作没生效就不算"触发成功"**。
        with self._lock:
            if rule_type == 'interlock':
                target_rule = self.interlocks.get(rule_id)
            elif rule_type == 'rule':
                target_rule = self.rules.get(rule_id)
            else:
                target_rule = self.rules.get(rule_id) or self.interlocks.get(rule_id)
            if target_rule is not None and log_entry.get('ok', True):
                target_rule['last_triggered'] = datetime.now().isoformat()
                target_rule['trigger_count'] = target_rule.get('trigger_count', 0) + 1
            self.decision_log.append(log_entry)

        # round 300：动作没生效时不能再用 info 写一行「成功」样子的日志
        if log_entry.get('ok', True):
            logger.info(f"[{rule_type.upper()}] {rule_id}: {log_entry.get('result', '')}")
        else:
            logger.warning(f"[{rule_type.upper()}] {rule_id}: 动作未生效 — "
                           f"{log_entry.get('result', '')}")

    @staticmethod
    def _action_failed(result: Any) -> bool:
        """动作回调的返回值是否表示「没生效」（round 369 口径，BUG-050 补齐）。

        项目内所有写/执行路径失败时都是**返回 False 或 `{'success': False}`**，
        不抛异常。`None` 不能算失败 —— 通知类回调（如 `lambda msg, level: ...`）
        本来就不返回结论，否则每次报警都会被记成失败、`trigger_count` 永不增长。
        """
        # 口径统一到 `core.action_result`：False / {'success': False} 才算失败，
        # None 是"没有结论"（通知类回调本来就不返回结果）。
        return is_failure(result)

    def _execute_pid(self, ctrl_id: str, ctrl: dict[str, Any], snapshot: dict[str, Any]):
        """执行PID控制计算"""
        input_key = ctrl['input_key']
        if input_key not in snapshot:
            return
        process_value = snapshot.get(input_key)

        if process_value is None:
            return

        setpoint = ctrl['setpoint']
        kp = ctrl['kp']
        ki = ctrl['ki']
        kd = ctrl['kd']
        # 同上：统一用 _finite_non_bool，NaN/Inf/布尔/非数值一律 None → 放弃本次下发。
        _pv = self._finite_non_bool(process_value)
        _sp = self._finite_non_bool(setpoint)
        _kp = self._finite_non_bool(kp)
        _ki = self._finite_non_bool(ki)
        _kd = self._finite_non_bool(kd)
        if None in (_pv, _sp, _kp, _ki, _kd):
            logger.error(
                "PID控制器 %s 输入或参数非数值/非有限 (pv=%r, sp=%r, kp=%r, ki=%r, kd=%r)，放弃本次计算与下发",
                ctrl_id, process_value, setpoint, kp, ki, kd,
            )
            return
        process_value, setpoint, kp, ki, kd = _pv, _sp, _kp, _ki, _kd
        error = setpoint - process_value
        if not math.isfinite(error):
            logger.error("PID控制器 %s 误差非有限 (%r)，放弃本次计算与下发", ctrl_id, error)
            return

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
        p_term = kp * error

        # 积分项（带抗饱和）
        prev_integral = ctrl['integral']
        ctrl['integral'] += error * dt
        # 积分限幅
        max_integral = (ctrl['output_max'] - ctrl['output_min']) / ki if ki > 0 else 1e6
        if not (math.isfinite(ctrl['integral']) and math.isfinite(max_integral)):
            ctrl['integral'] = prev_integral
            logger.error("PID控制器 %s 积分项非有限，已回滚并放弃下发", ctrl_id)
            return
        ctrl['integral'] = max(-max_integral, min(max_integral, ctrl['integral']))
        i_term = ki * ctrl['integral']

        # 微分项
        d_term = kd * (error - ctrl['prev_error']) / dt if dt > 0 else 0

        # 输出
        output = p_term + i_term + d_term
        if not (
            math.isfinite(output)
            and math.isfinite(ctrl['output_min'])
            and math.isfinite(ctrl['output_max'])
        ):
            ctrl['integral'] = prev_integral
            logger.error("PID控制器 %s 输出非有限 (%r)，已回滚积分并放弃下发", ctrl_id, output)
            return
        output = max(ctrl['output_min'], min(ctrl['output_max'], output))

        ctrl['output'] = output
        ctrl['prev_error'] = error
        ctrl['prev_time'] = now

        # 写入输出
        output_key = ctrl['output_key']
        with self._lock:
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
            res = callback(device_id, register_name, output)
            # round 369：同 `_execute_action` —— 写失败是返回 False，不是抛异常
            if self._action_failed(res):
                with self._lock:
                    self._action_failures += 1
                logger.error("PID 输出写入未生效: %s = %s（底层返回 %r）",
                             output_key, output, res)
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
        if limit is not None and limit <= 0:
            return []
        with self._lock:
            if limit is None:
                return list(self.decision_log)
            return list(self.decision_log)[-limit:]

    def get_data_snapshot(self) -> dict[str, float]:
        """获取当前数据快照"""
        with self._lock:
            return dict(self._data_snapshot)

    def get_status(self) -> dict[str, Any]:
        """获取引擎状态"""
        with self._lock:
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
