
# update_from_reviews.py
import os
import json
import chainlit as cl
from cli_async_basic import AgentSystem, BaseContextBuilder, Tool, StandardTools, WorkspaceManager

from prompts import UPDATE_PROMPT

class UpdateContextBuilder(BaseContextBuilder):
    def build_context(self, system, request_text, active_tasks_info, finished_tasks_info, workspace_tree, hardware_status):
        review_text = "未找到审稿意见。"
        review_path = os.path.join(system.workspace_dir, "review.txt")
        if os.path.exists(review_path):
            with open(review_path, "r", encoding="utf-8") as f: review_text = f.read()

        context = f"【!!! 审稿人给出的意见 !!!】\n{review_text}\n\n"
        context += f"【工作目录与代码】\n{workspace_tree}\n\n"
        context += f"【硬件状态】\n{hardware_status}\n\n"
        context += f"【任务监控】\n{active_tasks_info}\n{finished_tasks_info}\n\n"
        
        # 注入历史摘要
        context += "【近期执行过的历史动作】\n"
        for h in system.action_history[-15:]:
            context += f"Action: {h.get('action')}, Params: {json.dumps(h.get('params',{}), ensure_ascii=False)}\nResult: {str(h.get('result', ''))}\n\n"
        
        context += f"【最近执行历史的概述】\n{system.summaries}\n\n"
        context += "请根据审稿意见，派发 Coder 修改代码，派发 RUN 获取新数据，或者直接修改 tex 论文。所有问题解决后调用 PASS_STEP。"
        return context

class UpdateSystem(AgentSystem):
    def setup_default_tools(self):
        super().setup_default_tools()
        self.tool_registry.register(Tool("PASS_STEP", "完成所有Rebuttal修改", StandardTools.finish))

async def run_update_from_reviews(workspace_dir, user_request, settings):
    log_dir = os.path.join(workspace_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

    system = UpdateSystem(workspace_dir, settings, cl.user_session.get("interrupt_event"), cl.user_session.get("user_interrupt_requests"))
    system.adversarial_mode = False
    system.context_builder = UpdateContextBuilder()
    
    system.prompt_file = os.path.join(log_dir, "temp_update_prompt.txt")
    with open(system.prompt_file, "w", encoding="utf-8") as f: f.write(UPDATE_PROMPT)

    await system.execute_workflow()