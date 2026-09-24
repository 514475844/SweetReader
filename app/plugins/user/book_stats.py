"""示例用户插件：书库统计。

演示插件框架的全部能力：
- 自定义 Blueprint 页面（/plugin/stats），带独立模板（templates/stats.html）。
- 首页「扩展功能」面板入口（点击跳转统计页）。
- 管理员侧栏入口（admin_nav）。
- 阅读页工具栏按钮（reader_tools）。
- 图形化设置（settings_schema）：用户在「插件管理」页即可配置，无需写任何前端。

把本文件复制改一改，就是你的第一个插件。设置读取用 registry.get_settings('book_stats')。
"""
from flask import Blueprint, render_template
from flask_login import login_required
from app.models import Book, Category, db
from app.plugins import Plugin, PluginEntry, registry

bp = Blueprint('book_stats', __name__, template_folder='templates')


@bp.route('/plugin/stats')
@login_required
def stats_page():
    # 读取本插件设置（合并默认值后的 dict）
    cfg = registry.get_settings('book_stats')
    top_n = int(cfg.get('topN', 8))
    show_format = bool(cfg.get('show_format', True))
    show_recent = bool(cfg.get('show_recent', True))

    total = Book.query.count()
    by_format = []
    if show_format:
        by_format = (db.session.query(Book.file_type, db.func.count(Book.id))
                     .group_by(Book.file_type)
                     .order_by(db.func.count(Book.id).desc()).all())
    top_cats = (Category.query.filter(Category.book_count > 0)
                .order_by(Category.book_count.desc()).limit(top_n).all())
    recent = []
    if show_recent:
        recent = Book.query.order_by(Book.id.desc()).limit(10).all()
    return render_template('stats.html',
                           total=total,
                           by_format=[(t or '未知', c) for t, c in by_format],
                           top_cats=top_cats,
                           recent=recent,
                           show_format=show_format,
                           show_recent=show_recent,
                           top_n=top_n)


PLUGIN = Plugin(
    id='book_stats',
    name='书库统计',
    description='查看书库总量、格式分布、热门分类与最近入库。',
    version='1.1.0',
    author='',
    homepage=[PluginEntry(label='书库统计', url='/plugin/stats', order=50, show_on_homepage=False)],
    admin_nav=[{'label': '书库统计', 'url': '/plugin/stats'}],
    # 阅读页工具栏不再注入「统计」按钮：入口保留在插件页 / 管理导航里即可
    reader_tools=[],
    blueprint=bp,
    enabled_by_default=True,
    settings_schema=[
        {'key': 'topN', 'label': '热门分类数量', 'type': 'int', 'default': 8,
         'min': 1, 'max': 50, 'help': '统计页展示前 N 个分类'},
        {'key': 'show_format', 'label': '显示格式分布', 'type': 'bool', 'default': True,
         'help': '是否展示 TXT/EPUB/DOC 等格式占比'},
        {'key': 'show_recent', 'label': '显示最近入库', 'type': 'bool', 'default': True,
         'help': '是否展示最近添加的 10 本书'},
    ],
)
