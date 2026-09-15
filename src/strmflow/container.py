from __future__ import annotations

from dataclasses import dataclass

import httpx

from strmflow.core.config import Settings
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.infrastructure.database import Database
from strmflow.repositories import (
    MediaRepository,
    RuntimeSettingsRepository,
    TransferJobRepository,
)
from strmflow.services.bdpan import BdpanCli
from strmflow.services.bdpan_automation import BdpanAutomationService
from strmflow.services.emby import EmbyClient
from strmflow.services.emby302 import Emby302Gateway
from strmflow.services.legacy_import import LegacyJsonImporter
from strmflow.services.media import MediaService
from strmflow.services.openlist import OpenListClient
from strmflow.services.path_config import PathConfigService
from strmflow.services.storage import StorageService
from strmflow.services.system_status import SystemStatusService
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
    bdpan: BdpanAutomationService
    system_status: SystemStatusService


def build_container(
    settings: Settings,
    database: Database,
    openlist_http: httpx.AsyncClient,
    emby_http: httpx.AsyncClient,
    baidu_http: httpx.AsyncClient,
    runtime_logs: RuntimeLogStore,
) -> ServiceContainer:
    openlist = OpenListClient(settings, openlist_http)
    path_config = PathConfigService(settings, RuntimeSettingsRepository(database.sessions))
    storage = StorageService(settings, openlist, path_config)
    media_repository = MediaRepository(database.sessions)
    transfer_repository = TransferJobRepository(database.sessions, settings.transfer_job_retention)
    bdpan_cli = BdpanCli(settings, baidu_http)
    transfers = TransferManager(
        settings, [BdpanTransferProvider(settings, bdpan_cli)], transfer_repository
    )
    media = MediaService(settings, openlist, storage, media_repository, path_config)
    emby = EmbyClient(settings, emby_http)
    emby302 = Emby302Gateway(
        settings,
        emby_http,
        openlist,
        RuntimeSettingsRepository(database.sessions),
        runtime_logs,
    )
    bdpan = BdpanAutomationService(
        settings,
        bdpan_cli,
        RuntimeSettingsRepository(database.sessions),
        media,
        openlist,
        emby,
        path_config,
        runtime_logs,
    )
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
        bdpan=bdpan,
        system_status=system_status,
    )
