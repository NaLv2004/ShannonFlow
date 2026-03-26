import os
import json
import asyncio
from cli_async_basic import AgentSystem, BaseContextBuilder
from prompts import WRITEUP_PLAN_PROMPT, PAPER_WRITER_SYSTEM_PROMPT
from utils import compile_latex_project
import chainlit as cl

class WriteupContextBuilder(BaseContextBuilder):
    def build_context(self, system, request_text, active_tasks_info, finished_tasks_info, workspace_tree, hardware_status):
        context = f"【论文撰写核心请求/意见】\n{request_text}\n\n"
        context += f"【当前工作空间结构目录（请自主选择有用的历史数据或代码进行读取以协助成文）】\n{workspace_tree}\n\n"
        
        if system.plan_mode:
            if system.plan_index < len(system.plan):
                active_steps = system.plan[system.plan_index : system.plan_index + system.concurrent_plan_steps]
                context += f"【写作大纲执行进度 (Plan Mode)】\n整体进度: {system.plan_index}/{len(system.plan)}\n当前你需要主攻完成如下大纲章节:\n"
                for i, step in enumerate(active_steps):
                    context += f"{i+1}. {step}\n"
                context += "完成上述当前章节的撰写并使用 WRITE_FILE 存入 papers 目录后，必须调用 FINISH_STEP 工具推进计划。\n\n"
            else:
                context += "【当前执行计划 (Plan Mode)】\n所有的规划章节均已完成，请检查所有源文件无误后调用 FINISH 工具宣告结束。\n\n"

        context += f"【近期执行过的历史动作】\n"
        for h in system.action_history[-15:]:
            context += f"Action: {h.get('action')}, Params: {json.dumps(h.get('params',{}), ensure_ascii=False)}\nResult: {str(h.get('result', ''))}\n\n"
            
        context += f"【最近执行历史的概述】\n{system.summaries}\n\n"
        
        context += "提示：务必将所有的 .tex 和 .bib 输出至 `papers` 文件夹中，最后确保能够有 `papers/main.tex`。（使用IEEE trans模板）\n"
        context += "撰写后面的章节时，务必保证你的上下文中确实存在前面所有章节的具体内容\n"
        context += "请根据上述信息，返回你的 JSON 决策！"
        return context

class WriteupSystem(AgentSystem):
    def __init__(self, workspace_dir, settings, interrupt_event, user_interrupt_requests):
        # 覆写大纲生成提示词
        super().__init__(
            workspace_dir, 
            settings, 
            interrupt_event, 
            user_interrupt_requests,
            student_planner_prompt=WRITEUP_PLAN_PROMPT,
            # teacher_critic_prompt 可以保留为None或继续使用默认的
            teacher_critic_prompt=None 
        )
        self.context_builder = WriteupContextBuilder()
        os.makedirs(os.path.join(workspace_dir, "papers"), exist_ok=True)
        
    async def finish_task_and_compile(self):
        """覆盖完成时的动作，尝试一次编译"""
        papers_dir = os.path.join(self.workspace_dir, "papers")
        await cl.Message(content="📑 全部章节及参考文献撰写完毕，正在自动调用 `pdflatex` 编译...").send()
        
        # 考虑到 utils.py 中的 compile_latex_project 是一个同步且耗时的操作，使用 to_thread
        success = await asyncio.to_thread(compile_latex_project, papers_dir, "main.tex")
        
        if success:
            await cl.Message(content=f"✅ 编译成功！生成的 PDF 位于 `{papers_dir}`。可以随时查看！").send()
        else:
            await cl.Message(content=f"⚠️ 编译遇到了一些问题，可能由于环境未配置好或有语法错误，建议手动去 `{papers_dir}` 查看日志。").send()

async def perform_writeup_workflow(workspace_dir, settings, interrupt_event, user_interrupt_requests, request_text=""):
    """
    提供给外部（app.py 等）直接调用的工作流接口入口
    """
    system = WriteupSystem(
        workspace_dir=workspace_dir,
        settings=settings,
        interrupt_event=interrupt_event,
        user_interrupt_requests=user_interrupt_requests
    )
    
    # 强制让 WriteupSystem 进入 Plan Mode
    system.settings["plan_mode"] = True
    system.plan_mode = True
    system.adversarial_mode = False
    # 执行标准的工作流 (使用专属文章编写提示词)
    await system.execute_workflow(request_text, orchestrator_sys_prompt=PAPER_WRITER_SYSTEM_PROMPT)
    
    # 在 workflow 退出后（不论是因为完成还是被中断），或者在明确被 FINISH 时执行
    if system.stop_workflow: 
        await system.finish_task_and_compile()