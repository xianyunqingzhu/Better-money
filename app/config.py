"""配置管理：所有用户设置存于 data/config.json（本地，不外传）。"""
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "better_money.db"
IMAGES_DIR = DATA_DIR / "images"
BACKUPS_DIR = DATA_DIR / "backups"

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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    merged = {**DEFAULTS, **cfg}
    return merged


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
