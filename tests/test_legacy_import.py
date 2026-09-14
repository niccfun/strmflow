import json

from strmflow.core.config import Settings
from strmflow.infrastructure.database import Database
from strmflow.repositories.media import MediaRepository
from strmflow.services.legacy_import import LegacyJsonImporter


class LegacyOpenList:
    async def read_text(self, _path: str) -> str:
        return json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "legacy1",
                        "name": "旧记录",
                        "sourcePath": "/source/旧记录",
                        "generatedPath": "/strm/旧记录",
                        "targetDir": "/emby/tv/旧记录",
                        "title": "旧记录",
                    }
                ],
            },
            ensure_ascii=False,
        )


async def test_legacy_json_is_imported_only_once(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db")
    database = Database(settings)
    await database.initialize()
    repository = MediaRepository(database.sessions)
    importer = LegacyJsonImporter(
        settings,
        database.sessions,
        repository,
        LegacyOpenList(),  # type: ignore[arg-type]
    )
    assert await importer.run_once() == 1
    assert await importer.run_once() == 0
    assert (await repository.get("legacy1"))["title"] == "旧记录"
    await database.close()
