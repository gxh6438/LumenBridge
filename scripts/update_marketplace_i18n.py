#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "src" / "endstone_lumenbridge" / "locales"

MARKET = {
    "zh_CN": {
        "title": "插件市场", "page_subtitle": "浏览自建市场的审核插件，安全校验下载并自动处理依赖。", "back_to_plugins": "返回子插件", "open_button": "插件市场", "check_updates_button": "检查市场更新", "check_framework_button": "检查框架更新", "framework_checking": "正在检查 LumenBridge 框架更新…", "framework_unconfigured": "尚未配置版本更新 API。请在配置页的“框架更新”中填写 API 地址。", "framework_latest": "当前已是最新版本 v{version}。", "framework_available": "发现 LumenBridge v{version} 新版本。", "framework_stage_button": "安全暂存更新", "framework_restart_note": "将下载、校验并原子替换 plugins 目录中的 wheel；完成后必须完整重启 Endstone 才会加载新版本。", "framework_stage_confirm": "确定暂存已校验的 LumenBridge 新版本吗？旧 wheel 将自动备份，完成后请完整重启 Endstone。", "framework_stage_success": "新版本已安全暂存。请完整重启 Endstone 以加载新 wheel。", "framework_check_failed": "框架更新检查失败", "framework_stage_failed": "框架更新暂存失败",
        "search_placeholder": "搜索插件名称、简介或标签", "search_button": "搜索", "loading_config": "正在读取市场配置…",
        "unconfigured_hint": "尚未配置插件市场 API。请在配置页的“插件市场”中填写 API 地址并启用。",
        "disabled_hint": "已填写市场 API，但市场功能尚未启用。请在配置页开启插件市场。",
        "secure_hint": "市场下载会校验 SHA-256；安装依赖后将自动尝试热重载子插件。",
        "loading": "正在加载市场插件…", "results": "找到 {count} 个插件", "empty": "没有找到符合条件的插件。",
        "install_button": "安全安装", "install_confirm": "确定从插件市场安装 {id} 吗？将校验下载完整性并安装声明的依赖。",
        "installing": "正在下载、校验并安装市场插件…", "install_success": "市场插件已安装，正在刷新子插件状态。", "install_failed": "市场插件安装失败",
        "task_success": "市场任务完成", "task_failed": "市场任务失败", "checking": "正在检查市场子插件更新…",
        "check_complete": "市场更新检查完成", "check_failed": "市场更新检查失败", "installed_badge": "市场安装",
        "update_available_badge": "可更新至 v{version}", "update_button": "更新插件", "update_confirm": "确定更新市场插件 {name} 吗？将校验新版本并升级其声明依赖。",
        "update_success": "{name} 已更新，正在刷新状态。", "update_failed": "{name} 更新失败",
        "update_deps_button": "更新依赖", "deps_update_confirm": "确定更新 {name} 的已声明依赖吗？会执行冲突预检并随后热重载。",
        "deps_update_success": "{name} 的依赖更新完成。", "deps_update_failed": "{name} 的依赖更新失败",
    },
    "zh_TW": {
        "title": "外掛市集", "page_subtitle": "瀏覽自建市集的審核外掛，安全驗證下載並自動處理相依套件。", "back_to_plugins": "返回子外掛", "open_button": "外掛市集", "check_updates_button": "檢查市集更新", "check_framework_button": "檢查框架更新", "framework_checking": "正在檢查 LumenBridge 框架更新…", "framework_unconfigured": "尚未設定版本更新 API。請在設定頁的「框架更新」填寫 API 位址。", "framework_latest": "目前已是最新版本 v{version}。", "framework_available": "發現 LumenBridge v{version} 新版本。", "framework_stage_button": "安全暫存更新", "framework_restart_note": "會下載、驗證並原子替換 plugins 目錄中的 wheel；完成後必須完整重啟 Endstone 才會載入新版本。", "framework_stage_confirm": "確定暫存已驗證的 LumenBridge 新版本嗎？舊 wheel 將自動備份，完成後請完整重啟 Endstone。", "framework_stage_success": "新版本已安全暫存。請完整重啟 Endstone 以載入新 wheel。", "framework_check_failed": "框架更新檢查失敗", "framework_stage_failed": "框架更新暫存失敗",
        "search_placeholder": "搜尋外掛名稱、簡介或標籤", "search_button": "搜尋", "loading_config": "正在讀取市集設定…",
        "unconfigured_hint": "尚未設定外掛市集 API。請在設定頁的「外掛市集」填寫 API 位址並啟用。",
        "disabled_hint": "已填寫市集 API，但市集功能尚未啟用。請在設定頁開啟外掛市集。",
        "secure_hint": "市集下載會驗證 SHA-256；安裝相依套件後將自動嘗試熱重載外掛。",
        "loading": "正在載入市集外掛…", "results": "找到 {count} 個外掛", "empty": "沒有找到符合條件的外掛。",
        "install_button": "安全安裝", "install_confirm": "確定從外掛市集安裝 {id} 嗎？將驗證下載完整性並安裝宣告的相依套件。",
        "installing": "正在下載、驗證並安裝市集外掛…", "install_success": "市集外掛已安裝，正在重新整理外掛狀態。", "install_failed": "市集外掛安裝失敗",
        "task_success": "市集工作完成", "task_failed": "市集工作失敗", "checking": "正在檢查市集外掛更新…",
        "check_complete": "市集更新檢查完成", "check_failed": "市集更新檢查失敗", "installed_badge": "市集安裝",
        "update_available_badge": "可更新至 v{version}", "update_button": "更新外掛", "update_confirm": "確定更新市集外掛 {name} 嗎？將驗證新版本並升級其宣告相依套件。",
        "update_success": "{name} 已更新，正在重新整理狀態。", "update_failed": "{name} 更新失敗",
        "update_deps_button": "更新相依套件", "deps_update_confirm": "確定更新 {name} 的已宣告相依套件嗎？會先執行衝突預檢，然後熱重載。",
        "deps_update_success": "{name} 的相依套件更新完成。", "deps_update_failed": "{name} 的相依套件更新失敗",
    },
    "en": {
        "title": "Plugin Marketplace", "page_subtitle": "Browse reviewed self-hosted plugins with verified downloads and automatic dependency handling.", "back_to_plugins": "Back to plugins", "open_button": "Marketplace", "check_updates_button": "Check marketplace updates", "check_framework_button": "Check framework update", "framework_checking": "Checking for a LumenBridge framework update…", "framework_unconfigured": "No version-update API is configured. Set it under Framework updates in Settings.", "framework_latest": "Already on the latest version v{version}.", "framework_available": "LumenBridge v{version} is available.", "framework_stage_button": "Stage verified update", "framework_restart_note": "The wheel will be downloaded, verified, and atomically placed in plugins. Fully restart Endstone to load it.", "framework_stage_confirm": "Stage the verified LumenBridge update? The old wheel will be backed up; fully restart Endstone afterwards.", "framework_stage_success": "The new version is safely staged. Fully restart Endstone to load it.", "framework_check_failed": "Framework update check failed", "framework_stage_failed": "Framework update staging failed",
        "search_placeholder": "Search plugin names, summaries, or tags", "search_button": "Search", "loading_config": "Loading marketplace configuration…",
        "unconfigured_hint": "Marketplace API is not configured. Set its API URL and enable it under Marketplace in Settings.",
        "disabled_hint": "A marketplace API URL is set, but the marketplace is disabled. Enable it in Settings.",
        "secure_hint": "Marketplace downloads are SHA-256 verified; declared dependencies are installed before a plugin reload is attempted.",
        "loading": "Loading marketplace plugins…", "results": "Found {count} plugins", "empty": "No plugins match your search.",
        "install_button": "Secure install", "install_confirm": "Install {id} from the marketplace? Its download will be verified and declared dependencies installed.",
        "installing": "Downloading, verifying, and installing marketplace plugin…", "install_success": "Marketplace plugin installed. Refreshing plugin status.", "install_failed": "Marketplace installation failed",
        "task_success": "Marketplace task completed", "task_failed": "Marketplace task failed", "checking": "Checking marketplace plugin updates…",
        "check_complete": "Marketplace update check completed", "check_failed": "Marketplace update check failed", "installed_badge": "Marketplace",
        "update_available_badge": "Update v{version} available", "update_button": "Update plugin", "update_confirm": "Update marketplace plugin {name}? The new release and declared dependencies will be verified.",
        "update_success": "{name} updated. Refreshing status.", "update_failed": "Failed to update {name}",
        "update_deps_button": "Update dependencies", "deps_update_confirm": "Update declared dependencies for {name}? A conflict preflight will run before hot-reload.",
        "deps_update_success": "Dependencies for {name} updated.", "deps_update_failed": "Failed to update dependencies for {name}",
    },
}

