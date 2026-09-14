from datetime import UTC, datetime

from strmflow.core.config import Settings
from strmflow.infrastructure.database import Database
from strmflow.repositories.media import MediaRepository
from strmflow.repositories.transfers import TransferJobRepository
from strmflow.schemas.api import TransferJob


async def test_media_repository_round_trip(tmp_path) -> None:
    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db"))
    await database.initialize()
    repository = MediaRepository(database.sessions)
    now = datetime.now(UTC).isoformat()
    item = {
        "id": "m1",
        "name": "示例 (2026)",
        "sourcePath": "/source/示例",
        "generatedPath": "/strm/示例",
        "targetDir": "/emby/tv/示例 (2026)",
        "title": "示例",
        "year": "2026",
        "category": "国产剧",
        "mediaType": "tv",
        "status": "ongoing",
        "totalEpisodes": "12",
        "syncedFiles": ["S01E01.strm"],
        "createdAt": now,
        "updatedAt": now,
    }
    saved = await repository.upsert(item)
    assert saved["totalEpisodes"] == "12"
    assert saved["season"] == 1
    assert (await repository.get("m1"))["syncedFiles"] == ["S01E01.strm"]
    assert (await repository.get_by_source_path("/source/示例"))["id"] == "m1"
    assert await repository.get_by_source_path("/source/不存在") is None
    assert len(await repository.list()) == 1
    await repository.delete("m1")
    assert await repository.count() == 0
    await database.close()


async def test_transfer_jobs_are_persistent(tmp_path) -> None:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db")
    database = Database(settings)
    await database.initialize()
    repository = TransferJobRepository(database.sessions, retention=20)
    job = TransferJob(
        id="job1",
        provider="bdpan",
        status="queued",
        destination="/目标",
        command=["bdpan", "transfer"],
        created_at=datetime.now(UTC),
    )
    await repository.create(job, {"mediaItemId": "m1"})
    await repository.update("job1", status="succeeded", return_code=0)
    loaded = await repository.get("job1")
    assert loaded.status == "succeeded"
    assert loaded.return_code == 0
    await database.close()
