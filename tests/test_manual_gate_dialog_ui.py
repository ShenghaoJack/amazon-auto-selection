from pathlib import Path
import os

import pytest
from playwright.sync_api import sync_playwright

from core.browser import find_chrome_path


@pytest.mark.parametrize("kind,panel", [
    ("keywords", "secKwLists"),
    ("attributes", "secAttr"),
    ("standardization", "secStd"),
])
def test_manual_gate_can_close_until_reentering_category(kind, panel):
    html_path = Path(os.environ.get("GATE_UI_TEST_HTML", Path(__file__).resolve().parents[1] / "webui/index.html"))
    markup = html_path.read_text(encoding="utf-8")
    code = markup[markup.index('const RUN_CARDS ='):markup.index('/* 从类目中心打开某类目运行台')]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=find_chrome_path(), headless=True)
        page = browser.new_page()
        try:
            page.set_content("""
                <header></header><div class="wrap"></div><div id="homeCard"></div>
                <div id="secRun"></div><div id="secState"></div>
                <button id="openMonitor"></button><button id="openConfig"></button><button id="openLog"></button>
                <div id="workbenchOverlay" style="display:none">
                  <button id="workbenchClose">关闭</button><h3 id="workbenchTitle"></h3>
                  <p id="workbenchNote"></p><div id="workbenchBody"></div>
                </div><div id="workbenchParking">
                  <div id="secKwLists"></div><div id="secAttr"></div><div id="secStd"></div>
                  <div id="secMon"></div><div id="secCfg"></div><div id="secLog"></div>
                </div>
            """)
            page.add_script_tag(content="""
                let selFile = 'data/Example/config.yaml';
                const $ = id => document.getElementById(id);
                let activeRun = true;
                function __viewIsRun() { return activeRun; }
                function loadKeywordPanel() {}
                function loadAttrSpec() {}
                function loadStdVocab() {}
                function loadHome() {}
                function pollNow() {}
            """ + code)
            page.evaluate("initWorkbenchPanels()")
            gate = {"manual_gate": {"kind": kind, "message": "请先完成确认"}}
            page.evaluate("gate => syncManualGate(gate)", gate)
            assert page.locator("#workbenchOverlay").is_visible()
            assert page.locator("#workbenchClose").is_visible()
            assert page.locator("#workbenchNote").inner_text() == "请先完成确认"
            page.locator("#workbenchClose").click()
            assert page.locator("#workbenchOverlay").is_hidden()
            page.evaluate("gate => syncManualGate(gate)", gate)
            assert page.locator("#workbenchOverlay").is_hidden()
            page.evaluate("openWorkbenchPanel('secCfg')")
            page.evaluate("gate => syncManualGate(gate)", gate)
            assert page.locator("#workbenchTitle").inner_text() == "配置中心"
            page.evaluate("closeWorkbenchPanel()")
            page.evaluate("id => openWorkbenchPanel(id)", panel)
            assert page.locator("#workbenchNote").inner_text() == "请先完成确认"
            page.evaluate("closeWorkbenchPanel()")
            page.evaluate("showHome(); showRun()")
            page.evaluate("gate => syncManualGate(gate)", gate)
            assert page.locator("#workbenchOverlay").is_visible()
            page.evaluate("syncManualGate({manual_gate:null})")
            assert page.locator("#workbenchOverlay").is_hidden()
        finally:
            browser.close()
