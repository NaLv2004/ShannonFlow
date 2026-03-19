### 6. `review.py` (AI 审稿阶段)
# review.py
import os
import json
import chainlit as cl
from cli_async_basic import AgentSystem, BaseContextBuilder, Tool, StandardTools
from utils import PDFReader

from prompts import REVIEW_PROMPT, PDF_COMMENTATOR_PROMPT

class ReviewContextBuilder(BaseContextBuilder):
    def build_context(self, system, request_text, active_tasks_info, finished_tasks_info, workspace_tree, hardware_status):
        context = f"【当前工作目录与论文源码】\n{workspace_tree}\n\n"
        
        # 附加 PDFReader 初审结果
        temp_pdf = os.path.join(system.workspace_dir, "log", "temp_pdf_review.txt")
        if os.path.exists(temp_pdf):
            with open(temp_pdf, "r", encoding="utf-8") as f:
                context += f"【PDF初审意见】\n{f.read()}\n\n"

        context += "请使用 READ_FILE 深入核对 Python 代码与 Tex 描述的一致性。核对完毕后调用 FINISH_REVIEW 给出你的最终意见。"
        return context

class ReviewSystem(AgentSystem):
    def setup_default_tools(self):
        super().setup_default_tools()
        self.tool_registry.register(Tool("FINISH_REVIEW", "完成审稿", self.finish_review_tool))
        
    async def finish_review_tool(self, system, params, resp):
        review_content = params.get("review_content", "Review completed.")
        review_path = os.path.join(system.workspace_dir, "review.txt")
        with open(review_path, "w", encoding="utf-8") as f:
            f.write("=== Comprehensive Review ===\n\n" + review_content)
        await cl.Message(content=f"👨‍⚖️ 审稿意见已保存至 `review.txt`").send()
        system.stop_workflow = True
        return "Review finished."

async def run_review_workflow(workspace_dir, settings):
    log_dir = os.path.join(workspace_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

    pdf_path = os.path.join(workspace_dir, "paper", "main.pdf")
    temp_pdf_review = os.path.join(log_dir, "temp_pdf_review.txt")
    
    # 阶段 1: 异步调用 PDFReader 初审
    async with cl.Step(name="📄 PDF 初审") as step:
        if os.path.exists(pdf_path):
            pdf_reader = PDFReader(
                api_key=os.environ.get("GEMINI_API_KEY", ""),
                system_prompt="你是一个严苛的学术审稿专家。",
                model=settings.get("orchestrator_model", "gemini-3-flash-preview")
            )
            await cl.make_async(pdf_reader.read_pdf)(pdf_path, temp_pdf_review, PDF_COMMENTATOR_PROMPT)
            step.output = "初审完成。"
        else:
            step.output = "未找到 PDF，跳过初审。"

    # 阶段 2: 深度代码核查
    system = ReviewSystem(workspace_dir, settings, cl.user_session.get("interrupt_event"), cl.user_session.get("user_interrupt_requests"))
    system.context_builder = ReviewContextBuilder()
    
    system.prompt_file = os.path.join(log_dir, "temp_review_prompt.txt")
    with open(system.prompt_file, "w", encoding="utf-8") as f: f.write(REVIEW_PROMPT)

    await system.execute_workflow()