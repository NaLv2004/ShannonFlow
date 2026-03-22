### 4. `perform_experiments.py` (数据实验阶段)
# perform_experiments.py
import os
import json
import chainlit as cl
from cli_async_basic import AgentSystem, BaseContextBuilder, Tool, StandardTools

from prompts import EXPERIMENT_PROMPT, EXPERIMENT_PLAN_PROMPT

class ExperimentContextBuilder(BaseContextBuilder):
    def build_context(self, system, request_text, active_tasks_info, finished_tasks_info, workspace_tree, hardware_status):
        # # 注入记录的数据历史
        # data_history = "暂无数据。"
        data_path = os.path.join(system.workspace_dir, "data", "recorded_data.txt")
        if os.path.exists(data_path):
            with open(data_path, "r", encoding="utf-8") as f: data_history = f.read()

        context = f"【当前已保存的实验数据】\n{data_history}\n\n"
        # context += f"【工作目录与硬件】\n{workspace_tree}\n{hardware_status}\n\n"
        # context += f"【任务监控 (并发测试)】\n{active_tasks_info}\n{finished_tasks_info}\n\n"
        # context += "如果本步骤完成，所有数据已经记录，调用 PASS_STEP 结束本步骤，进入下一步骤。"
        # return context
          
        context = f"【用户的核心请求/意见】\n{request_text}\n\n"
        context += f"【工作目录结构】\n{workspace_tree}\n\n{hardware_status}\n"
        
        if system.plan_mode:
            if system.plan_index < len(system.plan):
                active_steps = system.plan[system.plan_index : system.plan_index + system.concurrent_plan_steps]
                context += f"【当前执行计划 (Plan Mode)】\n整体进度: {system.plan_index}/{len(system.plan)}\n当前你需要完成计划中的以下步骤:\n"
                for i, step in enumerate(active_steps):
                    context += f"{i+1}. {step}\n"
                context += "完成上述所有当前步骤后，必须调用 PASS_STEP 工具推进计划。\n\n"
            else:
                context += "【当前执行计划 (Plan Mode)】\n所有计划步骤均已完成，你必须并调用 FINISH 工具结束任务。\n\n"

        context += f"【当前运行中的任务监控 (最大并发:{system.task_manager.max_concurrent})】\n{active_tasks_info}\n\n"
        if finished_tasks_info: context += f"【刚刚结束的任务】\n{finished_tasks_info}\n\n"
            
        context += "【近期执行过的历史动作】\n"
        for h in system.action_history[-15:]:
            context += f"Action: {h.get('action')}, Params: {json.dumps(h.get('params',{}), ensure_ascii=False)}\nResult: {str(h.get('result', ''))[-10000:]}\n\n"
            
        data_path = os.path.join(system.workspace_dir, "data", "recorded_data.txt")
        if os.path.exists(data_path):
            with open(data_path, "r", encoding="utf-8") as f: data_history = f.read()

        context = f"【当前已保存的实验数据】\n{data_history}\n\n"
            
        context += f"【最近执行历史的概述】\n{system.summaries}\n\n请根据上述监控状态和请求，返回你的 JSON 决策。如果你需要等待时间收集日志输出，请选择 WAIT。"
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