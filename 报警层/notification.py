"""
通知模块
实现报警通知功能
"""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

logger = logging.getLogger(__name__)


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
            with smtplib.SMTP(smtp_server, smtp_port) as server:
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
                    <td>{alarm_data.get('level', 'warning')}</td>
                </tr>
                <tr>
                    <td><strong>设备ID</strong></td>
                    <td>{alarm_data.get('device_id', '-')}</td>
                </tr>
                <tr>
                    <td><strong>参数名称</strong></td>
                    <td>{alarm_data.get('register_name', '-')}</td>
                </tr>
                <tr>
                    <td><strong>当前值</strong></td>
                    <td>{alarm_data.get('value', '-')}</td>
                </tr>
                <tr>
                    <td><strong>阈值</strong></td>
                    <td>{alarm_data.get('threshold', '-')}</td>
                </tr>
                <tr>
                    <td><strong>报警时间</strong></td>
                    <td>{alarm_data.get('timestamp', '-')}</td>
                </tr>
                <tr>
                    <td><strong>报警消息</strong></td>
                    <td>{alarm_data.get('message', '-')}</td>
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

        Args:
            phone_numbers: 手机号列表
            message: 短信内容

        Returns:
            bool: 发送是否成功
        """
        if not self.sms_enabled:
            logger.warning("短信通知未启用")
            return False

        # 这里可以实现短信发送逻辑
        # 示例：使用阿里云短信服务
        # 安全地记录日志，不暴露敏感信息
        logger.info(f"短信发送: 发送{len(phone_numbers)}条短信")
        return True

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
