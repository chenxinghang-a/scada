/**
 * 界面语言切换（前端侧）。
 *
 * 设计约束（都是踩过的坑）：
 *
 * 1. **没有词条的元素保持原文，不置空**。
 *    3000 处文案不可能一次翻完。缺失时保留中文原文，比显示 `undefined`
 *    或空白好得多 —— 现场至少还能用。
 *
 * 2. **词条缺失不算错误**，`t()` 返回空串由调用方决定回退。
 *
 * 3. **语言存在 localStorage**，刷新/跳页都不丢；同时写回后端配置
 *    （`ui.language`），这样换浏览器打开也是同一种语言。
 *
 * 4. **不翻译「数据」**：设备名、点位名、报警内容都是现场填的，翻了反而
 *    对不上工单。只翻界面骨架（导航/按钮/表头/提示）。
 */
(function () {
  'use strict';

  var LS_KEY = 'scada.ui.lang';
  var FALLBACK = 'zh-CN';

  var messages = {};
  var current = null;

  function stored() {
    try { return localStorage.getItem(LS_KEY); } catch (e) { return null; }
  }

  function store(lang) {
    try { localStorage.setItem(LS_KEY, lang); } catch (e) { /* 隐私模式忽略 */ }
  }

  /** 替换带 data-i18n 的元素文本；带子元素（如图标）时只替换文本节点 */
  function applyToDom() {
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      var key = el.getAttribute('data-i18n');
      var text = messages[key];
      if (!text) return;              // 没词条 → 保留原文
      // 元素里有图标时，只改最后一个文本节点，别把 <i> 冲掉
      var hasElementChild = el.children.length > 0;
      if (hasElementChild) {
        var last = null;
        for (var i = el.childNodes.length - 1; i >= 0; i--) {
          if (el.childNodes[i].nodeType === 3) { last = el.childNodes[i]; break; }
        }
        if (last) { last.nodeValue = ' ' + text; return; }
      }
      el.textContent = text;
    });

    document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
      var t = messages[el.getAttribute('data-i18n-placeholder')];
      if (t) el.setAttribute('placeholder', t);
    });

    document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
      var t = messages[el.getAttribute('data-i18n-title')];
      if (t) el.setAttribute('title', t);
    });
  }

  /** 切换语言并落盘 */
  function apply(lang) {
    return fetch('/api/i18n/' + encodeURIComponent(lang || ''), {
      headers: { 'Accept': 'application/json' }
    })
      // BUG-100：不校验 HTTP 状态就直接 `r.json()` —— 404/500 时后端返回
      // HTML 错误页，JSON.parse 抛错被 catch 成"切换失败"，真正原因看不到。
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        messages = data.messages || {};
        current = data.language || FALLBACK;
        store(current);
        document.documentElement.setAttribute('lang', current);
        applyToDom();
        document.querySelectorAll('[data-i18n-select]').forEach(function (sel) {
          sel.value = current;
        });
        // 通知页面（图表/表格里的动态文案可以自己监听）
        document.dispatchEvent(new CustomEvent('scada:language-changed', {
          detail: { language: current, messages: messages }
        }));
        // BUG-100：写回后端部署默认值，换浏览器打开也是同一种语言。
        // 未登录（登录页）会 401，属预期 —— 本地切换已生效，忽略即可。
        var token = window.localStorage.getItem('auth_token');
        fetch('/api/i18n', {
          method: 'PUT',
          headers: Object.assign(
            { 'Content-Type': 'application/json' },
            token ? { 'Authorization': 'Bearer ' + token } : {}
          ),
          body: JSON.stringify({ language: current })
        }).catch(function () { /* 写回失败不影响本地切换 */ });
        return current;
      })
      .catch(function (e) {
        console.warn('[i18n] 切换失败，保留当前语言', e);
        return current;
      });
  }

  function t(key, fallback) {
    var v = messages[key];
    return (v === undefined || v === '') ? (fallback === undefined ? '' : fallback) : v;
  }

  window.scadaI18n = {
    apply: apply,
    t: t,
    current: function () { return current; },
    messages: function () { return messages; }
  };

  document.addEventListener('DOMContentLoaded', function () {
    var initial = stored() || document.documentElement.getAttribute('lang') || FALLBACK;
    apply(initial);
  });
})();
