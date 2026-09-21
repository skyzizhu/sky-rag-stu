"""Validate project-saved review overlays; never substitute formal rule text."""
import json
import re
import math
import hashlib

def digest(note):
    return hashlib.sha256(json.dumps(note,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest() if note is not None else None

def validate(notes, spec):
    if not isinstance(notes,dict): raise ValueError('项目批注必须按页面登记')
    pages={p['id']:p for p in spec.get('pages',[])}
    for pid,n in notes.items():
        if pid not in pages or not isinstance(n,dict) or n.get('schema_version') not in (1,2) or n.get('page')!=pid: raise ValueError('批注页面/格式错误')
        base={i['id'] for i in spec.get('interactions',[]) if i['page']==pid}
        if any(key not in n for key in ('hidden','overrides','custom')): raise ValueError('批注缺少列表字段')
        for key in ('hidden','overrides','custom','reviews'):
            if not isinstance(n.get(key,[]),list) or len(n.get(key,[]))>1000: raise ValueError('批注列表无效或过大')
        used=set(base)
        for i in n.get('custom',[]):
            if not isinstance(i,dict) or not re.fullmatch(r'manual-[A-Za-z0-9-]{1,113}',i.get('id','')) or i['id'] in used: raise ValueError('手动批注编号错误')
            used.add(i['id'])
            for field in ('selector','description'):
                if not isinstance(i.get(field),str) or not i[field].strip() or len(i[field])>2000: raise ValueError('批注内容错误')
            if i.get('level') not in ('crit','warn','info','ok'): raise ValueError('批注优先级错误')
            if any(not isinstance(i.get(k),(float,int)) or isinstance(i[k],bool) or not math.isfinite(i[k]) or not 0<=i[k]<=1 for k in ('x','y')): raise ValueError('批注坐标错误')
        # Shared annotations cannot hide or override canonical rule markers.
        if n.get('overrides') or any(i not in used-base for i in n.get('hidden',[])): raise ValueError('正式需求标记不可通过共享批注修改或隐藏；请修改 PRD')
        if len(set(n.get('hidden',[])))!=len(n.get('hidden',[])): raise ValueError('删除标记重复')
        seen=set()
        for r in n.get('reviews',[]):
            if not isinstance(r,dict) or r.get('id') not in used or r['id'] in seen or r.get('status') not in ('pending','accepted','rejected','completed'): raise ValueError('批注处理记录错误')
            seen.add(r['id'])
            for key,limit in [('note',2000),('change_ref',200),('evidence',2000),('updated_at',80)]:
                if not isinstance(r.get(key),str) or len(r[key])>limit: raise ValueError('处理字段错误')
            if not r['updated_at'].strip(): raise ValueError('处理时间缺失')
            refs=r.get('requirement_ids')
            if not isinstance(refs,list) or len(refs)>100 or any(x not in pages[pid]['requirement_ids'] for x in refs): raise ValueError('批注需求关联错误')
            if r['status']!='pending' and not r['note'].strip(): raise ValueError('处理说明缺失')
            if r['status'] in ('accepted','completed') and not refs: raise ValueError('采纳需关联需求')
            if r['status']=='completed' and (not r['change_ref'].strip() or not r['evidence'].strip()): raise ValueError('完成缺少修改记录或证据')
            history=r.get('history',[])
            if not isinstance(history,list) or len(history)>30: raise ValueError('处理历史错误')
            for e in history:
                if not isinstance(e,dict) or any(e.get(k) not in ('pending','accepted','rejected','completed') for k in ('from','to')) or any(not isinstance(e.get(k),str) or not e[k].strip() or len(e[k])>limit for k,limit in [('at',80),('note',2000)]): raise ValueError('处理历史字段错误')
    if len(json.dumps(notes,ensure_ascii=False).encode())>1024*1024: raise ValueError('项目批注最多 1MB')
