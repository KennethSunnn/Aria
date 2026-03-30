# ====================== 配置文件 config.py ======================
# 全链路使用单一模型；实际模型 ID 以环境变量 MODEL_NAME 为准（未设置时用下方默认值）。
DEFAULT_MODEL = "doubao-seed-2-0-lite-260215"

MODEL_POOL = {
    "llm": DEFAULT_MODEL,
}
