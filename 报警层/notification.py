"""
通知模块
实现报警通知功能
"""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape
from typing import Any

logger = logging.getLogger(__name__)

#: SMTP 连接/读写的默认超时（秒）。round 288：不设超时会让投递线程永久阻塞。
#: 10 秒足够完成一次正常的 STARTTLS + LOGIN + 发信，也不会让停机等太久。
SMTP_TIMEOUT_S = 10.0


class Notification:
    """
    通知管理类
    支持邮件、短信等通知方式
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化通知管理

        Args:
            config: 通知配置
        """
        self.config = config or {}

        # 邮件配置
        self.email_config = self.config.get('email', {})
        self.email_enabled = self.email_config.get('enabled', False)

        # 短信配置
        self.sms_config = self.config.get('sms', {})
        self.sms_enabled = self.sms_config.get('enabled', False)

    def send_email(self, subject: str, message: str, 
                   recipients: list[str] = None) -> bool:
        """
        发送邮件通知

        Args:
            subject: 邮件主题
            message: 邮件内容
            recipients: 收件人列表

        Returns:
            bool: 发送是否成功
        """
        if not self.email_enabled:
            logger.warning("邮件通知未启用")
            return False

        try:
            # 获取配置
            smtp_server = self.email_config.get('smtp_server')
            smtp_port = self.email_config.get('smtp_port', 587)
            username = self.email_config.get('username')
            password = self.email_config.get('password')

            if not all([smtp_server, username, password]):
                logger.error("邮件配置不完整")
                return False

            # 收件人
            if not recipients:
                recipients = self.email_config.get('recipients', [])

            if not recipients:
                logger.warning("没有收件人")
                return False

            # 创建邮件
            msg = MIMEMultipart()
            msg['From'] = username
            msg['To'] = ', '.join(recipients)
            msg['Subject'] = subject

            # 添加正文
            msg.attach(MIMEText(message, 'html', 'utf-8'))

            # 发送邮件
            # round 288：**必须显式给超时**。`smtplib.SMTP(host, port)` 不传
            # timeout 时用的是 `socket._GLOBAL_DEFAULT_TIMEOUT`（= 阻塞到永远）。
            # SMTP 服务器被防火墙丢包 / 断电时，`starttls()` / `login()` 会卡住
            # 几分钟甚至永远 —— 而报警管理器只有一个投递线程，
            # 那一条卡死就等于**所有邮件都发不出去**，且失败计数一次都不涨
            # （第一个任务还没返回），「通知通道坏了」永远报不出来。
            timeout = self.email_config.get('timeout', SMTP_TIMEOUT_S)
            # 用 try_finite_float 而不是裸 float()：`float('inf')` 不抛异常，
            # 会让"超时无穷大"静默通过（`test_non_finite_input_guard` 会拦）。
            from core.safe_numeric import try_finite_float
            timeout = try_finite_float(timeout)
            if timeout is None or timeout <= 0:
                timeout = SMTP_TIMEOUT_S
            with smtplib.SMTP(smtp_server, smtp_port, timeout=timeout) as server:
                server.starttls()
                server.login(username, password)
                server.send_message(msg)

            logger.info(f"邮件发送成功: {subject}")
            return True

        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
            return False

    def send_alarm_notification(self, alarm_data: dict[str, Any]) -> bool:
        """
        发送报警通知

        Args:
            alarm_data: 报警数据

        Returns:
            bool: 发送是否成功
        """
        # 构建邮件内容
        subject = f"[{alarm_data.get('level', 'warning').upper()}] {alarm_data.get('message', '报警通知')}"

        message = f"""
        <html>
        <body>
            <h2>报警通知</h2>
            <table border="1" cellpadding="5" cellspacing="0">
                <tr>
                    <td><strong>报警级别</strong></td>
                    <td>{escape(str(alarm_data.get('level', 'warning')))}</td>
                </tr>
                <tr>
                    <td><strong>设备ID</strong></td>
                    <td>{escape(str(alarm_data.get('device_id', '-')))}</td>
                </tr>
                <tr>
                    <td><strong>参数名称</strong></td>
                    <td>{escape(str(alarm_data.get('register_name', '-')))}</td>
                </tr>
                <tr>
                    <td><strong>当前值</strong></td>
                    <td>{escape(str(alarm_data.get('value', '-')))}</td>
                </tr>
                <tr>
                    <td><strong>阈值</strong></td>
                    <td>{escape(str(alarm_data.get('threshold', '-')))}</td>
                </tr>
                <tr>
                    <td><strong>报警时间</strong></td>
                    <td>{escape(str(alarm_data.get('timestamp', '-')))}</td>
                </tr>
                <tr>
                    <td><strong>报警消息</strong></td>
                    <td>{escape(str(alarm_data.get('message', '-')))}</td>
                </tr>
            </table>
            <p>请及时处理！</p>
        </body>
        </html>
        """

        # 发送邮件
        return self.send_email(subject, message)

    def send_sms(self, phone_numbers: list[str], message: str) -> bool:
        """
        发送短信通知

        ⚠️ **尚未实现**（round 250 如实标注）。此前这个方法直接
        ``logger.info(...)`` 然后 ``return True`` —— 一条短信都没发，
        却告诉调用方"发送成功"。这是比"没接线"更坏的形态：一旦有人
        把它接进报警链路，运维会在日志里看到"短信发送: 发送3条短信"，
        于是放心地依赖一个从不工作的通道。

        现在改为：明确报错并返回 False，让失败在**接线的那一刻就可见**。

        要实现它需要补的是（按 `配置/alarms.yaml` 的 `notification.sms` 段）：
        1. 选服务商：配置里写的是 `provider: aliyun`，即阿里云短信
           （`dysms.aliyuncs.com`，POP RPC 协议，SignatureVersion=1.0，
           HMAC-SHA1 签名 —— 百分号编码规则是这里最容易错的地方）；
        2. 用 `access_key` / `secret_key` 签名，`sign_name` + `template_code`
           调用 SendSms，`phone_numbers` 为接收方；
        3. **必须能在没有真实凭证的环境下被测试**：签名构造单独成函数，
           用固定向量单测（否则签名错一条都发不出去，且只在生产炸）。

        在完成上述三项之前，请保持 `配置/alarms.yaml` 里
        `notification.sms.enabled: false`。

        Args:
            phone_numbers: 手机号列表
            message: 短信内容

        Returns:
            bool: 发送是否成功（当前恒为 False —— 未实现）
        """
        logger.error(
            "短信通知**未实现**：send_sms 是占位实现，不会发出任何短信"
            "（phone_numbers=%d 条）。详见本方法 docstring 里的实现清单；"
            "在实现完成前请把 notification.sms.enabled 置为 false。",
            len(phone_numbers) if phone_numbers else 0,
        )
        return False

    def test_email(self) -> bool:
        """
        测试邮件发送

        Returns:
            bool: 测试是否成功
        """
        return self.send_email(
            subject="测试邮件",
            message="<h1>测试邮件</h1><p>这是一封测试邮件。</p>"
        )
