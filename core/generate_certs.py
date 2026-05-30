"""
TLS证书生成工具
===============
GB/T 35718 + GB/T 37980 要求：工控系统通信必须加密

生成自签名证书用于开发/测试环境。
生产环境应使用CA签发的正式证书。

Usage:
    python -m core.generate_certs
    python -m core.generate_certs --hostname 192.168.1.100
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


def generate_self_signed_cert(hostname: str = 'localhost',
                               cert_dir: str = 'certs',
                               days_valid: int = 365,
                               key_size: int = 2048):
    """
    生成自签名TLS证书

    Args:
        hostname: 服务器主机名或IP
        cert_dir: 证书输出目录
        days_valid: 证书有效期（天）
        key_size: RSA密钥长度（位）
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("错误: 需要安装 cryptography 库")
        print("  pip install cryptography")
        sys.exit(1)

    cert_path = Path(cert_dir)
    cert_path.mkdir(parents=True, exist_ok=True)

    # 生成RSA私钥
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
    )

    # 构建证书主体
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, 'CN'),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, 'Beijing'),
        x509.NameAttribute(NameOID.LOCALITY_NAME, 'Beijing'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Industrial SCADA System'),
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])

    # 构建证书
    now = datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days_valid))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(hostname),
                x509.IPAddress(
                    __import__('ipaddress').ip_address(hostname)
                    if _is_ip(hostname) else
                    __import__('ipaddress').ip_address('127.0.0.1')
                ),
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    # 写入私钥文件
    key_file = cert_path / 'server.key'
    with open(key_file, 'wb') as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    # 写入证书文件
    cert_file = cert_path / 'server.crt'
    with open(cert_file, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    # 设置权限（仅限Unix系统）
    if os.name != 'nt':
        os.chmod(key_file, 0o600)

    print(f"TLS 证书已生成:")
    print(f"  证书: {cert_file}")
    print(f"  私钥: {key_file}")
    print(f"  主机名: {hostname}")
    print(f"  有效期: {days_valid} 天 (至 {(now + timedelta(days=days_valid)).strftime('%Y-%m-%d')})")
    print(f"  密钥长度: {key_size} bits")
    print()
    print("启用 TLS:")
    print(f"  set SCADA_TLS_ENABLED=true")
    print(f"  set SCADA_TLS_CERT={cert_file}")
    print(f"  set SCADA_TLS_KEY={key_file}")
    print()
    print("注意: 自签名证书仅用于开发/测试。")
    print("生产环境请使用CA签发的正式证书。")


def _is_ip(s: str) -> bool:
    """检查字符串是否是IP地址"""
    try:
        __import__('ipaddress').ip_address(s)
        return True
    except ValueError:
        return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='生成SCADA系统TLS证书')
    parser.add_argument('--hostname', default='localhost',
                       help='服务器主机名或IP地址 (默认: localhost)')
    parser.add_argument('--cert-dir', default='certs',
                       help='证书输出目录 (默认: certs)')
    parser.add_argument('--days', type=int, default=365,
                       help='证书有效期天数 (默认: 365)')
    parser.add_argument('--key-size', type=int, default=2048,
                       help='RSA密钥长度 (默认: 2048)')

    args = parser.parse_args()
    generate_self_signed_cert(
        hostname=args.hostname,
        cert_dir=args.cert_dir,
        days_valid=args.days,
        key_size=args.key_size,
    )
