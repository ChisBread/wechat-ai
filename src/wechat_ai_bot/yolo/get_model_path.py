import sys
from pathlib import Path


def get_model_path(model_name):
    """
    获取 yolo 模型文件的绝对路径，兼容源码和打包环境。
    """
    if getattr(sys, "frozen", False):
        # 打包后
        base_dir = Path(sys.executable).parent
        model_path = base_dir / "wechat_ai_bot" / "yolo" / "models" / model_name
    else:
        # 源码
        base_dir = Path(__file__).resolve().parent
        model_path = base_dir / "models" / model_name
    return str(model_path)
