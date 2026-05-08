"""
工业4.0智能层API
预测性维护/OEE/SPC/能源管理/边缘决策/总览
"""

import logging
from flask import Blueprint, jsonify, request, current_app

from 用户层.auth import jwt_required
from core.module_registry import ModuleRegistry
from core.service_response import success_response, module_unavailable_response

logger = logging.getLogger(__name__)

industry40_bp = Blueprint('api_industry40', __name__, url_prefix='/api')

_require_auth = jwt_required


# ==================== 工业4.0智能层API ====================

@industry40_bp.route('/industry40/health', methods=['GET'])
@_require_auth
def get_health_scores():
    """获取所有设备健康评分"""
    pm = current_app.predictive_maintenance
    if not pm:
        return jsonify({})
    scores = pm.get_health_scores()
    return jsonify(scores if scores else {})


@industry40_bp.route('/industry40/health/<device_id>', methods=['GET'])
@_require_auth
def get_device_health_by_id(device_id):
    """获取指定设备健康评分"""
    pm = current_app.predictive_maintenance
    if not pm:
        return jsonify({'error': '预测性维护模块未启用'}), 503
    return jsonify(pm.get_device_health(device_id))


@industry40_bp.route('/industry40/maintenance-alerts', methods=['GET'])
@_require_auth
def get_maintenance_alerts():
    """获取维护建议列表"""
    pm = current_app.predictive_maintenance
    if not pm:
        return jsonify([])
    limit = request.args.get('limit', 50, type=int)
    return jsonify(pm.get_maintenance_alerts(limit))


@industry40_bp.route('/industry40/trend/<device_id>/<register_name>', methods=['GET'])
@_require_auth
def get_trend_data(device_id, register_name):
    """获取趋势分析数据"""
    pm = current_app.predictive_maintenance
    if not pm:
        return jsonify({'error': '预测性维护模块未启用'}), 503
    return jsonify(pm.get_trend_data(device_id, register_name))


@industry40_bp.route('/industry40/oee', methods=['GET'])
@_require_auth
def get_all_oee():
    """获取所有设备OEE"""
    oee = current_app.oee_calculator
    if not oee:
        return jsonify({})
    return jsonify(oee.get_all_oee())


@industry40_bp.route('/industry40/oee/<device_id>', methods=['GET'])
@_require_auth
def get_device_oee(device_id):
    """获取指定设备OEE"""
    oee = current_app.oee_calculator
    if not oee:
        return jsonify({'error': 'OEE模块未启用'}), 503
    result = oee.get_device_oee(device_id)
    return jsonify(result if result else {'error': '无数据'})


@industry40_bp.route('/industry40/spc/<device_id>/<register_name>', methods=['GET'])
@_require_auth
def get_spc_chart(device_id, register_name):
    """获取SPC控制图数据"""
    spc = current_app.spc_analyzer
    if not spc:
        return jsonify({'error': 'SPC模块未启用'}), 503
    chart = spc.get_control_chart(device_id, register_name)
    capability = spc.get_capability(device_id, register_name)
    return jsonify({
        'control_chart': chart,
        'capability': capability,
    })


@industry40_bp.route('/industry40/energy', methods=['GET'])
@_require_auth
def get_energy_summary():
    """获取能耗汇总"""
    em = current_app.energy_manager
    if not em:
        return module_unavailable_response('energy_manager')
    return jsonify(em.get_energy_summary())


@industry40_bp.route('/industry40/energy/cost', methods=['GET'])
@_require_auth
def get_energy_cost():
    """获取电费分时明细"""
    em = current_app.energy_manager
    if not em:
        return module_unavailable_response('energy_manager')
    return jsonify(em.get_energy_cost_breakdown())


@industry40_bp.route('/industry40/energy/carbon', methods=['GET'])
@_require_auth
def get_carbon_emission():
    """获取碳排放数据"""
    em = current_app.energy_manager
    if not em:
        return module_unavailable_response('energy_manager')
    return jsonify(em.get_carbon_emission())


