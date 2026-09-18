"""
运维工具API
提供运行时配置、数据库维护、系统诊断、数据清理等运维接口
"""

import logging
from flask import Blueprint, request

from core.ops_tools import (
    ops_audit, runtime_config_manager, db_maintainer,
    data_cleaner, diagnostic_exporter,
)
from core.service_response import success_response, error_response
from 用户层.auth import jwt_required, role_required
from ._common import clamp_limit

logger = logging.getLogger(__name__)

ops_bp = Blueprint('api_ops', __name__, url_prefix='/api/ops')


def _result_error_detail(result):
    """从后端"吞异常 + 返回结果"的契约里抽出错误信息；无错返回 None。

    覆盖两种形态（同一个 `core/ops_tools.py` 里并存）：
      * dict：失败 → ``{'status': 'error', 'error': ...}``（DatabaseMaintainer/
        DataCleaner/DiagnosticExporter 都用这个）
      * list：失败 → ``[{'error': ...}]``（``DatabaseMaintainer.get_table_stats()``
        整体失败时走这条，见 core/ops_tools.py:373）
    """
    if isinstance(result, dict):
        if result.get('status') == 'error':
            return result.get('error') or result.get('result') or '未知错误'
        return None
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get('error'):
                return item['error']
    return None


def _ops_status_response(result, action):
    """运维类接口的统一返回：结果里 `status == 'error'` 必须变成**错误响应**。

    round 162 修复了清理接口；本次把同一契约推广到 `/db/*` 与
    `/diagnostics/export`。这些后端方法（`DatabaseMaintainer.*`、
    `DiagnosticExporter.export_diagnostics`）与 `DataCleaner` 是同一套
    "吞异常 + 返回结果字典"契约：失败时给 `{'status': 'error', 'error': ...}`，
    **不抛异常**。原先接口无条件包进 `success_response()` —— 于是失败时回
    **HTTP 200**、只在 body 里带一个 `status:'error'`，调用方只看 HTTP 状态
    就会显示"成功"。最危险的一例是 `integrity_check`：库已损坏时它也回
    `success: true`，运维据此认为数据库是好的。

    修复后：`status == 'error'` 一律 `error_response(..., 500)`，调用方**没法**忽略。

    Args:
        result: 后端方法返回的结果（通常是 dict，`/db/tables` 是 list）
        action: 动作名，用于拼错误信息，如 'VACUUM'
    """
    detail = _result_error_detail(result)
    if detail is not None:
        logger.error("运维操作失败 [%s]: %s", action, result)
        return error_response(f"{action}失败: {detail}", 500)
    return success_response(result)


def _cleanup_response(result):
    """清理类接口的统一返回：`status == 'error'` 必须变成**错误响应**。

    round 162 修复。`DataCleaner` 的方法是"吞异常 + 返回结果字典"的契约
    （失败时给 `{'status': 'error', 'error': ...}`，不抛异常）。原先三个清理接口
    无条件把它包进 `success_response()` —— 于是清理失败时接口回 **HTTP 200**、
    只在 body 里带一个 `status:'error'`，前端只看 HTTP 状态就会显示"清理成功"。
    这是本项目最典型的"静默假成功"。

    修复后：失败一律 `error_response(..., 500)`，让调用方**没法**忽略。
    """
    return _ops_status_response(result, '清理')


def _ops_operator(default: str = 'unknown') -> str:
    """从 JWT 上下文取操作者（不信任客户端传入的 operator）。"""
    user = getattr(request, 'current_user', None) or {}
    return user.get('username') or default


def _log_ops(operation, result, target: str = '', details=None):
    """按后端返回的**真实 status** 写审计日志。

    一起修两个问题（都在 `core/ops_tools.py` 的 `OpsAuditLogger.log_operation` 契约上）：

    1. `log_operation` 的 `result` 参数默认值是 `'success'`，而本文件所有调用点
       都没传它。于是 `db_maintainer.vacuum()` 返回 `{'status': 'error'}`、
       `data_cleaner.clean_history_data()` 清理失败时，审计日志照样落一条
       `result: 'success'` —— 事后追责读到的是"操作成功"，与事实完全相反。
    2. 这些**变更类**操作也都没传 `operator`，日志里一律是默认值 `'system'`。
       出事后（谁把库 VACUUM 坏了 / 谁删了历史数据）根本定位不到人。

    Args:
        operation: 审计动作名，如 'db_vacuum'
        result: 后端方法返回的结果（dict / list 均可）
        target: 操作对象
        details: 落日志的明细，缺省用 result 本身
    """
    detail = _result_error_detail(result)
    ops_audit.log_operation(
        operation,
        operator=_ops_operator(),
        target=target,
        details=details if details is not None else (
            result if isinstance(result, dict) else {'result': result}),
        result='error' if detail is not None else 'success',
        error=detail or '',
    )


