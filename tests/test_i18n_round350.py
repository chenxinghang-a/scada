"""界面多语言（round 350）。

## 为什么需要

用户指出：「选择语言安装不是最基础的吗？设置内也应该支持随时切换」。

检查下来两处都缺：

1. **安装包**：`[Languages]` 只有一条 → Inno **不出语言选择页**（两条以上才出）。
2. **应用**：模板 3000 处中文硬编码、无 `data-i18n`、无语言切换 UI、
   `/api/i18n` 不存在、配置里也没有 `language` 项。

安装包那边已在 `installer/scada.iss` 里补（英/中双语 + `[CustomMessages]`）。
这个文件守应用侧的四件事：

* 语言码归一化（`en` / `en-US` / `en_US` / `EN-US,en;q=0.9` 都认）
* 词条缺失时**回退原文**，不返回 `undefined` / 空串
* `/api/i18n/*` **免认证**（登录页就要显示正确语言）
* 中英词条 **key 集合一致**（漏翻一条就少一条，不能静默）

## 边界（别当成「全站已翻译」）

只翻「界面骨架」（导航/按钮/提示），**不翻数据**（设备名、点位名、报警内容）。
模板里 3000 处文案不可能一次迁完，缺失时保留中文原文 —— 这比显示空白安全。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import i18n  # noqa: E402

I18N_DIR = Path(__file__).resolve().parent.parent / '配置' / 'i18n'


class TestNormalize:
    """各种写法都要归一化到支持的语言。"""

    @pytest.mark.parametrize('raw,expect', [
        ('en', 'en-US'),
        ('en-US', 'en-US'),
        ('en_US', 'en-US'),
        ('EN-us', 'en-US'),
        ('en-US,en;q=0.9', 'en-US'),     # 浏览器实际发的格式
        ('zh', 'zh-CN'),
        ('zh-CN', 'zh-CN'),
        ('zh-Hans-CN', 'zh-CN'),
        (None, 'zh-CN'),
        ('', 'zh-CN'),
        ('klingon', 'zh-CN'),            # 认不出 → 默认
    ])
    def test_normalize(self, raw, expect):
        assert i18n.normalize(raw) == expect

    def test_supported_first_is_default(self):
        """`SUPPORTED` 第一个是兜底语言，词条最全。"""
        assert i18n.SUPPORTED[0] == i18n.DEFAULT_LANG


class TestTranslate:
    def test_known_key(self):
        assert i18n.translate('nav.dashboard', 'en-US') == 'Dashboard'
        assert i18n.translate('nav.dashboard', 'zh-CN') == '仪表盘'

    def test_missing_key_returns_default(self):
        """缺词条时返回调用方给的原文，**不是空串也不是 key**。"""
        got = i18n.translate('no.such.key', 'en-US', default='仪表盘')
        assert got == '仪表盘'

    def test_missing_key_without_default_returns_key(self):
        assert i18n.translate('no.such.key', 'en-US') == 'no.such.key'

    def test_fallback_to_default_language(self):
        """英文缺词条 → 回退中文（中文最全），不是留空。"""
        # 造一个只在中文里有的 key
        zh = i18n.load('zh-CN')
        missing_in_en = [k for k in zh if k not in i18n.load('en-US')]
        if not missing_in_en:
            pytest.skip('中英词条完全一致，无法验证回退')
        key = missing_in_en[0]
        assert i18n.translate(key, 'en-US') == zh[key]

    def test_bundle_shape(self):
        b = i18n.bundle('en-US')
        assert b['language'] == 'en-US'
        assert isinstance(b['messages'], dict) and b['messages']
        assert any(x['code'] == 'en-US' for x in b['supported'])
        assert all('label' in x for x in b['supported'])


class TestBundleFiles:
    """词条文件本身的完整性。"""

    def test_both_languages_exist(self):
        for lang in i18n.SUPPORTED:
            assert (I18N_DIR / f'{lang}.json').exists(), f'缺少 {lang}.json'

    def test_key_sets_match(self):
        """中英 key 集合必须一致 —— 漏翻一条不会报错，但界面会突然变中文。"""
        zh = set(i18n.load('zh-CN'))
        en = set(i18n.load('en-US'))
        only_zh = sorted(zh - en)
        only_en = sorted(en - zh)
        assert not only_zh and not only_en, (
            f'词条不对齐：仅中文 {only_zh}，仅英文 {only_en}')

    def test_no_empty_values(self):
        for lang in i18n.SUPPORTED:
            empty = [k for k, v in i18n.load(lang).items() if not v.strip()]
            assert not empty, f'{lang} 有空词条: {empty}'

    def test_nav_keys_cover_all_menu_items(self):
        """导航 8 项必须有词条 —— 这是「界面骨架」的核心，缺一条就中英混杂。"""
        zh = i18n.load('zh-CN')
        required = ['nav.dashboard', 'nav.devices', 'nav.control', 'nav.history',
                    'nav.alarms', 'nav.alarm_output', 'nav.industry40', 'nav.config']
        missing = [k for k in required if k not in zh]
        assert not missing, f'导航缺词条: {missing}'


class TestApi:
    """/api/i18n 端点。"""

    @pytest.fixture
    def client(self):
        from flask import Flask
        from 展示层.api.api_i18n import i18n_bp

        app = Flask(__name__)
        app.register_blueprint(i18n_bp)
        app.config['TESTING'] = True
        return app.test_client()

    def test_get_specific_language(self, client):
        r = client.get('/api/i18n/en-US')
        assert r.status_code == 200
        data = r.get_json()
        assert data['language'] == 'en-US'
        assert data['messages']['nav.dashboard'] == 'Dashboard'

    def test_get_auto_by_accept_language(self, client):
        # 蓝图里是 `route('/')` + `url_prefix='/api/i18n'` → 完整路径带尾斜杠；
        # 不带斜杠会 308，test_client 默认不跟随。
        r = client.get('/api/i18n/', headers={'Accept-Language': 'en-US,en;q=0.9'})
        assert r.get_json()['language'] == 'en-US'

    def test_unknown_language_falls_back(self, client):
        """语言写错不能 404 —— 前端会因此白屏。"""
        r = client.get('/api/i18n/klingon')
        assert r.status_code == 200
        assert r.get_json()['language'] == i18n.DEFAULT_LANG

    def test_no_auth_required(self, client):
        """登录页就要显示正确语言，所以这个接口**不能**要 token。"""
        r = client.get('/api/i18n/zh-CN')
        assert r.status_code == 200, '需要认证的话登录页永远只能显示默认语言'
