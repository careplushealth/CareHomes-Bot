import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from src.config import AppConfig
from src.db import DatabaseManager

logger = logging.getLogger(__name__)


class BaseStage(ABC):
    def __init__(self, config: AppConfig, db: DatabaseManager, stage_name: str):
        self.config = config
        self.db = db
        self.stage_name = stage_name

    def can_process_today(self, custom_cap: Optional[int] = None) -> bool:
        current_count = self.db.get_daily_count(self.stage_name)
        cap = custom_cap if custom_cap is not None else self.config.pipeline.daily_processing_cap
        if cap > 0 and current_count >= cap:
            logger.warning(
                f"[{self.stage_name}] Daily processing cap reached ({current_count}/{cap}). "
                f"Pipeline paused for today."
            )
            return False
        return True

    def increment_daily_progress(self) -> int:
        return self.db.increment_daily_count(self.stage_name)

    @abstractmethod
    def run(self, max_items: Optional[int] = None) -> Dict[str, Any]:
        """Execute stage processing. Must return summary dictionary."""
        pass