@industry40_bp.route('/industry40/energy/power', methods=['GET'])
@_require_auth
def get_realtime_power():
    """获取实时功率"""
    em = current_app.energy_manager
    if not em:
        return module_unavailable_response('energy_manager')
    return jsonify({
        'total_power_kw': em.get_total_power(),
        'devices': em.get_realtime_power(),
    })


@industry40_bp.route('/industry40/edge/status', methods=['GET'])
@_require_auth
def get_edge_status():
    """获取边缘决策引擎状态"""
    edge = current_app.edge_decision
    if not edge:
        return module_unavailable_response('edge_decision')
    return jsonify(edge.get_status())


@industry40_bp.route('/industry40/edge/rules', methods=['GET'])
@_require_auth
def get_edge_rules():
    """获取边缘决策规则"""
    edge = current_app.edge_decision
    if not edge:
        return module_unavailable_response('edge_decision')
    return jsonify(edge.get_rules())


@industry40_bp.route('/industry40/edge/log', methods=['GET'])
@_require_auth
def get_edge_log():
    """获取决策日志"""
    edge = current_app.edge_decision
    if not edge:
        return module_unavailable_response('edge_decision')
    limit = request.args.get('limit', 50, type=int)
    return jsonify(edge.get_decision_log(limit))


@industry40_bp.route('/industry40/devices/status', methods=['GET'])
@_require_auth
def get_devices_status():
    """获取所有设备运行状态（用于工艺流程图）"""
    oee = current_app.oee_calculator
    if not oee:
        return jsonify({})
    return jsonify(oee.get_all_device_states())


@industry40_bp.route('/industry40/spc/violations', methods=['GET'])
@_require_auth
def get_spc_violations():
    """获取SPC判异结果"""
    spc = current_app.spc_analyzer
    if not spc:
        return jsonify([])
    device_id = request.args.get('device_id')
    limit = request.args.get('limit', 50, type=int)
    return jsonify(spc.get_violations(device_id, limit))


@industry40_bp.route('/industry40/overview', methods=['GET'])
@_require_auth
def get_industry40_overview():
    """工业4.0总览数据"""
    result = {
        'predictive_maintenance': {
            'status': 'unavailable',
            'device_count': 0,
            'avg_health_score': 0,
            'recent_alerts': [],
        },
        'oee': {
            'status': 'unavailable',
            'device_count': 0,
            'avg_oee_percent': 0,
            'devices': {},
        },
        'energy': {
            'status': 'unavailable',
            'total_energy_kwh': 0,
            'total_power_kw': 0,
            'electricity_cost': 0,
            'carbon_emission_kg': 0,
        },
        'edge_decision': {
            'status': 'unavailable',
            'running': False,
            'rules_count': 0,
            'interlocks_count': 0,
            'pid_controllers_count': 0,
        },
    }

    pm = current_app.predictive_maintenance
    if pm:
        scores = pm.get_health_scores()
        if scores:
            avg_health = sum(s.get('health_score', 0) for s in scores.values()) / len(scores)
            alerts = pm.get_maintenance_alerts(5)
            result['predictive_maintenance'] = {
                'status': 'available',
                'device_count': len(scores),
                'avg_health_score': round(avg_health, 1),
                'recent_alerts': alerts,
            }

    oee_calc = current_app.oee_calculator
    if oee_calc:
        all_oee = oee_calc.get_all_oee()
        if all_oee:
            avg_oee = sum(o.get('oee_percent', 0) for o in all_oee.values()) / len(all_oee)
            result['oee'] = {
                'status': 'available',
                'device_count': len(all_oee),
                'avg_oee_percent': round(avg_oee, 1),
                'devices': all_oee,
            }

    em = current_app.energy_manager
    if em:
        energy_summary = em.get_energy_summary()
        energy_summary['status'] = 'available'
        energy_summary['total_power_kw'] = em.get_total_power()
        result['energy'] = energy_summary

    edge = current_app.edge_decision
    if edge:
        edge_status = edge.get_status()
        edge_status['status'] = 'available'
        result['edge_decision'] = edge_status

    return jsonify(result)
