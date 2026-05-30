"""
配方/批量过程模拟器
模拟多阶段生产过程（如注塑、发酵、化工反应）
"""
import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable
from enum import Enum

logger = logging.getLogger(__name__)


class RecipePhase(Enum):
    """配方阶段"""
    IDLE = 'idle'
    HEATING = 'heating'
    HOLDING = 'holding'
    INJECTION = 'injection'
    COOLING = 'cooling'
    EJECTION = 'ejection'
    COMPLETE = 'complete'


@dataclass
class PhaseConfig:
    """阶段配置"""
    name: str
    duration: float  # 秒
    setpoints: Dict[str, float]  # 参数名 → 目标值
    transitions: Dict[str, str] = field(default_factory=dict)  # 条件 → 下一阶段
    tolerance: float = 0.05  # 5% 容差


@dataclass
class Recipe:
    """生产配方"""
    name: str
    version: str
    phases: List[PhaseConfig]
    parameters: Dict[str, float] = field(default_factory=dict)  # 全局参数


class RecipeSimulator:
    """配方模拟器 - 驱动设备行为模拟器按配方运行"""

    # 注塑成型配方示例
    INJECTION_MOLD_RECIPE = Recipe(
        name="标准注塑",
        version="1.0",
        phases=[
            PhaseConfig(
                name=RecipePhase.HEATING.value,
                duration=120,
                setpoints={'mold_temperature': 180, 'barrel_temperature': 220},
            ),
            PhaseConfig(
                name=RecipePhase.HOLDING.value,
                duration=30,
                setpoints={'mold_temperature': 180, 'injection_pressure': 80},
            ),
            PhaseConfig(
                name=RecipePhase.INJECTION.value,
                duration=15,
                setpoints={
                    'injection_pressure': 120,
                    'injection_speed': 85,
                    'mold_temperature': 175,
                },
            ),
            PhaseConfig(
                name=RecipePhase.COOLING.value,
                duration=60,
                setpoints={'mold_temperature': 40, 'cooling_flow': 50},
            ),
            PhaseConfig(
                name=RecipePhase.EJECTION.value,
                duration=10,
                setpoints={'clamping_force': 0, 'mold_temperature': 35},
            ),
        ],
        parameters={'cycle_target': 240, 'shot_weight': 150}
    )

    # 发酵过程配方
    FERMENTATION_RECIPE = Recipe(
        name="标准发酵",
        version="1.0",
        phases=[
            PhaseConfig(
                name="sterilization",
                duration=1800,
                setpoints={'temperature': 121, 'pressure': 0.15, 'ph_value': 7.0},
            ),
            PhaseConfig(
                name="inoculation",
                duration=300,
                setpoints={'temperature': 37, 'dissolved_oxygen': 80, 'agitation_speed': 200},
            ),
            PhaseConfig(
                name="growth",
                duration=14400,
                setpoints={'temperature': 37, 'ph_value': 6.8, 'dissolved_oxygen': 40},
            ),
            PhaseConfig(
                name="harvest",
                duration=1800,
                setpoints={'temperature': 4, 'agitation_speed': 50},
            ),
        ],
    )

    RECIPES = {
        'injection_mold': INJECTION_MOLD_RECIPE,
        'fermentation': FERMENTATION_RECIPE,
    }

    def __init__(self, recipe: Recipe, behavior_simulator=None):
        self.recipe = recipe
        self.behavior_simulator = behavior_simulator
        self.current_phase_index = 0
        self.phase_start_time = time.time()
        self.is_running = False
        self._lock = threading.Lock()
        self._completion_callback: Optional[Callable] = None
        self._cycle_count = 0
        self._history: List[dict] = []

    @property
    def current_phase(self) -> Optional[PhaseConfig]:
        if 0 <= self.current_phase_index < len(self.recipe.phases):
            return self.recipe.phases[self.current_phase_index]
        return None

    def start(self):
        """开始配方执行"""
        with self._lock:
            self.current_phase_index = 0
            self.phase_start_time = time.time()
            self.is_running = True
            logger.info(f"配方 '{self.recipe.name}' 开始执行")

    def stop(self):
        """停止配方执行"""
        with self._lock:
            self.is_running = False
            logger.info(f"配方 '{self.recipe.name}' 停止")

    def update(self, dt: float) -> Dict[str, float]:
        """更新配方状态，返回当前目标值"""
        with self._lock:
            if not self.is_running or not self.current_phase:
                return {}

            phase = self.current_phase
            elapsed = time.time() - self.phase_start_time

            # 检查阶段是否完成
            if elapsed >= phase.duration:
                self._advance_phase()
                return self.get_current_setpoints()

            # 返回当前阶段的目标值（带渐变）
            return self._interpolate_setpoints(phase, elapsed)

    def _advance_phase(self):
        """进入下一阶段"""
        self._history.append({
            'phase': self.current_phase.name,
            'duration': time.time() - self.phase_start_time,
            'time': time.time()
        })

        self.current_phase_index += 1
        self.phase_start_time = time.time()

        if self.current_phase_index >= len(self.recipe.phases):
            self.is_running = False
            self._cycle_count += 1
            logger.info(f"配方 '{self.recipe.name}' 完成 (周期 #{self._cycle_count})")
            if self._completion_callback:
                self._completion_callback(self._cycle_count)
        else:
            logger.info(f"配方进入阶段: {self.current_phase.name}")

    def _interpolate_setpoints(self, phase: PhaseConfig, elapsed: float) -> Dict[str, float]:
        """在阶段内渐变到目标值"""
        progress = min(1.0, elapsed / max(1.0, phase.duration))
        # 使用S曲线渐变（更真实的工业过程）
        smooth_progress = 3 * progress**2 - 2 * progress**3

        result = {}
        for param, target in phase.setpoints.items():
            if self.behavior_simulator and hasattr(self.behavior_simulator, f'_current_{param}'):
                current = getattr(self.behavior_simulator, f'_current_{param}')
                result[param] = current + (target - current) * smooth_progress
            else:
                result[param] = target * smooth_progress
        return result

    def get_current_setpoints(self) -> Dict[str, float]:
        """获取当前阶段的目标值"""
        if self.current_phase:
            return dict(self.current_phase.setpoints)
        return {}

    def get_status(self) -> dict:
        """获取配方状态"""
        with self._lock:
            phase = self.current_phase
            elapsed = time.time() - self.phase_start_time if self.is_running else 0
            return {
                'recipe': self.recipe.name,
                'phase': phase.name if phase else 'idle',
                'phase_index': self.current_phase_index,
                'total_phases': len(self.recipe.phases),
                'elapsed': round(elapsed, 1),
                'duration': phase.duration if phase else 0,
                'progress': round(min(1.0, elapsed / phase.duration) * 100, 1) if phase else 0,
                'is_running': self.is_running,
                'cycle_count': self._cycle_count,
                'setpoints': self.get_current_setpoints(),
            }