SUBPLUGIN_RELOAD = {
    "zh_CN": {
        "reload_one_button": "重载此插件", "reload_one_confirm": "确定只重载子插件 {name} 吗？不会重载其他子插件。",
        "reload_one_success": "子插件 {name} 已重载。", "reload_one_failed": "子插件 {name} 重载失败：{error}",
        "manual_install_next_step": "请在 Endstone 服务器后台执行下面的命令。命令成功后，回到此插件卡片并点击“重载此插件”；无需重载全部子插件，也无需重启服务器。若仍失败，请展开错误详情检查依赖的导入名。",
        "manual_command_copied": "手动安装命令已复制。",
    },
    "zh_TW": {
        "reload_one_button": "重載此外掛", "reload_one_confirm": "確定只重載子外掛 {name} 嗎？不會重載其他子外掛。",
        "reload_one_success": "子外掛 {name} 已重載。", "reload_one_failed": "子外掛 {name} 重載失敗：{error}",
        "manual_install_next_step": "請在 Endstone 伺服器後台執行下方命令。命令成功後，回到此外掛卡片並點選「重載此外掛」；無需重載全部子外掛，也無需重啟伺服器。若仍失敗，請展開錯誤詳情檢查相依套件的匯入名稱。",
        "manual_command_copied": "手動安裝命令已複製。",
    },
    "en": {
        "reload_one_button": "Reload this plugin", "reload_one_confirm": "Reload only sub-plugin {name}? Other sub-plugins will not be reloaded.",
        "reload_one_success": "Sub-plugin {name} reloaded.", "reload_one_failed": "Failed to reload sub-plugin {name}: {error}",
        "manual_install_next_step": "Run the command below in the Endstone server console. Once it succeeds, return to this plugin card and select Reload this plugin; do not reload every plugin or restart the server. If it still fails, open error details and check the dependency import name.",
        "manual_command_copied": "Manual installation command copied.",
    },
}

