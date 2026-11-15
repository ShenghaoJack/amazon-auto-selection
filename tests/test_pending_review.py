import json, os, sys
sys.path.insert(0, '.')
import pytest

def test_cold_start_is_not_incremental(tmp_path):
    '''★ 冷启动（prev_known={}）**不能**列\"新属性\" —— 没有参照物。'''
    from core.config import Config
    from core.paths import Paths
    from steps.base import StepContext
    from steps.step17_attr_annotation import Step17AttrAnnotation
    cfg = Config({'CAT':'t','DATA_ROOT':str(tmp_path),'MARKET':'amazon.com'})
    ctx = StepContext(cfg, Paths(cfg)); os.makedirs(ctx.paths.cat_root, exist_ok=True)
    st = Step17AttrAnnotation(); st._cat = 't'
    d = tmp_path / 'out'; d.mkdir()
    # 只传 delta、不传 prev_known → 会去读（读不到 → {}）→ 也是冷启动
    p = st._write_pending_review(ctx, str(d), delta={'A':[('x',1)]})
    assert p is None or '新属性' not in json.dumps(
        json.loads(open(p, encoding='utf-8').read()), ensure_ascii=False)
    # 显式 prev_known={} 同理
    p2 = st._write_pending_review(ctx, str(d), delta={'A':[('x',1)]}, prev_known={})
    assert p2 is None or '新属性' not in json.dumps(
        json.loads(open(p2, encoding='utf-8').read()), ensure_ascii=False)

def test_incremental_lists_new_attr_and_value(tmp_path):
    from core.config import Config
    from core.paths import Paths
    from steps.base import StepContext
    from steps.step17_attr_annotation import Step17AttrAnnotation
    cfg = Config({'CAT':'t','DATA_ROOT':str(tmp_path),'MARKET':'amazon.com'})
    ctx = StepContext(cfg, Paths(cfg)); os.makedirs(ctx.paths.cat_root, exist_ok=True)
    st = Step17AttrAnnotation(); st._cat = 't'
    d = tmp_path / 'out'; d.mkdir()
    p = st._write_pending_review(ctx, str(d), delta={'A':[('old',1),('new',1)], 'B':[('y',1)]},
                                 prev_known={'A': {'old'}})
    assert p
    o = json.loads(open(p, encoding='utf-8').read())
    assert o['统计']['新属性'] == 1          # B 是全新的属性
    assert o['统计']['已有属性的新值'] == 1     # A 有 1 个新值
    assert 'new' in json.dumps(o, ensure_ascii=False)
    assert 'old' not in json.dumps(o.get('items')[1], ensure_ascii=False)
