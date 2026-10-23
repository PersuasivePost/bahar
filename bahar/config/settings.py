from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    waqi_token: str = ""
    nominatim_email: str = ""
    openaq_api_key: str = ""
    model_repo: str = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
    model_file: str = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    cache_dir: str = ".cache"
    public_base_url: str = "http://localhost:7860"
    osm_dir: str = "~/osm"  # OSM_DIR: *.osm.pbf extracts read by scripts/build_osm_index.py
    osm_index_path: str = "data/osm_index.sqlite"
    n_ctx: int = 4096
    n_threads: int | None = None  # N_THREADS; default = affinity/cgroup-derived
    rate_limit_per_min: int = 10  # /ask requests per client IP per minute (public tunnel URL)


@lru_cache
def get_settings() -> Settings:
    return Settings()
