from dataclasses import dataclass
from route74.sources.yandex import config_validation as validation
from route74.sources.yandex.models import YandexSourceMode


@dataclass(frozen=True)
class YandexSourceConfig:
    enabled: bool = True
    mode: YandexSourceMode = YandexSourceMode.AUTO
    primary: bool = True
    cache_seconds: int = 10
    timeout_seconds: float = 8.0
    browser_min_interval_seconds: float = 1.0
    browser_cooldown_seconds: int = 20
    persistent_browser: bool = True
    snapshot_cache_max_age_seconds: int = 600
    debug: bool = False

    def __post_init__(self) -> None:
        validation.require_bool("enabled", self.enabled)
        validation.require_mode("mode", self.mode)
        validation.require_bool("primary", self.primary)
        validation.require_non_negative_int("cache_seconds", self.cache_seconds)
        validation.require_positive_float("timeout_seconds", self.timeout_seconds)
        validation.require_non_negative_float("browser_min_interval_seconds", self.browser_min_interval_seconds)
        validation.require_non_negative_int("browser_cooldown_seconds", self.browser_cooldown_seconds)
        validation.require_bool("persistent_browser", self.persistent_browser)
        validation.require_non_negative_int("snapshot_cache_max_age_seconds", self.snapshot_cache_max_age_seconds)
        validation.require_bool("debug", self.debug)
