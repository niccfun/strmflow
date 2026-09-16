import re
from html.parser import HTMLParser
from pathlib import Path


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append(value)


def test_index_has_unique_element_ids() -> None:
    template = (Path(__file__).parents[1] / "src/strmflow/web/templates/index.html").read_text(
        encoding="utf-8"
    )
    parser = IdCollector()
    parser.feed(template)
    duplicates = {element_id for element_id in parser.ids if parser.ids.count(element_id) > 1}
    assert duplicates == set()
    assert 'id="settingsButton"' in template
    assert "<title>StrmFlow</title>" in template
    assert "<h1>StrmFlow</h1>" in template
    assert 'src="/static/strmflow.svg"' in template
    assert 'id="sidebar"' in template
    sidebar_start = template.index('id="sidebar"')
    sidebar_end = template.index("</aside>", sidebar_start)
    assert sidebar_start < template.index('id="settingsButton"') < sidebar_end
    assert sidebar_start < template.index('id="systemStatusButton"') < sidebar_end
    assert sidebar_start < template.index('id="logsButton"') < sidebar_end
    assert sidebar_start < template.index('id="emby302Button"') < sidebar_end
    assert sidebar_start < template.index('id="aboutButton"') < sidebar_end
    assert 'id="mediaView"' in template
    assert 'id="systemStatusView"' in template
    assert 'id="systemStatusOverall"' in template
    assert 'id="systemStatusStorageList"' in template
    assert 'id="systemStatusRefreshButton"' in template
    assert "showView('status')" in template
    assert "'/api/status/overview'" in template
    assert 'data-status-field="quota-used"' in template
    assert 'data-status-field="quota-total"' in template
    assert "formatByteSize" in template
    assert "media-kind-mark" in template
    assert "episode-summary" in template
    assert "episode-total-tag" in template
    assert "media-progress-track" not in template
    assert "填写总集数后可显示完整进度" not in template
    assert "details-toggle" not in template
    assert "查看目录详情" not in template
    assert 'id="mediaDetailOverview"' in template
    assert 'id="editorPanel"' in template
    assert 'id="mediaDetailSourcePath"' in template
    assert 'id="mediaDetailTargetPath"' in template
    assert "media-details-button" in template
    assert "媒体详情" in template
    assert ".target-panel { width: min(1180px" in template
    assert "overflow: hidden; display: grid; grid-template-columns: repeat(4" in template
    assert (
        ".target-panel:not(.detail-mode) .source-resource-field { grid-column: span 2; }"
        in template
    )
    assert (
        ".target-panel:not(.detail-mode) .editor-actions .danger-button { display: none; }"
        in template
    )
    assert ".target-panel.detail-mode { width: min(1180px" in template
    assert "elements.editorPanel.classList.add('detail-mode')" in template
    assert "restoreSavedSourceSelection" in template
    detail_handler = template[
        template.index("function selectFolder(folder)") : template.index(
            "function updateTargetPreview()"
        )
    ]
    assert "loadSourceFolders" not in detail_handler
    assert "restoreSavedSourceSelection(folder" in detail_handler
    assert "loadSourceFolders(true, typeDir, category, sourcePath, false)" in template
    assert "openListExternalUrl" in template
    assert "在 OpenList 打开此目录" in template
    assert "OPENLIST_WEB_URL" in template
    assert 'id="settingsView"' in template
    assert 'id="logsView"' in template
    assert 'id="logsList"' in template
    assert 'class="log-table"' in template
    assert ".log-table th:nth-child(2), .log-table td:nth-child(2)" in template
    assert "width: 96px; min-width: 96px; white-space: nowrap" in template
    assert 'id="logsCategoryFilter"' not in template
    assert 'id="logsRefreshButton"' in template
    assert 'id="logsRealtimeButton"' in template
    assert "'/api/logs?limit=500'" in template
    assert "toggleRealtimeLogs" in template
    assert 'id="emby302View"' in template
    assert 'id="emby302EnabledInput"' in template
    assert 'id="emby302EmbyUrlInput"' in template
    assert 'id="emby302OpenListUrlInput"' in template
    assert 'id="emby302RecentList"' in template
    assert ".gateway-config-panel { width: 100%" in template
    assert "http://emby:8096" in template
    assert "http://openlist:5244" in template
    assert "'/api/emby302'" in template
    assert 'id="aboutView"' in template
    assert 'id="settingsModal"' not in template
    assert 'id="logsModal"' not in template
    assert 'id="aboutModal"' not in template
    assert "showView('settings')" in template
    assert "showView('logs')" in template
    assert "showView('about')" in template
    assert "strmflow.logs" in template
    assert 'id="bdpanSettingsTitle"' in template
    assert 'id="bdpanEnabledInput"' in template
    assert 'id="bdpanBinaryInput"' in template
    assert 'id="bdpanSaveRootInput"' in template
    assert 'id="bdpanIntervalInput"' in template
    assert 'id="bdpanLoginStartButton"' in template
    assert "'当前账号：' + accountName" in template
    assert "date.getFullYear() + '-'" in template
    assert "分钟后" not in template
    assert 'id="bdpanDisclaimerInput"' in template
    assert 'id="bdpanAuthorizationCodeInput"' in template
    assert 'id="bdpanWatchList"' in template
    assert "'/api/bdpan'" in template
    assert "'/api/bdpan/check'" in template
    assert "首次检查只建立基线" in template
    assert "百度网盘分享链接（用于自动追更）" in template
    assert 'id="wecomSettingsTitle"' in template
    assert 'id="wecomWebhookInput"' in template
    assert 'id="wecomEpisodeUpdateInput"' in template
    assert 'id="wecomLinkInvalidInput"' in template
    assert 'id="wecomTestButton"' in template
    assert 'id="wecomClearButton"' in template
    assert "'/api/notifications/wecom'" in template
    assert "'/api/notifications/wecom/test'" in template
    assert "Webhook 密钥保存后不再返回页面" in template
    assert 'id="addModeSwitch"' in template
    assert 'data-add-mode="saved"' in template
    assert 'data-add-mode="share"' in template
    assert "已保存添加" in template
    assert "分享链接添加" in template
    assert 'id="shareImportPanel"' in template
    assert 'id="shareLinkInput"' in template
    assert 'id="shareInspectButton"' in template
    assert 'id="shareCandidateInput"' in template
    assert "'/api/bdpan/share/inspect'" in template
    assert "'/api/bdpan/share/import'" in template
    assert "function configureAddMode(mode)" in template
    assert "function inspectShare()" in template
    assert "转存已提交，文件落盘后将自动扫描同步" in template
    assert "v__STRMFLOW_VERSION__" in template
    assert 'id="sourceFolderInput"' in template
    assert 'id="sourceTypeInput"' in template
    assert 'id="refreshSourcesButton"' in template
    assert 'id="sourcePathInput" type="hidden"' in template
    assert "/api/media/options" in template
    assert "'请选择一级目录'" in template
    assert "'请选择二级分类'" in template
    assert "'请选择媒体资源'" in template
    assert "loadSourceFolders(true, '', '', '', true)" in template
    assert "· 已添加" in template
    assert "该媒体已添加" in template
    assert "generateSelected(savedItem, { autoConfirm: true })" in template
    assert "默认季数（多季资源自动识别）" in template
    assert "同步时自动生成 Season XX" in template
    assert "function seasonSummary(folder)" in template
    assert 'class="modal-actions sync-actions"' in template
    assert template.index('id="renameCancelButton"') < template.index('id="renameConfirmButton"')
    assert 'class="overview"' not in template
    assert "查看当前路径配置" not in template
    assert "快捷键" not in template
    assert "addEventListener('keydown'" not in template
    queried_ids = set(re.findall(r"querySelector\('#([^']+)'\)", template))
    assert queried_ids.difference(parser.ids) == set()