# ================================================================
# 运行时配置 API
# ================================================================

@ops_bp.route('/config', methods=['GET'])
@jwt_required
def get_runtime_config():
    """获取运行时配置"""
    try:
        return success_response(runtime_config_manager.get_all())
    except Exception as e:
        logger.error(f"获取运行时配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/config', methods=['PUT'])
@jwt_required
@role_required('admin')
def update_runtime_config():
    """更新运行时配置"""
    try:
        data = request.get_json()
        if not data:
            return error_response("请求体不能为空", 400)

        # SECURITY: 从JWT获取操作者，不信任客户端传入
        operator = request.current_user.get('username', 'unknown')
        updates = data.get('config', {})
        if not updates:
            return error_response("config 字段不能为空", 400)

        changed = runtime_config_manager.update_batch(updates, operator)

        # 记录审计
        ops_audit.log_operation(
            'config_update', operator=operator,
            details={'keys': list(updates.keys()), 'changed_count': changed},
        )

        return success_response({
            'changed_count': changed,
            'config': runtime_config_manager.get_all(),
        })
    except Exception as e:
        logger.error(f"更新运行时配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/config/<key>', methods=['GET'])
@jwt_required
def get_config_key(key):
    """获取单个配置值"""
    try:
        value = runtime_config_manager.get(key)
        if value is None:
            return error_response(f"配置项 {key} 不存在", 404)
        return success_response({'key': key, 'value': value})
    except Exception as e:
        logger.error(f"获取配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/config/<key>', methods=['PUT'])
@jwt_required
@role_required('admin')
def set_config_key(key):
    """设置单个配置值"""
    try:
        data = request.get_json()
        if not data or 'value' not in data:
            return error_response("value 字段必填", 400)

        # SECURITY: 从JWT获取操作者，不信任客户端传入
        operator = request.current_user.get('username', 'unknown')
        value = data['value']
        changed = runtime_config_manager.set(key, value, operator)

        ops_audit.log_operation(
            'config_set', operator=operator, target=key,
            details={'value': value, 'changed': changed},
        )

        return success_response({'key': key, 'value': value, 'changed': changed})
    except Exception as e:
        logger.error(f"设置配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/config/history', methods=['GET'])
@jwt_required
def get_config_history():
    """获取配置变更历史"""
    try:
        limit = clamp_limit(request.args.get('limit', 50, type=int), default=50, maximum=500)
        return success_response(runtime_config_manager.get_history(limit))
    except Exception as e:
        logger.error(f"获取配置历史失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 数据库维护 API
# ================================================================

@ops_bp.route('/db/vacuum', methods=['POST'])
@jwt_required
@role_required('admin')
def db_vacuum():
    """执行 VACUUM"""
    try:
        result = db_maintainer.vacuum()
        ops_audit.log_operation('db_vacuum', details=result)
        return _ops_status_response(result, 'VACUUM')
    except Exception as e:
        logger.error(f"VACUUM 失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/db/reindex', methods=['POST'])
@jwt_required
@role_required('admin')
def db_reindex():
    """执行 REINDEX"""
    try:
        data = request.get_json() or {}
        table = data.get('table')
        result = db_maintainer.reindex(table)
        ops_audit.log_operation('db_reindex', details=result)
        return _ops_status_response(result, 'REINDEX')
    except Exception as e:
        logger.error(f"REINDEX 失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/db/analyze', methods=['POST'])
@jwt_required
@role_required('admin')
def db_analyze():
    """执行 ANALYZE"""
    try:
        result = db_maintainer.analyze()
        ops_audit.log_operation('db_analyze', details=result)
        return _ops_status_response(result, 'ANALYZE')
    except Exception as e:
        logger.error(f"ANALYZE 失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/db/integrity', methods=['GET'])
@jwt_required
def db_integrity_check():
    """数据库完整性检查"""
    try:
        result = db_maintainer.integrity_check()
        # 最危险的一处：库损坏时 integrity_check 返回 status:'error'，
        # 若包成 success 则运维会认为库是好的。
        return _ops_status_response(result, '数据库完整性检查')
    except Exception as e:
        logger.error(f"完整性检查失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/db/tables', methods=['GET'])
@jwt_required
def db_table_stats():
    """获取数据库表统计"""
    try:
        result = db_maintainer.get_table_stats()
        # get_table_stats() 的失败形态是 **list**：`[{'error': ...}]`（core/ops_tools.py:373），
        # 原先无条件 `success_response()` → 前端拿到 HTTP 200「成功」，
        # 而 body 里只有一条错误项，表统计实际上是空的。
        return _ops_status_response(result, '获取表统计')
    except Exception as e:
        logger.error(f"获取表统计失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 数据清理 API
# ================================================================

@ops_bp.route('/cleanup/history', methods=['POST'])
@jwt_required
@role_required('admin')
def cleanup_history():
    """清理过期历史数据"""
    try:
        data = request.get_json() or {}
        days = data.get('retention_days', 90)
        result = data_cleaner.clean_history_data(days)
        _log_ops('cleanup_history', result, details={'retention_days': days, **(result if isinstance(result, dict) else {})})
        return _cleanup_response(result)
    except Exception as e:
        logger.error(f"清理历史数据失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/cleanup/backups', methods=['POST'])
@jwt_required
@role_required('admin')
def cleanup_backups():
    """清理旧备份"""
    try:
        data = request.get_json() or {}
        keep = data.get('keep_count', 5)
        result = data_cleaner.clean_old_backups(keep_count=keep)
        _log_ops('cleanup_backups', result, details={'keep_count': keep, **(result if isinstance(result, dict) else {})})
        return _cleanup_response(result)
    except Exception as e:
        logger.error(f"清理备份失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/cleanup/logs', methods=['POST'])
@jwt_required
@role_required('admin')
def cleanup_logs():
    """清理过期日志"""
    try:
        data = request.get_json() or {}
        days = data.get('retention_days', 30)
        result = data_cleaner.clean_log_files(retention_days=days)
        ops_audit.log_operation('cleanup_logs', details=result)
        return _cleanup_response(result)
    except Exception as e:
        logger.error(f"清理日志失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 系统诊断 API
# ================================================================

@ops_bp.route('/diagnostics/export', methods=['POST'])
@jwt_required
@role_required('admin')
def export_diagnostics():
    """一键导出系统诊断信息"""
    try:
        data = request.get_json() or {}
        result = diagnostic_exporter.export_diagnostics(
            include_logs=data.get('include_logs', True),
            include_config=data.get('include_config', True),
            include_db_stats=data.get('include_db_stats', True),
            include_system_state=data.get('include_system_state', True),
        )
        ops_audit.log_operation('export_diagnostics', details={'status': result.get('status')})
        return _ops_status_response(result, '导出诊断信息')
    except Exception as e:
        logger.error(f"导出诊断信息失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 审计日志 API
# ================================================================

@ops_bp.route('/audit', methods=['GET'])
@jwt_required
def get_ops_audit():
    """获取运维操作审计记录"""
    try:
        limit = clamp_limit(request.args.get('limit', 50, type=int), default=50, maximum=500)
        return success_response(ops_audit.get_recent(limit))
    except Exception as e:
        logger.error(f"获取审计记录失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 后台维护任务 API
# ================================================================

@ops_bp.route('/maintenance/tasks', methods=['GET'])
@jwt_required
def get_maintenance_tasks():
    """查看后台维护任务（归档/缓存清理/黑名单清理）的运行状态

    这些任务负责清理会无界增长的结构（JWT 黑名单、API 缓存、nonce、
    限流计数、离线消息队列、分层缓存），并用例程归档历史数据。
    """
    try:
        from core.maintenance import get_maintenance_status
        return success_response(get_maintenance_status())
    except Exception as e:
        logger.error(f"获取维护任务状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@ops_bp.route('/maintenance/tasks/<name>/run', methods=['POST'])
@jwt_required
@role_required('admin')
def run_maintenance_task(name):
    """立即执行一次指定的维护任务（不等调度周期）"""
    try:
        from core.maintenance import get_maintenance_status, run_task_now

        if name not in (get_maintenance_status() or {}):
            return error_response(f"未注册的维护任务: {name}", 404)

        operator = request.current_user.get('username', 'unknown')
        accepted = run_task_now(name)
        # accepted=False 表示任务**没有**被接受执行（未注册/已在运行/调度器不可用）。
        # 原先固定按默认值记 success —— 管理员点了「立即执行」，审计里显示成功，
        # 而任务根本没跑。这里如实记 error。
        ops_audit.log_operation(
            'maintenance_task_run', operator=operator, target=name,
            details={'task': name, 'accepted': accepted},
            result='success' if accepted else 'error',
            error='' if accepted else '维护任务未被接受执行',
        )

        return success_response({'task': name, 'accepted': accepted})
    except Exception as e:
        logger.error(f"执行维护任务失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)
