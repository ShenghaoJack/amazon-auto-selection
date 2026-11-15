"""Step 11 login, resume and quota regression coverage."""
from pathlib import Path
from types import SimpleNamespace
import zipfile
import pytest
from core.errors import BrowserError, DownloadError, StepFailedError
from steps.step11_traffic_source import Step11TrafficSource, SellerSpriteSessionError, _valid_xlsx


def test_empty_marker_is_bound_to_exact_group_site_and_month(tmp_path):
    step=Step11TrafficSource(); first=['B000C91625','B04A8EAA04']; second=['B08B0176A6']
    step._mark_empty_skip(str(tmp_path),1,first,site='美国',month='2026-09')
    files={p.name for p in tmp_path.iterdir()}
    assert step._already_downloaded(first,files,str(tmp_path),site='美国',month='2026-09')
    assert not step._already_downloaded(second,files,str(tmp_path),site='美国',month='2026-09')
    assert not step._already_downloaded(first,files,str(tmp_path),site='美国',month='2026-08')
    assert not step._already_downloaded(first,files,str(tmp_path),site='英国',month='2026-09')
    assert not step._already_downloaded([first[0],'B06CF129BA'],files,str(tmp_path),site='美国',month='2026-09')


def test_legacy_empty_marker_does_not_skip_unrelated_or_unverifiable_group(tmp_path):
    p=tmp_path/'B000C91625-empty.no-content';p.write_text('旧标记，无站点月份',encoding='utf8');step=Step11TrafficSource()
    assert not step._already_downloaded(['B000C91625'],{p.name},str(tmp_path),site='美国',month='2026-09')
    assert not step._already_downloaded(['B04A8EAA04'],{p.name},str(tmp_path),site='美国',month='2026-09')


def test_incomplete_or_non_xlsx_cannot_be_reused(tmp_path):
    path=tmp_path/'B000C91625.xlsx';step=Step11TrafficSource();path.write_bytes(b'incomplete')
    assert not _valid_xlsx(str(path))
    assert not step._already_downloaded(['B000C91625'],{path.name},str(tmp_path))
    from openpyxl import Workbook
    workbook=Workbook();workbook.active.append(['商品', '流量']);workbook.save(path);workbook.close()
    assert _valid_xlsx(str(path)) and step._already_downloaded(['B000C91625'],{path.name},str(tmp_path))


def test_guest_rechecks_login_and_refuses_inconsistent_tool_page(monkeypatch):
    from steps import step11_traffic_source as mod
    from core.browser import BrowserAutomation
    monkeypatch.setattr(mod.time,'sleep',lambda n:None)
    monkeypatch.setattr(mod,'_visible_guest_marker',lambda page:True)
    cleared=[];monkeypatch.setattr(BrowserAutomation,'reset_login_verified',lambda profile:cleared.append(profile))
    step=Step11TrafficSource();step.log=lambda *a:None; calls=[];step._ensure_login_for=lambda *a:calls.append('rechecked')
    browser=SimpleNamespace(profile_dir='backup-profile',goto=lambda *a:calls.append('goto'))
    with pytest.raises(SellerSpriteSessionError,match='仍显示未登录'):
        step._ensure_tool_session(None,browser,object(),{'kind':'backup','note':'备用1'})
    assert cleared==['backup-profile'] and calls==['rechecked','goto']


def test_transient_guest_marker_does_not_force_relogin(monkeypatch):
    from steps import step11_traffic_source as mod
    monkeypatch.setattr(mod.time,'sleep',lambda n:None); markers=iter([True,False]);monkeypatch.setattr(mod,'_visible_guest_marker',lambda page:next(markers))
    step=Step11TrafficSource();step._ensure_login_for=lambda *a:pytest.fail('temporary marker must not force relogin')
    step._ensure_tool_session(None,object(),object(),{'kind':'backup','note':'备用1'})


def test_query_error_does_not_mark_group_success(monkeypatch,tmp_path):
    from steps import step11_traffic_source as mod
    monkeypatch.setattr(mod.time,'sleep',lambda n:None)
    step=Step11TrafficSource();step.log=lambda *a:None;step._shot=lambda *a:None
    for name in ('_select_site','_select_month','_fill_asins','_click_query'): monkeypatch.setattr(step,name,lambda *a:None)
    monkeypatch.setattr(step,'_wait_results',lambda *a:'error')
    browser=SimpleNamespace(goto=lambda *a:None)
    with pytest.raises(BrowserError,match='平台错误'):
        step._run_group(None,browser,object(),1,['B000C91625'],'美国','2026-09',3,str(tmp_path))
    assert not list(tmp_path.glob('*.no-content'))


