"""SweetReader 插件框架（轻量、可禁用、可被用户自行扩展）。

设计目标（用户需求：插件功能尽早做 + 首页扩展功能优先级提高 + 用户要自己写插件用）：
- 内置插件放在 app/plugins/builtin/，每个模块定义 PLUGIN = Plugin(...) 即被自动发现。
- 随包发布的用户插件放在 app/plugins/user/（随代码同步部署）。
- 用户自己丢的插件放在 instance/plugins/（持久卷，重部署不丢，无需改代码即可用）。
- 插件可声明的能力：
    - homepage 入口：在首页「扩展功能」面板露出按钮/卡片（widget 键走内置渲染，url 走链接，
      html_url 走「拉取该端点返回的 HTML 片段」实时注入）。
    - blueprint：一个 Flask Blueprint，插件即可拥有自己的路由 + 模板 + 静态资源，独立成页。
    - reader_tools：在阅读页工具栏注入按钮（label + url）。
    - admin_nav：在管理员侧栏注入链接（label + url）。
    - settings_schema：声明一组设置字段（text/int/float/bool/select/textarea），框架在「插件管理」
      页自动渲染成图形化表单，用户改完即时保存，无需插件自己写 UI。
    - settings_blueprint：若通用表单不够用，可声明一个自定义设置页 Blueprint（完全自定义 UI）。
- 启用/停用/设置 存于 instance/plugins_state.json（持久卷，重启不丢）。
- 用户插件导入失败不会拖垮整个应用：会被记录并在管理页标红，其余插件照常工作。

插件上下文可用（在插件模块里直接 import）：
    from app.plugins import registry, get_plugin_settings
    from app.models import Book, Category, User, db
    from flask_login import current_user
读取自己的设置：get_plugin_settings('your_plugin_id')  ->  合并默认值后的 dict。
"""
import os
import json
import importlib
import importlib.util
import pkgutil
from dataclasses import dataclass, field, asdict
from typing import List, Any, Optional


@dataclass
class PluginEntry:
    label: str                 # 首页面板显示的标签
    widget: str = ''           # 可选：内置小组件键（recent / random / related ...）
    url: str = ''              # 可选：直接跳转链接
    html_url: str = ''         # 可选：返回 HTML 片段的端点，前端拉取后注入卡片
    order: int = 0             # 排序，越小越靠前
    show_on_homepage: bool = True  # 是否在首页「扩展功能」面板露出（False 则仅出现在管理/用户菜单）


@dataclass
class Plugin:
    id: str
    name: str
    description: str = ''
    version: str = '1.0.0'
    author: str = ''
    homepage: List[PluginEntry] = field(default_factory=list)
    blueprint: Any = None                  # 可选：Flask Blueprint（自定义页面/路由/模板）
    reader_tools: List[dict] = field(default_factory=list)   # 可选：[{label,url,icon?}]
    admin_nav: List[dict] = field(default_factory=list)      # 可选：[{label,url}]
    settings_schema: List[dict] = field(default_factory=list)  # 可选：图形化设置表单字段列表
    settings_blueprint: Any = None         # 可选：完全自定义的设置页 Blueprint
    enabled_by_default: bool = True
    source: str = 'builtin'     # builtin / user（由 loader 填写，模块无需声明）
    error: str = ''             # 非空表示导入失败原因
    removable: bool = False     # 用户自放插件（可在管理页删除）

    def to_dict(self):
        caps = {
            'homepage': bool(self.homepage),
            'blueprint': getattr(self, 'blueprint', None) is not None,
            'reader_tools': bool(self.reader_tools),
            'admin_nav': bool(self.admin_nav),
            'settings': bool(self.settings_schema),
            'custom_settings': getattr(self, 'settings_blueprint', None) is not None,
        }
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'version': self.version,
            'author': self.author,
            'homepage': [asdict(e) for e in self.homepage],
            'reader_tools': self.reader_tools,
            'admin_nav': self.admin_nav,
            'settings_schema': self.settings_schema,
            'has_custom_settings': caps['custom_settings'],
            'capabilities': caps,
            'enabled_by_default': self.enabled_by_default,
            'source': self.source,
            'error': self.error,
            'removable': self.removable,
            'enabled': None,  # 运行时填充
            'settings': {},    # 运行时填充
        }


