"""内置插件：随便看看（需求  随机推荐，优先级 25）。

在首页「扩展功能」面板露出「随便看看」入口，点击随机打开一本书。
"""
from app.plugins import Plugin, PluginEntry

PLUGIN = Plugin(
    id='random_reading',
    name='随便看看',
    description='一键打开一本随机的书，发现书库里的惊喜。',
    version='1.0.0',
    author='',
    homepage=[PluginEntry(label='随便看看', widget='random', order=20)],
    enabled_by_default=True,
)
