"""Environment-based configuration. No secrets are hard-coded."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    env: str            # "demo" or "production"
    db_path: str
    session_hours: int
    max_failed_logins: int
    lockout_minutes: int
    cookie_secure: bool
    demo_password: str
    display_tz: str
    secret_key: str = ""
    upload_dir: str = "data/uploads"
    max_upload_mb: int = 10
    currency: str = "PKR"
    assistant_name: str = "Jarvis"
    transcript_retention_days: int = 30
    # AI / voice providers (all optional; empty provider = feature runs in its offline fallback)
    ai_provider: str = ""        # "", "anthropic", "openai" (any OpenAI-compatible chat API)
    ai_api_key: str = ""
    ai_model: str = ""
    ai_base_url: str = ""
    ai_max_per_hour: int = 60     # per user; protects the API budget of a shared demo. Over the cap the rule engine answers (and says so).
    stt_provider: str = ""       # "", "openai" (OpenAI-compatible /audio/transcriptions)
    stt_api_key: str = ""
    stt_model: str = ""
    stt_base_url: str = ""
    tts_provider: str = ""       # "", "openai" (OpenAI-compatible /audio/speech)
    tts_api_key: str = ""
    tts_model: str = ""
    tts_voice: str = ""
    tts_base_url: str = ""
    # Public demo (WW_PUBLIC_DEMO=1): a demo reachable from the internet. Requires private passwords, secure cookies, noindex, login throttling.
    public_demo: bool = False
    demo_admin_password: str = ""    # separate password for the 'admin' demo account (never shared with the client)
    demo_admin_remote: bool = False   # public demo: allow the admin account to sign in through the public link (default: only directly on the host)
    demo_seed: int = 20261028        # random seed for the showcase data set


def _g(k, d=""):
    return os.environ.get(k, d)


def load() -> Config:
    env = os.environ.get("WW_ENV", "demo")
    return Config(
        secret_key=_g("WW_SECRET_KEY"), upload_dir=_g("WW_UPLOAD_DIR", "data/uploads"),
        max_upload_mb=int(_g("WW_MAX_UPLOAD_MB", "10")), currency=_g("WW_CURRENCY", "PKR"),
        assistant_name=_g("WW_ASSISTANT_NAME", "Jarvis"),
        transcript_retention_days=int(_g("WW_TRANSCRIPT_RETENTION_DAYS", "30")),
        ai_provider=_g("WW_AI_PROVIDER"), ai_api_key=_g("WW_AI_API_KEY"), ai_model=_g("WW_AI_MODEL"),
        ai_base_url=_g("WW_AI_BASE_URL"),
        ai_max_per_hour=int(_g("WW_AI_MAX_PER_HOUR", "60")),
        stt_provider=_g("WW_STT_PROVIDER"), stt_api_key=_g("WW_STT_API_KEY"), stt_model=_g("WW_STT_MODEL", "whisper-1"),
        stt_base_url=_g("WW_STT_BASE_URL"),
        tts_provider=_g("WW_TTS_PROVIDER"), tts_api_key=_g("WW_TTS_API_KEY"), tts_model=_g("WW_TTS_MODEL", "tts-1"),
        tts_voice=_g("WW_TTS_VOICE", "alloy"), tts_base_url=_g("WW_TTS_BASE_URL"),
        env=env,
        db_path=os.environ.get("WW_DB_PATH", "data/wwtextile.db"),
        session_hours=int(os.environ.get("WW_SESSION_HOURS", "12")),
        max_failed_logins=int(os.environ.get("WW_MAX_FAILED_LOGINS", "5")),
        lockout_minutes=int(os.environ.get("WW_LOCKOUT_MINUTES", "10")),
        cookie_secure=os.environ.get("WW_COOKIE_SECURE", "1" if (env == "production" or os.environ.get("WW_PUBLIC_DEMO") == "1") else "0") == "1",
        demo_password=os.environ.get("WW_DEMO_PASSWORD", "demo-only-change-me"),
        display_tz=os.environ.get("WW_DISPLAY_TZ", "Asia/Karachi"),
        public_demo=os.environ.get("WW_PUBLIC_DEMO", "0") == "1",
        demo_admin_password=os.environ.get("WW_DEMO_ADMIN_PASSWORD", ""),
        demo_admin_remote=os.environ.get("WW_DEMO_ADMIN_REMOTE", "0") == "1",
        demo_seed=int(os.environ.get("WW_DEMO_SEED", "20261028")),
    )


def validate_for_start(cfg: Config) -> list[str]:
    """Return problems that must stop a production start. Demo mode is permissive by design."""
    problems = []
    if cfg.env == "production":
        if len(cfg.secret_key) < 32:
            problems.append("WW_SECRET_KEY must be set to a random string of at least 32 characters.")
        if not cfg.cookie_secure:
            problems.append("WW_COOKIE_SECURE=0 is not allowed in production (serve over HTTPS).")
    elif cfg.env != "demo":
        problems.append(f"WW_ENV must be 'demo' or 'production', not '{cfg.env}'.")
    if cfg.public_demo:
        if cfg.env != "demo":
            problems.append("WW_PUBLIC_DEMO=1 is only for WW_ENV=demo.")
        if len(cfg.secret_key) < 32:
            problems.append("Public demo: WW_SECRET_KEY (32+ characters) is required.")
        if not cfg.cookie_secure:
            problems.append("Public demo: WW_COOKIE_SECURE must be 1 (the public link must be HTTPS).")
        if cfg.demo_password == "demo-only-change-me" or len(cfg.demo_password) < 10:
            problems.append("Public demo: set WW_DEMO_PASSWORD to a private value of at least 10 characters (the default is published in the README).")
        if len(cfg.demo_admin_password) < 12 or cfg.demo_admin_password == cfg.demo_password:
            problems.append("Public demo: set WW_DEMO_ADMIN_PASSWORD (12+ characters, different from WW_DEMO_PASSWORD); the admin account is not for the client.")
    return problems
