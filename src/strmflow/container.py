from __future__ import annotations

from dataclasses import dataclass

import httpx

from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.infrastructure.database import Database
from strmflow.repositories import (
    MediaProbeRepository,
    MediaRepository,
    RuntimeSettingsRepository,
    TransferJobRepository,
)
from strmflow.services.bdpan import BdpanCli
from strmflow.services.bdpan_automation import BdpanAutomationService
from strmflow.services.emby import EmbyClient
from strmflow.services.emby302 import Emby302Gateway
from strmflow.services.episode_images import EpisodeImageService
from strmflow.services.legacy_import import LegacyJsonImporter
from strmflow.services.media import MediaService
from strmflow.services.media_probe import MediaProbeService
from strmflow.services.notifications import WecomWebhookService
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.services.storage import StorageService
from strmflow.services.system_status import SystemStatusService
from strmflow.services.telegram_tracker import TelegramTrackerService
from strmflow.services.transfers import BdpanTransferProvider, TransferManager


@dataclass(slots=True)
class ServiceContainer:
    database: Database
    openlist: OpenListClient
    storage: StorageService
    media: MediaService
    emby: EmbyClient
    transfers: TransferManager
    legacy_importer: LegacyJsonImporter
    path_config: PathConfigService
    emby302: Emby302Gateway
    media_probe: MediaProbeService
    bdpan: BdpanAutomationService
    telegram: TelegramTrackerService
    notifications: WecomWebhookService
    system_status: SystemStatusService


def build_container(
    settings: Settings,
    database: Database,
    openlist_http: httpx.AsyncClient,
    emby_http: httpx.AsyncClient,
    baidu_http: httpx.AsyncClient,
    notification_http: httpx.AsyncClient,
    runtime_logs: RuntimeLogStore,
) -> ServiceContainer:
    openlist = OpenListClient(settings, openlist_http)
    runtime_repository = RuntimeSettingsRepository(database.sessions)
    path_config = PathConfigService(settings, runtime_repository)
    storage = StorageService(settings, openlist, path_config)
    media_repository = MediaRepository(database.sessions)
    transfer_repository = TransferJobRepository(database.sessions, settings.transfer_job_retention)
    bdpan_cli = BdpanCli(settings, baidu_http)
    transfers = TransferManager(
        settings,
        [BdpanTransferProvider(settings, bdpan_cli)],
        transfer_repository,
        runtime_logs,
    )
    media = MediaService(
        settings,
        openlist,
        storage,
        media_repository,
        path_config,
        runtime_logs,
    )
    emby = EmbyClient(settings, emby_http)
    episode_images = EpisodeImageService(settings, openlist, emby, runtime_logs)
    media_probe = MediaProbeService(
        settings,
        MediaProbeRepository(database.sessions),
        runtime_repository,
        openlist,
        emby,
        path_config,
        runtime_logs,
        episode_images,
    )
    emby302 = Emby302Gateway(
        settings,
        emby_http,
        openlist,
        runtime_repository,
        runtime_logs,
    )
    notifications = WecomWebhookService(runtime_repository, notification_http, runtime_logs)
    bdpan = BdpanAutomationService(
        settings,
        bdpan_cli,
        runtime_repository,
        media,
        openlist,
        emby,
        path_config,
        runtime_logs,
        notifications,
        emby302,
        media_probe,
        storage=storage,
    )
    telegram = TelegramTrackerService(runtime_repository, media, bdpan, runtime_logs)
    system_status = SystemStatusService(
        settings,
        database,
        openlist,
        emby,
        bdpan,
        emby302,
        media,
        transfers,
        path_config,
    )
    return ServiceContainer(
        database=database,
        openlist=openlist,
        storage=storage,
        media=media,
        emby=emby,
        transfers=transfers,
        legacy_importer=LegacyJsonImporter(settings, database.sessions, media_repository, openlist),
        path_config=path_config,
        emby302=emby302,
        media_probe=media_probe,
        bdpan=bdpan,
        telegram=telegram,
        notifications=notifications,
        system_status=system_status,
    )
