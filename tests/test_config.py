from pathlib import Path

import yaml  # type: ignore[import-untyped]

from modules.config import load_config


def write_yaml(path: Path, payload):
    """테스트용 YAML 저장 헬퍼."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(payload, file, allow_unicode=True)


def test_load_default_config(tmp_path: Path):
    """default.yaml만 있을 때 기본 설정을 읽는지 검증한다."""
    config_dir = tmp_path / "config"
    write_yaml(
        config_dir / "default.yaml",
        {
            "logging": {"level": "INFO", "format": "text"},
            "publisher": {"headless": True},
            "pipeline": {"max_llm_calls_per_job": 15},
            "retry": {"max_retries": 3},
        },
    )

    config = load_config(str(config_dir))
    assert config.logging.level == "INFO"
    assert config.publisher.headless is True
    assert config.pipeline.max_llm_calls_per_job == 15
    assert config.retry.max_retries == 3


def test_local_config_overrides_default(tmp_path: Path):
    """local.yaml이 default.yaml보다 우선하는지 검증한다."""
    config_dir = tmp_path / "config"
    write_yaml(config_dir / "default.yaml", {"logging": {"level": "INFO"}})
    write_yaml(config_dir / "local.yaml", {"logging": {"level": "DEBUG"}})

    config = load_config(str(config_dir))
    assert config.logging.level == "DEBUG"


def test_env_overrides_local_and_default(tmp_path: Path, monkeypatch):
    """환경변수가 local/default보다 우선하는지 검증한다."""
    config_dir = tmp_path / "config"
    write_yaml(config_dir / "default.yaml", {"logging": {"level": "INFO"}})
    write_yaml(config_dir / "local.yaml", {"logging": {"level": "WARNING"}})

    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("PUBLISHER_HEADLESS", "false")
    monkeypatch.setenv("RETRY_BACKOFF_BASE_SEC", "3.5")

    config = load_config(str(config_dir))
    assert config.logging.level == "ERROR"
    assert config.publisher.headless is False
    assert config.retry.backoff_base_sec == 3.5
