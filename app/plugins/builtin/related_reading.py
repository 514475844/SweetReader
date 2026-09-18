"""内置插件：相关推荐（需求 ，优先级 15）。

首页「扩展功能」面板露出「相关推荐」入口，前端以用户最近阅读的书为种子调
/api/recommend 渲染同分类/同作者推荐。开关与阅读页共用用户设置 show_recommend。
"""
from app.plugins import Plugin, PluginEntry

PLUGIN = Plugin(
    id='related_reading',
    name='相关推荐',
    description='根据你最近读过的书，推荐同分类或同作者的书。',
    version='1.0.0',
    author='SweetReader',
    homepage=[PluginEntry(label='相关推荐', widget='related', order=20)],
    enabled_by_default=True,
)