COMMUNITY_MARKET_I18N = {
    "zh_CN": {"sort_score": "综合热度", "sort_time": "最新发布", "sort_likes": "点赞最多", "sort_downloads": "下载最多", "report_button": "举报", "report_reason_prompt": "请填写举报内容：", "report_contact_prompt": "联系方式（邮箱或 QQ，可留空匿名）：", "report_success": "举报已提交给市场管理员处理。", "report_failed": "举报提交失败"},
    "zh_TW": {"sort_score": "綜合熱度", "sort_time": "最新發佈", "sort_likes": "最多讚好", "sort_downloads": "最多下載", "report_button": "檢舉", "report_reason_prompt": "請填寫檢舉內容：", "report_contact_prompt": "聯絡方式（電子郵件或 QQ，可留空匿名）：", "report_success": "檢舉已提交給市集管理員處理。", "report_failed": "檢舉提交失敗"},
    "en": {"sort_score": "Overall popularity", "sort_time": "Newest", "sort_likes": "Most liked", "sort_downloads": "Most downloaded", "report_button": "Report", "report_reason_prompt": "Describe the report:", "report_contact_prompt": "Contact (email or QQ; optional for anonymous):", "report_success": "Report submitted to marketplace administrators.", "report_failed": "Failed to submit report"},
}

REPORT_KEY_LABELS = {
    "zh_CN": ("WebUI 举报代理密钥", "可选：与 PHP 市场的 webui_report_api_key 保持一致，使 WebUI 可安全转发插件举报。"),
    "zh_TW": ("WebUI 檢舉代理金鑰", "可選：需與 PHP 市集的 webui_report_api_key 相同，讓 WebUI 可安全轉送外掛檢舉。"),
    "en": ("WebUI report proxy key", "Optional: match webui_report_api_key in the PHP market so WebUI can safely forward plugin reports."),
}