class PluginRegistry:
    def __init__(self):
        self._plugins = {}
        self._loaded = False

    # ---------- 发现与加载 ----------
    def load(self):
        if self._loaded:
            return
        self._plugins = {}
        self._scan_package('app.plugins.builtin', 'builtin')
        self._scan_package('app.plugins.user', 'user')
        self._scan_instance_plugins()
        self._loaded = True

    def reload(self):
        """重新扫描（管理页「重新扫描」用）。"""
        self._loaded = False
        self._plugins = {}
        self.load()

    def _scan_package(self, pkg_name, source):
        try:
            pkg = importlib.import_module(pkg_name)
        except Exception as e:
            print('[plugins] 跳过包 %s: %s' % (pkg_name, e), flush=True)
            return
        for mod in pkgutil.iter_modules(pkg.__path__):
            self._import_plugin_module('%s.%s' % (pkg_name, mod.name), source)

    def _scan_instance_plugins(self):
        # 用环境变量取 instance 路径，避免依赖 app context（模块导入期未必有）
        inst = os.environ.get('INSTANCE_DIR', '/app/instance')
        d = os.path.join(inst, 'plugins')
        if not os.path.isdir(d):
            return
        for fn in sorted(os.listdir(d)):
            if fn.endswith('.py') and not fn.startswith('_'):
                path = os.path.join(d, fn)
                name = fn[:-3]
                self._import_plugin_file(path, 'sr_user_%s' % name, 'user')

    def _import_plugin_module(self, modname, source):
        try:
            m = importlib.import_module(modname)
            p = getattr(m, 'PLUGIN', None)
            if isinstance(p, Plugin):
                p.source = source
                self._plugins[p.id] = p
            else:
                print('[plugins] %s 未定义 PLUGIN，跳过' % modname, flush=True)
        except Exception as e:
            print('[plugins] 导入失败 %s: %s' % (modname, e), flush=True)

    def _import_plugin_file(self, path, modname, source):
        try:
            spec = importlib.util.spec_from_file_location(modname, path)
            if spec is None or spec.loader is None:
                print('[plugins] 无法加载 %s' % path, flush=True)
                return
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            p = getattr(m, 'PLUGIN', None)
            if isinstance(p, Plugin):
                p.source = source
                p.removable = True      # 来自用户插件目录，可由管理员删除
                self._plugins[p.id] = p
            else:
                print('[plugins] %s 未定义 PLUGIN，跳过' % path, flush=True)
        except Exception as e:
            msg = '%s: %s' % (type(e).__name__, e)
            print('[plugins] 用户插件导入失败 %s: %s' % (path, msg), flush=True)
            pid = 'broken_%s' % os.path.splitext(os.path.basename(path))[0]
            self._plugins[pid] = Plugin(
                id=pid, name=os.path.basename(path), description='导入失败，请检查代码',
                source=source, error=msg, enabled_by_default=False, removable=True)

    # ---------- 状态持久化 ----------
    def _state_path(self):
        from flask import current_app
        return os.path.join(current_app.instance_path, 'plugins_state.json')

    def _load_state(self):
        try:
            with open(self._state_path(), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, state):
        try:
            path = self._state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def is_enabled(self, pid):
        state = self._load_state()
        entry = state.get(pid)
        if isinstance(entry, dict):
            return bool(entry.get('enabled', True))
        if entry is not None:
            return bool(entry)  # 兼容旧版 bool 存储
        p = self._plugins.get(pid)
        return bool(p.enabled_by_default) if p else False

    def set_enabled(self, pid, enabled):
        if pid not in self._plugins:
            return False
        state = self._load_state()
        entry = state.get(pid)
        if not isinstance(entry, dict):
            entry = {'enabled': True, 'settings': {}}
        entry['enabled'] = bool(enabled)
        state[pid] = entry
        self._save_state(state)
        return True

    # ---------- 设置读写 ----------
    def get_settings(self, pid):
        """返回合并默认值后的设置 dict（缺省用 schema 里的 default）。"""
        p = self._plugins.get(pid)
        schema = getattr(p, 'settings_schema', None) or []
        defaults = {}
        for f in schema:
            if isinstance(f, dict) and f.get('key') is not None:
                defaults[f['key']] = f.get('default')
        state = self._load_state()
        entry = state.get(pid)
        stored = {}
        if isinstance(entry, dict):
            stored = entry.get('settings', {}) or {}
        merged = dict(defaults)
        for k, v in stored.items():
            merged[k] = v
        return merged

    def set_settings(self, pid, data):
        """保存设置（只保留 schema 声明的 key，避免脏数据；与默认值相同则丢弃以精简存储）。"""
        if pid not in self._plugins:
            return False
        p = self._plugins.get(pid)
        schema = getattr(p, 'settings_schema', None) or []
        allowed = {f.get('key') for f in schema if isinstance(f, dict)}
        if allowed:
            cleaned = {k: data.get(k) for k in allowed if k in data}
        else:
            cleaned = dict(data or {})
        # 去掉与默认值相同的项，存储更干净
        defaults = {k: f.get('default') for f in schema if isinstance(f, dict) for k in [f.get('key')] if k is not None}
        cleaned = {k: v for k, v in cleaned.items() if k not in defaults or v != defaults[k]}
        state = self._load_state()
        entry = state.get(pid)
        if not isinstance(entry, dict):
            entry = {'enabled': bool(entry) if entry is not None else True, 'settings': {}}
        entry['settings'] = cleaned
        state[pid] = entry
        self._save_state(state)
        return True

    # ---------- 查询 ----------
    def all(self):
        return list(self._plugins.values())

    def active(self):
        return [p for p in self._plugins.values() if self.is_enabled(p.id)]

    def blueprints(self):
        out = []
        for p in self._plugins.values():
            if self.is_enabled(p.id) and getattr(p, 'blueprint', None) is not None:
                out.append(p.blueprint)
        return out

    def settings_blueprints(self):
        out = []
        for p in self._plugins.values():
            if self.is_enabled(p.id) and getattr(p, 'settings_blueprint', None) is not None:
                out.append(p.settings_blueprint)
        return out

    def reader_tools_entries(self):
        out = []
        for p in self.active():
            for t in (p.reader_tools or []):
                out.append({
                    'plugin_id': p.id,
                    'label': t.get('label', ''),
                    'url': t.get('url', ''),
                    'icon': t.get('icon', ''),
                })
        return out

    def admin_nav_entries(self):
        out = []
        for p in self.active():
            for t in (p.admin_nav or []):
                out.append({
                    'plugin_id': p.id,
                    'label': t.get('label', ''),
                    'url': t.get('url', ''),
                })
        return out

    def homepage_entries(self):
        entries = []
        for p in self.active():
            for e in p.homepage:
                if not e.show_on_homepage:
                    continue
                entries.append({
                    'plugin_id': p.id,
                    'label': e.label,
                    'widget': e.widget,
                    'url': e.url,
                    'html_url': e.html_url,
                    'order': e.order,
                })
        entries.sort(key=lambda x: x['order'])
        return entries

    def list_for_admin(self):
        out = []
        for p in self._plugins.values():
            d = p.to_dict()
            d['enabled'] = self.is_enabled(p.id)
            d['settings'] = self.get_settings(p.id)
            out.append(d)
        return out


registry = PluginRegistry()
registry.load()


def get_plugin_settings(plugin_id):
    """模块级助手：插件在自己的路由里读取本插件设置。

    用法：from app.plugins import get_plugin_settings
          cfg = get_plugin_settings('my_plugin')
          limit = int(cfg.get('limit', 10))
    """
    return registry.get_settings(plugin_id)
