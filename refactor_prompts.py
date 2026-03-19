import os
import re
import json

def refactor_prompts(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Prepend PromptBuilder class
    builder_code = """import os
import json

class PromptBuilder:
    def __init__(self, schemas_path="tools_schema.json"):
        base_dir = os.path.dirname(__file__)
        schema_file = os.path.join(base_dir, schemas_path)
        with open(schema_file, 'r', encoding='utf-8') as f:
            self.tools_schema = json.load(f)

    def build_prompt(self, base_prompt, tool_names):
        tools_desc = ""
        json_params = {}
        for i, tool in enumerate(tool_names):
            if tool in self.tools_schema:
                schema = self.tools_schema[tool]
                tools_desc += f"{i+1}. `{tool}`: {schema['description']}\\n"
                if schema['params']:
                    for k, v in schema['params'].items():
                        json_params[k] = f"如果是{tool}, {v}"
            else:
                tools_desc += f"{i+1}. `{tool}`: (未知工具)\\n"
        
        json_template = {
            "Thoughts": "你的思考过程...",
            "Action": " | ".join(tool_names),
            "Action_Params": json_params
        }
        
        # Build prompt
        res = base_prompt + "\\n\\n【你可以执行的操作（Tools）包括】：\\n" + tools_desc
        res += "\\n【交互格式】请严格按照以下JSON格式返回（必须包含在 ```json 和 ``` 之间）：\\n"
        res += "```json\\n" + json.dumps(json_template, ensure_ascii=False, indent=4) + "\\n```"
        return res

pb = PromptBuilder("tools_schema.json")
"""

    # We will manually replace the major prompts by extracting their base part string and calling pb.build_prompt()
    # It is hard to regex everything perfectly. Let's just create a new prompts.py file manually since we know its exact contents.
    pass

if __name__ == "__main__":
    refactor_prompts("prompts.py")
