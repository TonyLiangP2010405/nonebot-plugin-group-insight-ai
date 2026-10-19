"""命令层：导入即注册全部命令 matcher。

- :mod:`~nonebot_plugin_group_insight.commands.report`：``#群聊报告`` / ``#强制生成报告``
- :mod:`~nonebot_plugin_group_insight.commands.wordcloud`：``#词云`` / ``#个人词云``
- :mod:`~nonebot_plugin_group_insight.commands.at_me`：``谁艾特我``
- :mod:`~nonebot_plugin_group_insight.commands.admin`：``#清除艾特数据`` / ``#清除全部艾特数据``
"""

from . import admin, at_me, report, wordcloud

__all__ = ["admin", "at_me", "report", "wordcloud"]
