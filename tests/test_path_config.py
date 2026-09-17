from strmflow.core.config import Settings
from strmflow.infrastructure.database import Database
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.services.path_config import PathConfigService


def test_default_path_config_uses_standard_openlist_roots() -> None:
    settings = Settings(_env_file=None)
    assert settings.list_root == "/temp_strm"
    assert settings.emby_strm_root == "/local_media/emby-strm"
    assert settings.bdpan_save_root == "video"


async def test_runtime_path_config_is_persisted(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db")
    database = Database(settings)
    await database.initialize()
    repository = RuntimeSettingsRepository(database.sessions)
    service = PathConfigService(settings, repository)
    await service.initialize()
    await service.update({"listRoot": "/strm/tv", "embyStrmRoot": "/emby/strm"})

    restored = PathConfigService(settings, repository)
    await restored.initialize()
    assert restored.as_dict() == {
        "listRoot": "/strm/tv",
        "embyStrmRoot": "/emby/strm",
    }
    await database.close()
