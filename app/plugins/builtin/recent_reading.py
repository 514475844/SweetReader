"""内置插件：最近阅读（需求 ，优先级 20）。

在首页「扩展功能」面板露出「最近阅读」入口，前端拉 /api/recent-books 渲染最近读过的书。
"""
from app.plugins import Plugin, PluginEntry

PLUGIN = Plugin(
    id='recent_reading',
    name='最近阅读',
    description='在首页展示你最近读过的书，点击即可继续阅读。',
    version='1.0.0',
    author='',
    homepage=[PluginEntry(label='最近阅读', widget='recent', order=10)],
    enabled_by_default=True,
)
