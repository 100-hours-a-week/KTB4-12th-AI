from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "seonjalal-ai"
    app_env: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True

    # Database
    database_url: str = "postgresql://ai_user:ai_password@localhost:5432/ai_chat"

    # Internal Auth Token
    internal_service_token: str = "local-dev-service-token"

    # Main Backend
    main_backend_url: str = "http://localhost:8080"

    # External LLM Keys
    gemini_api_key: str = ""
    openai_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
