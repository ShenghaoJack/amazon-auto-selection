from steps.step16_attribute_annotation import _review_final_nonboolean_duplicates
from tools.attr_stability.spec import build_dedupe_merge_prompt

def item(name,hint='',**extra): return dict(name=name,hint=hint,runs=3,source='llm',**extra)

def test_final_review_preserves_source_boundaries_and_manual_items():
    attrs=[item('食碗数量','碗数量'),item('食盆数量','碗个数'),item('是否带录音','录音'),item('手工属性','人工',modified=True)]
    out,n=_review_final_nonboolean_duplicates(attrs,lambda a:[{'name':'食盆数量','aliases':['食碗数量']}])
    assert n==1 and [a['name'] for a in out]==['食盆数量','是否带录音','手工属性']
    merged=out[0]; assert '碗数量' in merged['hint'] and '碗个数' in merged['hint'] and merged['use_full_hint']
    assert '食碗数量' in merged['aliases']

def test_final_review_rejects_partial_overlap_or_invented_source():
    attrs=[item('食碗数量'),item('食盆数量'),item('机身材质')]
    for response in ([{'name':'食盆数量','aliases':['食碗数量']}],
                     [{'name':'食盆数量','aliases':['食碗数量','不存在']},{'name':'机身材质','aliases':[]}],
                     [{'name':'食盆数量','aliases':['食碗数量']},{'name':'机身材质','aliases':['食碗数量']}]):
        assert _review_final_nonboolean_duplicates(attrs,lambda a:response)==(attrs,0)

def test_merge_prompt_rejects_assuming_subsumption_is_synonym():
    prompt=build_dedupe_merge_prompt([item('食盆数量'),item('餐格数量')])
    assert '可派生关系不是同义' in prompt and '不同部件的同类数量' in prompt
    assert '粮仓' not in prompt and '餐格' not in prompt.split('待归并属性清单')[0]  # 提示词规则不带类目专属例子
    assert '不得' not in prompt or '不得并进上位词' not in prompt

def test_step17_uses_full_boundaries_when_requested():
    from steps.step17_attr_annotation import Step17AttrAnnotation
    step=Step17AttrAnnotation()
    step._spec_attributes=[
        {'name':'餐数','hint':'必须明确每日周期；48小时2餐不能写每天2餐','hint_short':'读取餐数','use_full_hint':True},
        {'name':'材质','hint':'完整说明','hint_short':'读取材质'}]
    output=step._build_attr_hint_block(None)
    assert '48小时2餐不能写每天2餐' in output
    assert '读取材质' in output and '完整说明' not in output

def test_extraction_prompt_uses_soft_ratio_and_preserves_evidence_boundaries():
    import pandas as pd
    from types import SimpleNamespace
    from steps.step16_attribute_annotation import Step16AttributeAnnotation
    class Model:
        def chat_json(self,prompt,**kwargs):
            self.prompt=prompt
            return {'attributes':[{'name':'餐格数量','hint':'记录餐格'}]}
    model=Model(); ctx=SimpleNamespace(config=SimpleNamespace(cat='Cat Feeders'),llm=lambda:model)
    step=Step16AttributeAnnotation();step.log=lambda *a,**kw:None
    step._extract_attrs(ctx,pd.DataFrame({'标题':['feeder']}))
    assert '25%-35% 为软参考' in model.prompt
    assert '原文未提不等于否' in model.prompt
    assert '必须"证据互斥"' not in model.prompt

def test_invalid_mappings_fail_closed_without_hallucinated_names(caplog):
    from tools.attr_stability import global_merge as gm
    class Model:
        def __init__(self,value): self.value=value
        def chat_json(self,*a,**kw): return {'mapping':self.value}
    for invalid in ({'甲':'乙'}, {'甲':'甲','乙':'乙','提示说明':'说明'}, {'甲':'甲','乙':''}):
        assert gm.global_merge(Model(invalid),['甲','乙'])=={'乙':'乙','甲':'甲'}
    assert '响应无效' in caplog.text

