### 4. `perform_experiments.py` (数据实验阶段)
# perform_experiments.py
import os
import json
import chainlit as cl
from cli_async_basic import AgentSystem, BaseContextBuilder, Tool, StandardTools

from prompts import EXPERIMENT_PROMPT, EXPERIMENT_PLAN_PROMPT

class ExperimentContextBuilder(BaseContextBuilder):
    def build_context(self, system, request_text, active_tasks_info, finished_tasks_info, workspace_tree, hardware_status):
        # 注入记录的数据历史
        data_history = "暂无数据。"
        data_path = os.path.join(system.workspace_dir, "data", "recorded_data.txt")
        if os.path.exists(data_path):
            with open(data_path, "r", encoding="utf-8") as f: data_history = f.read()

        context = f"【当前已保存的实验数据】\n{data_history}\n\n"
        context += f"【工作目录与硬件】\n{workspace_tree}\n{hardware_status}\n\n"
        context += f"【任务监控 (并发测试)】\n{active_tasks_info}\n{finished_tasks_info}\n\n"
        context += "如果本步骤完成，所有数据已经记录，调用 PASS_STEP 结束本步骤，进入下一步骤。"
        return context

class ExperimentSystem(AgentSystem):
    def setup_default_tools(self):
        super().setup_default_tools()
        self.tool_registry.register(Tool("PASS_STEP", "完成数据获取阶段", StandardTools.finish))

async def run_experiments(workspace_dir, user_request, settings):
    log_dir = os.path.join(workspace_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

    system = ExperimentSystem(workspace_dir, settings, cl.user_session.get("interrupt_event"), cl.user_session.get("user_interrupt_requests"), student_planner_prompt=EXPERIMENT_PLAN_PROMPT)
    system.adversarial_mode = False
    system.context_builder = ExperimentContextBuilder()
    
    # 强制覆盖保存路径到 data 目录
    system.data_record_txt = os.path.join(workspace_dir, "data", "recorded_data.txt")
    system.prompt_file = os.path.join(log_dir, "temp_exp_prompt.txt")
    with open(system.prompt_file, "w", encoding="utf-8") as f: f.write(EXPERIMENT_PROMPT)

    await system.execute_workflow()