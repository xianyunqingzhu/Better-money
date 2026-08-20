"""配置管理：所有用户设置存于 data/config.json（本地，不外传）。"""
import json

from app.paths import get_paths

DEFAULTS = {
    # AI 层（可切换适配器：OpenAI / DeepSeek / Qwen，都走 OpenAI 兼容接口）
    "api_base": "https://api.openai.com/v1",
    "api_key": "",
    "model_text": "gpt-4o-mini",
    "model_vision": "gpt-4o",
    "model_image": "gpt-image-1",
    # 业务参数
    "initial_balance": 0.0,
    "monthly_budget": 1500.0,
    "auto_save_ratio": 0.3,   # 每笔收入自动存进第一优先级目标的比例
    "tone": "朋友",            # 朋友 / 毒舌 / 温柔 / 老师
    "cooldown_days": 7,        # 愿望清单冷静期
    "image_gen_enabled": False,  # 总结配图开关（默认关，省钱）
}


def load_config() -> dict:
    paths = get_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if paths.config_path.exists():
        try:
            cfg = json.loads(paths.config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    merged = {**DEFAULTS, **cfg}
    return merged


def save_config(cfg: dict) -> None:
    paths = get_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
