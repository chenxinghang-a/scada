# SmartSCADA 数据备份与恢复指南

## 概述

数据是工业SCADA系统的核心资产。本文档介绍数据备份策略、备份方法和恢复流程。

---

## 1. 备份策略

### 备份类型

| 类型 | 频率 | 保留期 | 说明 |
|------|------|--------|------|
| 全量备份 | 每日 | 30天 | 完整数据库备份 |
| 增量备份 | 每小时 | 7天 | WAL文件备份 |
| 归档备份 | 每月 | 1年 | 历史数据归档 |

### 备份内容
- 数据库文件: `data/scada.db`
- 配置文件: `配置/` 目录
- 日志文件: `logs/` 目录（可选）
- 证书文件: `certs/` 目录（如有）

---

## 2. 自动备份

### Windows任务计划

#### 创建备份脚本
```batch
@echo off
REM backup_daily.bat
set BACKUP_DIR=backups\%date:~0,4%-%date:~5,2%-%date:~8,2%
mkdir "%BACKUP_DIR%" 2>nul

REM 备份数据库
copy data\scada.db "%BACKUP_DIR%\scada.db"

REM 备份配置
xcopy /E /I 配置 "%BACKUP_DIR%\配置"

REM 压缩备份
powershell Compress-Archive -Path "%BACKUP_DIR%\*" -DestinationPath "%BACKUP_DIR%.zip"
rmdir /S /Q "%BACKUP_DIR%"

REM 删除30天前的备份
forfiles /P "backups" /S /M *.zip /D -30 /C "cmd /c del @path"

echo Backup completed: %BACKUP_DIR%.zip
```

#### 创建计划任务
```bash
schtasks /create /tn "SmartSCADA_DailyBackup" /tr "C:\path\to\backup_daily.bat" /sc daily /st 02:00
```

### Linux Cron
```bash
# 编辑crontab
crontab -e

# 添加每日备份任务
0 2 * * * /path/to/backup_daily.sh
```

---

## 3. 手动备份

### 使用Python脚本
```python
from 存储层.database import Database
from datetime import datetime

db = Database()
backup_path = f"backups/scada_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
db.backup(backup_path)
print(f"备份完成: {backup_path}")
```

### 使用命令行
```bash
# 备份数据库
copy data\scada.db backups\scada_%date:~0,8%.db

# 备份配置
xcopy /E /I 配置 backups\配置_%date:~0,8%
```

---

## 4. 数据归档

### 自动归档
系统每24小时自动归档7天前的历史数据。

### 手动归档
```python
from 存储层.database import Database

db = Database()

# 归档7天前的数据，删除30天前的数据
result = db.archive_old_data(archive_days=7, delete_days=30)
print(f"归档: {result['archived']} 条")
print(f"删除: {result['deleted_history']} 条")
```

### 归档表结构
```sql
CREATE TABLE history_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    register_name TEXT NOT NULL,
    avg_value REAL,
    min_value REAL,
    max_value REAL,
    sample_count INTEGER,
    archive_date DATE NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 5. 数据恢复

### 恢复流程

#### 步骤1: 停止服务
```bash
# Windows服务
nssm stop SmartSCADA

# 或终止进程
taskkill /IM python.exe /F
```

#### 步骤2: 备份当前数据
```bash
copy data\scada.db data\scada.db.bak
```

#### 步骤3: 恢复备份
```bash
copy backups\scada_20260603.db data\scada.db
```

#### 步骤4: 恢复配置
```bash
xcopy /E /Y backups\配置_20260603\* 配置\
```

#### 步骤5: 启动服务
```bash
nssm start SmartSCADA
```

### 使用Python恢复
```python
import shutil
from pathlib import Path

# 恢复数据库
backup_file = "backups/scada_20260603.db"
target_file = "data/scada.db"

# 备份当前文件
shutil.copy2(target_file, f"{target_file}.bak")

# 恢复备份
shutil.copy2(backup_file, target_file)

print(f"恢复完成: {backup_file} -> {target_file}")
```

---

## 6. WAL恢复

### WAL文件说明
- `scada.db`: 主数据库文件
- `scada.db-wal`: Write-Ahead Log文件
- `scada.db-shm`: 共享内存文件

### WAL检查点
```python
from 存储层.database import Database

db = Database()
db.wal_checkpoint()
print("WAL checkpoint完成")
```

### 手动WAL恢复
```bash
# 复制WAL文件
copy backups\scada.db-wal data\scada.db-wal

# 执行checkpoint
python -c "from 存储层.database import Database; Database().wal_checkpoint()"
```

---

## 7. 灾难恢复

### 场景1: 数据库损坏

#### 检查数据库完整性
```bash
sqlite3 data/scada.db "PRAGMA integrity_check;"
```

#### 修复数据库
```bash
# 导出数据
sqlite3 data/scada.db ".dump" > dump.sql

# 重建数据库
sqlite3 data/scada_new.db < dump.sql

# 替换文件
copy data\scada_new.db data\scada.db
```

### 场景2: 配置文件丢失

#### 恢复默认配置
```bash
# 从模板恢复
copy 配置\*.yaml.example 配置\*.yaml

# 或从备份恢复
xcopy /E /Y backups\配置\* 配置\
```

### 场景3: 完全重建

#### 步骤
1. 安装系统依赖
2. 恢复数据库备份
3. 恢复配置文件
4. 恢复证书文件
5. 启动服务
6. 验证功能

---

## 8. 备份验证

### 定期验证
```bash
# 每月验证备份完整性
sqlite3 backups/scada_20260601.db "PRAGMA integrity_check;"
```

### 恢复测试
```bash
# 在测试环境恢复备份
copy backups\scada_20260601.db test_data\scada.db
python run.py --test
```

---

## 9. 备份存储

### 本地存储
```
backups/
├── 2026-06-01/
│   ├── scada.db
│   └── 配置/
├── 2026-06-02/
│   ├── scada.db
│   └── 配置/
└── ...
```

### 远程存储
```bash
# 上传到云存储
aws s3 cp backups/scada_20260603.zip s3://smartscada-backups/

# 或使用SCP
scp backups/scada_20260603.zip user@backup-server:/backups/
```

### 加密备份
```bash
# 使用GPG加密
gpg --encrypt --recipient user@example.com backups/scada_20260603.db

# 解密
gpg --decrypt backups/scada_20260603.db.gpg > data/scada.db
```

---

## 10. 监控备份状态

### 备份日志
```bash
# 检查备份日志
type logs\backup.log
```

### 备份告警
```yaml
# Prometheus告警规则
groups:
  - name: backup_alerts
    rules:
      - alert: BackupFailed
        expr: time() - scada_last_backup_timestamp > 86400 * 2
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "备份超过2天未执行"
```

---

## 11. 最佳实践

### 备份原则
1. **3-2-1规则**: 3份备份，2种介质，1份异地
2. **定期验证**: 每月测试备份恢复
3. **加密存储**: 敏感数据加密备份
4. **文档记录**: 记录备份和恢复流程

### 注意事项
1. 备份前停止写入操作
2. 验证备份文件完整性
3. 保留足够的备份版本
4. 定期清理过期备份
5. 记录备份和恢复日志

---

## 12. 常见问题

### Q: 备份文件太大？
A: 启用数据归档，删除过期历史数据

### Q: 恢复后数据不完整？
A: 检查WAL文件是否一起恢复

### Q: 备份过程中服务卡住？
A: 使用在线备份，避免锁表

### Q: 如何恢复误删的数据？
A: 从最近的备份恢复，或使用SQLite恢复工具