def test_download_failure_does_not_mark_quota_zero(monkeypatch,tmp_path):
    from steps import step11_traffic_source as mod
    class B:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def new_page(self):return object()
        def set_download_dir(self,*a):pass
    ctx=SimpleNamespace(browser=lambda *a,**k:B());step=Step11TrafficSource();step.log=lambda *a:None
    monkeypatch.setattr(step,'_ensure_login_for',lambda *a:None);monkeypatch.setattr(step,'_already_downloaded',lambda *a,**k:False)
    monkeypatch.setattr(step,'_run_group',lambda *a,**k:(_ for _ in ()).throw(DownloadError('非配额下载失败')))
    monkeypatch.setattr(step,'_mark_main_exhausted',lambda *a:pytest.fail('下载失败不应标记配额耗尽'))
    todo=[(1,['B000C91625'])];assert step._run_groups_on_account(ctx,{'kind':'main','note':'主'},todo,'美国','2026-09',5,10,str(tmp_path),has_next=True)==todo


def test_quota_flags_do_not_switch_shared_chrome_account(monkeypatch,tmp_path):
    from steps import step11_traffic_source as mod
    ctx=SimpleNamespace(config={'SS_CDP_URL':'http://127.0.0.1:9222'},paths=SimpleNamespace(cat_root=tmp_path)); step=Step11TrafficSource();step.log=lambda *a:None;seen=[]
    monkeypatch.setattr(step,'_run_groups_on_account',lambda ctx,acct,*args,**kwargs:seen.append(acct['kind']) or [])
    flag=tmp_path/f".ss-main-quota-{mod.time.strftime('%Y%m%d')}.flag"; flag.write_text('main sellersprite account quota exhausted (backup-first today)',encoding='utf8')
    step._run_real(ctx,[['B000C91625']],'美国','2026-09',5,1,str(tmp_path));assert seen==['current']
    seen.clear();flag.write_text('confirmed_zero_quota\n',encoding='utf8')
    step._run_real(ctx,[['B000C91625']],'美国','2026-09',5,1,str(tmp_path));assert seen==['current']

def test_quota_exhaustion_requires_manual_switch_and_preserves_todo(monkeypatch,tmp_path):
    ctx=SimpleNamespace(config={'SS_CDP_URL':'http://127.0.0.1:9222'}); step=Step11TrafficSource();step.log=lambda *a:None
    seen=[]
    def run(ctx,acct,todo,*args,**kwargs):
        seen.append((acct['kind'],kwargs['has_next']))
        return todo
    monkeypatch.setattr(step,'_run_groups_on_account',run)
    with pytest.raises(StepFailedError,match='切换到有配额的账号'):
        step._run_real(ctx,[['B000C91625']],'美国','2026-09',5,1,str(tmp_path))
    assert seen==[('current',False)]

def test_real_step_requires_cdp_instead_of_testing_browser(tmp_path):
    step=Step11TrafficSource();step.log=lambda *a:None
    with pytest.raises(StepFailedError,match='SS_CDP_URL'):
        step._run_real(SimpleNamespace(config={}),[['B000C91625']],'美国','2026-09',5,1,str(tmp_path))

def test_download_started_and_finished_during_click_is_detected(monkeypatch,tmp_path):
    from steps import step11_traffic_source as mod
    from openpyxl import Workbook
    step=Step11TrafficSource();step.log=lambda *a:None
    monkeypatch.setattr(mod,'read_export_quota',lambda *a:(10,'剩余10次'))
    def click(*args,**kwargs):
        wb=Workbook();wb.active.append(['keyword','ASIN']);wb.save(tmp_path/'fast.xlsx');wb.close()
        return True
    monkeypatch.setattr(mod,'click_verified',click);monkeypatch.setattr(mod.time,'sleep',lambda _:None)
    assert step._export_with_retry(None,object(),object(),str(tmp_path),2)==str(tmp_path/'fast.xlsx')


def test_download_that_overwrites_empty_file_is_detected(monkeypatch,tmp_path):
    from steps import step11_traffic_source as mod
    from openpyxl import Workbook
    path=tmp_path/'same.xlsx';path.write_bytes(b'');before=mod._download_snapshot(str(tmp_path))
    wb=Workbook();wb.save(path);wb.close();monkeypatch.setattr(mod.time,'sleep',lambda _:None)
    assert mod._wait_new_workbook(str(tmp_path),before,2)==str(path)


def test_unmodified_old_workbook_is_not_a_new_download(monkeypatch,tmp_path):
    from steps import step11_traffic_source as mod
    from openpyxl import Workbook
    path=tmp_path/'old.xlsx';wb=Workbook();wb.save(path);wb.close();before=mod._download_snapshot(str(tmp_path))
    clock=iter([0.0,0.1,0.5,1.1]);monkeypatch.setattr(mod.time,'monotonic',lambda:next(clock));monkeypatch.setattr(mod.time,'sleep',lambda _:None)
    assert mod._wait_new_workbook(str(tmp_path),before,1)==None


def test_failed_export_click_does_not_wait_for_download(monkeypatch,tmp_path):
    from steps import step11_traffic_source as mod
    step=Step11TrafficSource();step.log=lambda *a:None
    monkeypatch.setattr(mod,'read_export_quota',lambda *a:(10,''));monkeypatch.setattr(mod,'click_verified',lambda *a,**kw:False)
    monkeypatch.setattr(mod,'_wait_new_workbook',lambda *a:pytest.fail('点击失败不应等待下载'))
    assert step._export_with_retry(None,object(),object(),str(tmp_path),2) is None

