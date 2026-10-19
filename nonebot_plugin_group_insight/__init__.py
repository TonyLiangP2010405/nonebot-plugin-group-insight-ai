from nonebot import require
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="群聊洞察",
    description="AI 驱动的群聊分析插件：群聊报告、词云生成、@ 消息追踪（移植自 Yunzai 插件 group-insight）",
    usage=(
        "#群聊报告 [群号] [日期]（别名 #群聊总结）—— 查询 / 生成群聊报告，日期支持 今天|昨天|前天|YYYY-MM-DD\n"
        "#强制生成报告 [群号] [日期] —— 主人强制重新生成（忽略批次缓存）\n"
        "#词云 [当天|三天|七天]（别名 #群聊词云）—— 群聊词云\n"
        "#个人词云 [当天|三天|七天] —— 个人词云\n"
        "谁艾特我 / 谁@我 / 谁at我 —— 查看被 @ 的记录（合并转发）\n"
        "#清除艾特数据 —— 清除自己被 @ 的记录\n"
        "#清除全部艾特数据 —— 主人清除全部 @ 记录"
    ),
    type="application",
    homepage="https://github.com/TonyLiangP2010405/nonebot-plugin-group-insight",
    supported_adapters={"~onebot.v11"},
    config=Config,
)

# 定时任务依赖 nonebot-plugin-apscheduler，必须在导入 scheduler 模块之前声明
require("nonebot_plugin_apscheduler")

# 导入以注册群消息监听 / 命令 matcher / 定时任务（必须在 require 之后）
from . import collector, commands, scheduler  # noqa: E402,F401
from .analysis.report import register_threshold_trigger  # noqa: E402

register_threshold_trigger()  # 消息数达到 ai_max_messages 整数倍时后台补全批次缓存