def test_consolidation_prompt_does_not_repeat_typical_values():
    from tools.attr_stability import global_merge as gm
    hint='看材质；典型取值：木 / 铁；不可包含底座。'
    body=gm._build_body(['材质'],{'材质':hint},hint_mode='hybrid')
    assert body.count('典型取值：')==1
    assert hint in body

def test_voting_stops_when_same_result_has_absolute_majority():
    from tools.attr_stability import global_merge as gm
    class Model:
        def __init__(self):self.calls=0
        def chat_json(self,*a,**kw):self.calls+=1;return {'mapping':{'甲':'甲','乙':'甲'}}
    m=Model(); assert gm.global_merge_vote(m,['甲','乙'])=={'乙':'甲','甲':'甲'}; assert m.calls==2

def test_voting_stops_when_invalid_results_cannot_reach_majority():
    from tools.attr_stability import global_merge as gm
    class Model:
        def __init__(self):self.calls=0
        def chat_json(self,*a,**kw):self.calls+=1;return {'mapping':{'错误':'说明'}}
    m=Model(); assert gm.global_merge_vote(m,['甲','乙'])=={'乙':'乙','甲':'甲'}; assert m.calls==2

def test_boolean_review_does_not_merge_when_independent_review_rejects():
    from steps.step16_attribute_annotation import _review_boolean_duplicates
    attrs=[{'name':'是否慢食','hint':'slow food'}, {'name':'是否慢食设计','hint':'slow drinking'}, {'name':'尺寸'}]
    result,count=_review_boolean_duplicates(attrs,lambda _: [{'name':'是否慢食','aliases':['是否慢食设计']}],verify_fn=lambda _:False)
    assert count==0 and result==attrs

def test_boolean_review_preserves_original_definitions_when_verified():
    from steps.step16_attribute_annotation import _review_boolean_duplicates
    attrs=[{'name':'是否带夜视','hint':'夜视方案A'}, {'name':'是否有夜视','hint':'夜视方案B'}, {'name':'尺寸'}]
    result,count=_review_boolean_duplicates(attrs,lambda _: [{'name':'是否带夜视','aliases':['是否有夜视']}],verify_fn=lambda _:True)
    assert count==1 and '夜视方案A' in result[0]['hint'] and '夜视方案B' in result[0]['hint'] and result[0]['use_full_hint']

def test_boolean_independent_reviewer_requires_complete_checked_ids():
    from types import SimpleNamespace
    from steps.step16_attribute_annotation import Step16AttributeAnnotation
    class Model:
        def __init__(self,answer): self.answer=answer
        def chat_json(self,*a,**kw): return self.answer
    step=Step16AttributeAnnotation()
    attrs=[{'name':'是否防滑','hint':'底部防滑'}, {'name':'是否带防滑','hint':'底部防滑'}]
    for bad in ({'equivalent':True,'checked_ids':['1'],'reason':'一样'},
                {'equivalent':True,'checked_ids':['1','1'],'reason':'一样'},
                {'equivalent':True,'checked_ids':['1','2'],'reason':''},
                {'equivalent':False,'checked_ids':['1','2'],'reason':'不同'}):
        assert not step._verify_boolean_merge(SimpleNamespace(llm=lambda:Model(bad)),attrs)
    ok={'equivalent':True,'checked_ids':['2','1'],'reason':'对象及条件一致'}
    assert step._verify_boolean_merge(SimpleNamespace(llm=lambda:Model(ok)),attrs)

def test_review_invalid_candidate_group_does_not_discard_other_valid_groups():
    from steps.step16_attribute_annotation import _review_boolean_duplicates
    attrs=[{'name':'是否带夜视','hint':'夜视'}, {'name':'是否有夜视','hint':'夜视'},
           {'name':'是否带防水','hint':'防水'}, {'name':'是否防水','hint':'防水'}]
    def merge(group):
        names={a['name'] for a in group}
        if '是否带夜视' in names:return [{'name':'是否带夜视','aliases':[]}]
        return [{'name':'是否防水','aliases':['是否带防水']}]
    result,n=_review_boolean_duplicates(attrs,merge,verify_fn=lambda group:True)
    assert n==1 and {a['name'] for a in result}=={'是否带夜视','是否有夜视','是否防水'}