CONFIG_LABELS = {
    "zh_CN": {
        "marketplace": ("插件市场", {"enable": ("启用插件市场", "启用后可在子插件页浏览、校验并安装自建市场中的插件。"), "api_url": ("市场 API 地址", "PHP 市场服务的 /api/v1 地址。生产环境请使用 HTTPS。"), "allow_http": ("允许 HTTP（仅开发）", "仅本地开发市场可开启；生产环境保持关闭。"), "timeout": ("市场网络超时（秒）", "市场查询或下载的网络超时。"), "max_download_bytes": ("插件下载上限（字节）", "单个市场插件 ZIP 的最大允许下载体积。"), "check_on_start": ("启动时检查市场更新", "在后台检查市场来源子插件的新版本，不会自动安装。"), "check_interval_seconds": ("更新检查间隔（秒）", "同一子插件两次自动市场检查的最小间隔。")}),
        "updates": ("框架更新", {"enable": ("启用框架更新检查", "在 WebUI 提供 LumenBridge 最新版本检查。"), "api_url": ("版本更新 API 地址", "PHP 市场服务的 /api/v1/updates/lumenbridge 地址。"), "timeout": ("更新检查超时（秒）", "查询框架最新版本的网络超时。")}),
    },
    "zh_TW": {
        "marketplace": ("外掛市集", {"enable": ("啟用外掛市集", "啟用後可在子外掛頁瀏覽、驗證並安裝自建市集中的外掛。"), "api_url": ("市集 API 位址", "PHP 市集服務的 /api/v1 位址。正式環境請使用 HTTPS。"), "allow_http": ("允許 HTTP（僅開發）", "僅本機開發市集可開啟；正式環境保持關閉。"), "timeout": ("市集網路逾時（秒）", "市集查詢或下載的網路逾時。"), "max_download_bytes": ("外掛下載上限（位元組）", "單一市集外掛 ZIP 的最大允許下載大小。"), "check_on_start": ("啟動時檢查市集更新", "在背景檢查市集來源子外掛的新版本，不會自動安裝。"), "check_interval_seconds": ("更新檢查間隔（秒）", "同一子外掛兩次自動市集檢查的最小間隔。")}),
        "updates": ("框架更新", {"enable": ("啟用框架更新檢查", "在 WebUI 提供 LumenBridge 最新版本檢查。"), "api_url": ("版本更新 API 位址", "PHP 市集服務的 /api/v1/updates/lumenbridge 位址。"), "timeout": ("更新檢查逾時（秒）", "查詢框架最新版本的網路逾時。")}),
    },
    "en": {
        "marketplace": ("Marketplace", {"enable": ("Enable marketplace", "Browse, verify, and install plugins from the configured self-hosted marketplace."), "api_url": ("Marketplace API URL", "The /api/v1 URL of the PHP marketplace. Use HTTPS in production."), "allow_http": ("Allow HTTP (development only)", "Enable only for a local development market; keep disabled in production."), "timeout": ("Marketplace timeout (seconds)", "Network timeout for marketplace queries and downloads."), "max_download_bytes": ("Plugin download limit (bytes)", "Maximum allowed download size for one marketplace plugin ZIP."), "check_on_start": ("Check marketplace updates on startup", "Check market-origin plugins in the background; no plugin is installed automatically."), "check_interval_seconds": ("Update check interval (seconds)", "Minimum interval between automatic market checks for the same plugin.")}),
        "updates": ("Framework updates", {"enable": ("Enable framework update checks", "Expose LumenBridge latest-version checks in the WebUI."), "api_url": ("Version update API URL", "The PHP market /api/v1/updates/lumenbridge URL."), "timeout": ("Update check timeout (seconds)", "Network timeout for querying the latest framework version.")}),
    },
}

for locale, translations in MARKET.items():
    path = LOCALES / f"{locale}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["marketplace"] = {**translations, **COMMUNITY_MARKET_I18N[locale]}
    payload.setdefault("subplugins", {}).update(SUBPLUGIN_RELOAD[locale])
    # 完整删除已废弃的 pip 白名单配置及其提示文案；保留业务白名单绑定模块的独立翻译。
    pip_labels = payload.setdefault("config_labels", {}).setdefault("pip", {})
    pip_labels.pop("allow_all", None)
    pip_labels.pop("allow_list", None)
    if isinstance(payload.get("pip"), dict):
        payload["pip"].pop("not_in_whitelist", None)
    if isinstance(payload.get("pip_page"), dict):
        page = payload["pip_page"]
        page.pop("allow_all", None)
        page.pop("allow_all_on", None)
        page.pop("allow_all_off", None)
        page.pop("allow_list", None)
        page["config_tip"] = {
            "zh_CN": "镜像源可在配置文件 config.json 的 pip 块修改",
            "zh_TW": "鏡像來源可在設定檔 config.json 的 pip 區塊修改",
            "en": "The package index URL can be changed in the pip section of config.json",
        }[locale]
    labels = payload.setdefault("config_labels", {})
    for section, (section_name, entries) in CONFIG_LABELS[locale].items():
        labels[section] = {"section": section_name}
        for key, (label, desc) in entries.items():
            labels[section][key] = {"label": label, "desc": desc}
    report_label, report_desc = REPORT_KEY_LABELS[locale]
    labels.setdefault("marketplace", {})["report_api_key"] = {"label": report_label, "desc": report_desc}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
