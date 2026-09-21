"""Compact source index. Explicit anchors survive renames; legacy text stays untouched."""
import re

MARKER = re.compile(r'^<!-- material: ([A-Za-z0-9_.-]+) -->\s*$', re.M)

def blocks(text):
    matches=list(MARKER.finditer(text))
    ids=[m[1] for m in matches]
    if len(ids)!=len(set(ids)): raise ValueError('物料锚点重复')
    return [{'id':m[1], 'start':m.end(), 'end':matches[n+1].start() if n+1<len(matches) else len(text)} for n,m in enumerate(matches)]

def index(manifest, spec, documents):
    result=[]
    def add(key,title,source,kind,**extra):
        result.append(dict(key=key,title=title,source=source,kind=kind,**extra))
    for module in manifest['modules']:
        add('module:'+module['id'],module['title'],'manifest.json','module')
        for p in module['pages']:
            add('page:'+p['id'],p['title'],p.get('file','spec.json'),'page',page=p['id'])
    for name,body in documents.items():
        title=next((line.lstrip('# ').strip() for line in body.splitlines() if line.startswith('# ')),name)
        add('doc:'+name,title,name,'document')
        for b in blocks(body):
            excerpt=body[b['start']:b['end']].strip()
            add('block:'+name+'#'+b['id'],excerpt.split('\n')[0].lstrip('# ')[:120] or b['id'],name,'block',anchor=b['id'])
        rowids=set()
        for line in body.splitlines():
            match=re.match(r'^\s*\|\s*([A-Za-z][A-Za-z0-9]*-[A-Za-z0-9_.-]+)\s*\|(.+)',line)
            if match:
                if match[1] in rowids: raise ValueError('表格物料编号重复：'+match[1])
                rowids.add(match[1]);add('row:'+name+'#'+match[1],match[2].replace('|',' · ').strip()[:120],name,'table-row',id=match[1])
    for r in (spec or {}).get('requirements',[]):
        add('req:'+r['id'],r['title'],'spec.json','requirement',id=r['id'])
        for group in ('rule_details','acceptance'):
            for item in r.get(group,[]):
                add('item:'+item['id'],item.get('statement',item.get('then',item['id']))[:120],'spec.json',group,id=item['id'],requirement=r['id'])
    for item in (spec or {}).get('interactions',[]):
        add('control:'+item['id'],item['description'],'spec.json','interaction',id=item['id'],page=item['page'],selector=item['selector'])
    return result

def replace(body, anchor, before, after):
    if not isinstance(before,str) or not before or not isinstance(after,str): raise ValueError('需要非空 before 和字符串 after')
    target=next((b for b in blocks(body) if b['id']==anchor),None) if anchor else dict(start=0,end=len(body))
    if target is None: raise ValueError('物料锚点不存在')
    part=body[target['start']:target['end']]
    if part.count(before)!=1: raise ValueError('原文不存在或有多个匹配，请缩小范围；不自动替换全部')
    edited=part.replace(before,after,1)
    result=body[:target['start']]+edited+body[target['end']:]
    if [b['id'] for b in blocks(result)] != [b['id'] for b in blocks(body)]: raise ValueError('局部文字修改不能增删改稳定锚点')
    return result
