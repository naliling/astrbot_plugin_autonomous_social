"""故意写错的 llm_tool 定义，供 lint 测试使用，不要安装到 AstrBot。"""

from astrbot.api.event import filter


class BadTool:
    @filter.llm_tool(name="bad_types")
    async def bad_types(self, event, count: int, flag: bool):
        '''类型写法不在 AstrBot 白名单内。

        Args:
            count(int): 数量
            flag(str): 开关
        '''
        return "x"

    @filter.llm_tool(name="no_args_block")
    async def no_args_block(self, event, keyword: str):
        '''缺少 Args 块。'''
        return "y"

    @filter.llm_tool(name="missing_decl")
    async def missing_decl(self, event, declared: str, undeclared: str):
        '''少声明了一个参数。

        Args:
            declared(string): 已声明
        '''
        return "z"

    @filter.llm_tool(name="no_doc")
    async def no_doc(self, event, q: str):
        return "w"