def test_site_selection_waits_after_each_click(monkeypatch):
    from steps import step11_traffic_source as mod
    events=[]; monkeypatch.setattr(mod,'click_verified',lambda page,spec,**kw:events.append('click:'+kw['what']) or True)
    monkeypatch.setattr(mod.time,'sleep',lambda t:events.append(('wait',t)));monkeypatch.setattr(mod,'park_mouse',lambda page:events.append('park'))
    step=Step11TrafficSource();step.log=lambda *a:None;browser=SimpleNamespace(wait_selector=lambda *a,**k:events.append('selector'))
    step._select_site(None,browser,object(),'美国')
    assert events==['click:站点下拉',('wait',mod._RENDER_WAIT),'selector','click:站点选项[美国]',('wait',mod._RENDER_WAIT),'park']


def test_month_selection_waits_after_each_click_before_readback(monkeypatch):
    from steps import step11_traffic_source as mod
    events=[];monkeypatch.setattr(mod,'click_verified',lambda *a,**kw:events.append('open') or True);monkeypatch.setattr(mod.time,'sleep',lambda t:events.append(('wait',t)));monkeypatch.setattr(mod,'park_mouse',lambda _:events.append('park'))
    class Elem:
        first=None
        def __init__(self):self.first=self
        def filter(self,**kw):return self
        def wait_for(self,**kw):events.append('visible')
        def click(self,**kw):events.append('choose')
        def input_value(self):events.append('read');return '2026-09'
    elem=Elem();page=SimpleNamespace(locator=lambda sel:elem);monkeypatch.setattr(mod,'first_visible',lambda *a:elem)
    step=Step11TrafficSource();step.log=lambda *a:None;step._select_month(None,None,page,'2026-09')
    assert events==['open',('wait',mod._RENDER_WAIT),'visible','choose',('wait',mod._RENDER_WAIT),'read','park']


def test_query_waits_after_click(monkeypatch):
    from steps import step11_traffic_source as mod
    events=[];monkeypatch.setattr(mod,'click_verified',lambda *a,**kw:events.append('click') or True);monkeypatch.setattr(mod.time,'sleep',lambda t:events.append(('wait',t)));monkeypatch.setattr(mod,'park_mouse',lambda _:events.append('park'))
    step=Step11TrafficSource();step.log=lambda *a:None;step._click_query(None,None,object())
    assert events==['click',('wait',mod._RENDER_WAIT),'park']


def test_export_menu_waits_after_both_clicks(monkeypatch,tmp_path):
    from steps import step11_traffic_source as mod
    events=[]; monkeypatch.setattr(mod,'read_export_quota',lambda *a:(1,''));monkeypatch.setattr(mod,'click_verified',lambda *a,**kw:events.append('click:'+kw['what']) or True);monkeypatch.setattr(mod.time,'sleep',lambda t:events.append(('wait',t)))
    results=iter([None,str(tmp_path/'done.xlsx')]);monkeypatch.setattr(mod,'_wait_new_workbook',lambda *a:events.append('download') or next(results));monkeypatch.setattr(mod,'_download_snapshot',lambda *_:{})
    browser=SimpleNamespace(wait_selector=lambda *a,**kw:events.append('selector'));step=Step11TrafficSource();step.log=lambda *a:None
    assert step._export_with_retry(None,browser,object(),str(tmp_path),2).endswith('done.xlsx')
    assert events==['click:导出',('wait',mod._RENDER_WAIT),'download','click:导出下拉',('wait',mod._RENDER_WAIT),'selector','click:导出Excel',('wait',mod._RENDER_WAIT),'download']


def test_login_submit_wait_is_opt_in_and_follows_click(monkeypatch):
    from core.browser import BrowserAutomation
    import core.browser as mod
    events=[];monkeypatch.setattr(mod.time,'sleep',lambda t:events.append(('wait',t)))
    class Input:
        def fill(self,value):events.append('fill')
    class Button:
        def click(self,**kw):events.append('submit')
    elems=iter([Input(),Input(),Button()]);browser=BrowserAutomation({},'test-profile');monkeypatch.setattr(browser,'_first_visible',lambda *a:next(elems))
    assert browser._fill_sellersprite_form(object(),'account','password',click_wait=1.5)
    assert events==['fill','fill','submit',('wait',1.5)]

def test_account_window_label_follows_configuration():
    step=Step11TrafficSource();a={'kind':'backup'}
    assert step._account_window_label(SimpleNamespace(config={'SS_QUOTA_WINDOW_START':'08:00','SS_QUOTA_WINDOW_END':'08:00'}),a)=='全天可用'
    assert step._account_window_label(SimpleNamespace(config={'SS_QUOTA_WINDOW_START':'19:00','SS_QUOTA_WINDOW_END':'08:00'}),a)=='19:00–08:00可导出'
