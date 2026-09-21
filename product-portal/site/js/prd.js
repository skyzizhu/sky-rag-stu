/* Business-first reading view; stable identifiers remain available for exact edits. */
window.PSPRD = {render:function (container, data, vid, page, query, esc) {
  var spec=data.versions[vid].spec, reqs=spec.requirements || [], pages=spec.pages || [], interactions=spec.interactions || [];
  var acLatest={};
  (data.outcomes || []).forEach(function (record) {
    var payload=record.payload || {};
    if (payload.kind !== 'execution' || record.superseded || payload.version !== vid || !payload.acceptance_id) return;
    if (!acLatest[payload.acceptance_id] || String(record.recorded_at || '') > String(acLatest[payload.acceptance_id].recorded_at || '')) acLatest[payload.acceptance_id]=record;
  });
  function acBadge(id) {
    var record=acLatest[id]; if (!record) return '';
    var names={passed:'通过',failed:'失败',blocked:'阻塞',not_run:'未执行'};
    var scope=record.payload.scope==='prototype'?'原型验证':'产品验收';
    var stale=record.stale?' · 基线已变化，需复核':'';
    return '<p class="prd-ac-badge'+(record.stale?' stale':'')+'">验证记录：'+esc(names[record.payload.result] || record.payload.result)+'（'+scope+'）· '+esc(String(record.payload.observed_at || '').slice(0,10))+stale+'；取自台账，有记录不代表当前仍通过</p>';
  }
  var category={validation:'输入校验',permission:'权限规则',state:'状态规则',data:'数据口径',feedback:'操作反馈'};
  function p(text) { return '<p>'+esc(text || '')+'</p>'; }
  function meta(item) { return (item.source==='ai-inferred'?'AI 推断':'原始需求')+' · '+(item.status==='confirmed'?'已确认':'待确认')+((item.rule_details || []).some(function(r){return r.status!=='confirmed';})?' · 含待确认规则':''); }
  var listRows=reqs.map(function(r){return '<tr><td>'+esc(r.id)+'</td><td>'+esc(r.title)+'</td><td>'+(r.priority?esc(r.priority):'待定')+'</td><td>'+esc(r.source==='ai-inferred'?'AI 推断':'原始需求')+'</td><td>'+esc(r.status==='confirmed'?'已确认':'待评审')+'</td></tr>';}).join('');
  container.className='content';
  container.innerHTML='<div class="prd-reader"><header><h1>产品需求文档</h1><p>先阅读业务规则与验收；编号用于精确定位，控件技术信息按需展开。</p><div class="prd-tools"><label>页面 <select id="prd-page"><option value="">全部页面</option>'+pages.map(function(x){return '<option value="'+esc(x.id)+'">'+esc(x.title)+'</option>';}).join('')+'</select></label><label>搜索标题、正文或编号 <input id="prd-search" type="search"></label><button id="prd-export">导出完整 PRD（Markdown）</button></div><p id="prd-count" role="status"></p></header><details class="prd-featlist" open><summary>功能清单（由 spec 自动生成；优先级缺失标待定）</summary><table><thead><tr><th>编号</th><th>功能</th><th>优先级</th><th>来源</th><th>状态</th></tr></thead><tbody>' + listRows + '</tbody></table></details><div class="prd-layout"><nav id="prd-toc" aria-label="需求目录"></nav><div id="prd-sections"></div></div><dialog id="prd-edit-context"><h2>交给 Agent 精确修改</h2><p>复制下面的定位信息并补充修改意图；不会自动提交，也不会重新生成项目。</p><textarea rows="7" aria-label="Agent 修改上下文" readonly></textarea><button>关闭</button></dialog></div>';
  function setActive(id){container.querySelectorAll('#prd-toc button').forEach(function(b){b.classList.toggle('on', b.dataset.jump===id);});}
  var spyBound=null;
  function watchSpy(list){
    if(spyBound){spyBound.el.removeEventListener('scroll',spyBound.fn);window.removeEventListener('scroll',spyBound.fn);}
    var scroller=container.closest('.content')||null;
    var fn=function(){
      var best=null,bestTop=-Infinity;
      list.forEach(function(r){var el=document.getElementById('prd-'+r.id);if(!el)return;var top=el.getBoundingClientRect().top;if(top<=150&&top>bestTop){bestTop=top;best=r.id;}});
      setActive(best||(list[0]||{}).id);
    };
    if(scroller)scroller.addEventListener('scroll',fn,{passive:true});
    window.addEventListener('scroll',fn,{passive:true});
    spyBound={el:scroller,fn:fn};
    fn();
  }
  function draw() {
    var pageId=container.querySelector('#prd-page').value, search=container.querySelector('#prd-search').value.toLowerCase();
    var selected=reqs.filter(function(r){return (!pageId || pages.some(function(pg){return pg.id===pageId && pg.requirement_ids.includes(r.id);})) && (!search || JSON.stringify(r).toLowerCase().includes(search) || interactions.some(function(i){return i.requirement_ids.includes(r.id) && JSON.stringify(i).toLowerCase().includes(search);}));});
    container.querySelector('#prd-count').textContent='显示 '+selected.length+' / '+reqs.length+' 条需求；导出始终包含完整文档。';
    container.querySelector('#prd-toc').innerHTML=selected.map(function(r){return '<button data-jump="'+esc(r.id)+'">'+esc(r.title)+'</button>';}).join('');
    container.querySelector('#prd-sections').innerHTML=selected.map(function(r){
      var html='<article class="prd-requirement" id="prd-'+esc(r.id)+'"><header><h2>'+esc(r.title)+'</h2><p class="prd-meta">'+esc(r.id+' · '+meta(r)+(r.blocking?' · 阻塞开发':''))+'</p><button data-edit="'+esc(r.id)+'">交给 Agent 修改此需求</button></header>'+p(r.description);
      var ruleKeys=Object.keys(r.rules || {});
      if(ruleKeys.length){html+='<h3>业务规则</h3>';ruleKeys.forEach(function(k){html+='<section class="prd-rule"><h4>'+esc(k)+'</h4>'+p(r.rules[k])+'</section>';});}
      (r.rule_details || []).forEach(function(rule){html+='<section class="prd-rule"><h4>'+esc(category[rule.category] || rule.category)+'</h4>'+p(rule.statement)+'<p class="prd-meta">'+esc(rule.id+' · '+meta(rule))+'</p>'; (rule.examples || []).forEach(function(e){html+=p('例：'+e.input+' → '+e.expected);}); html+='</section>';});
      html+='<h3>验收标准</h3><ol class="prd-acceptance">';
      (r.acceptance || []).forEach(function(a){html+='<li>'+p('前提：'+a.given)+p('操作：'+a.when)+p('预期：'+a.then)+acBadge(a.id)+'<small>'+esc(a.id)+'</small></li>';});
      html+='</ol><h3>页面与控件</h3>';
      interactions.filter(function(i){return i.requirement_ids.includes(r.id);}).forEach(function(i){var pg=pages.find(function(x){return x.id===i.page;}); html+='<details class="prd-control"><summary>'+esc((pg?pg.title:i.page)+' · '+i.description)+'</summary><p class="prd-meta">'+esc(i.id+' · '+i.selector)+'</p>'+p('动作：'+i.action+(i.target?' → '+i.target:'')); Object.keys(i.properties || {}).forEach(function(k){html+=p(k+'：'+JSON.stringify(i.properties[k]));}); html+='<button data-edit="'+esc(r.id)+'" data-control="'+esc(i.id)+'">交给 Agent 修改此控件</button></details>';});
      return html+'</article>';
    }).join('') || '<p>没有匹配需求，请调整筛选。编号不存在时不会自动定位到其他需求。</p>';
    watchSpy(selected);
    setActive((selected[0] || {}).id);
  }
  container.querySelector('#prd-search').value=new URLSearchParams(query).get('requirement') || '';
  ['prd-page','prd-search'].forEach(function(id){container.querySelector('#'+id).addEventListener('input',draw);});
  container.onclick=function(event){
    var jump=event.target.closest('[data-jump]'); if(jump){var target=document.getElementById('prd-'+jump.dataset.jump); if(target){target.scrollIntoView({block:'start'});setActive(jump.dataset.jump);}}
    var edit=event.target.closest('[data-edit]'); if(!edit)return;
    var r=reqs.find(function(x){return x.id===edit.dataset.edit;});
    var dialog=container.querySelector('#prd-edit-context');
    dialog.querySelector('textarea').value='请在既有项目中增量修改，不重新生成。\n页面入口：'+location.href.split('?')[0]+'?requirement='+encodeURIComponent(r.id)+'\n版本：'+vid+'\n需求：'+r.title+'（'+r.id+'）'+(edit.dataset.control?'\n控件：'+edit.dataset.control:'')+'\n修改要求：【请填写】\n保留其他需求、编号、页面结构及历史；若此版已冻结，请先定位对应规划版，不修改冻结物料。';
    dialog.showModal();dialog.querySelector('textarea').select();
  };
  container.querySelector('#prd-edit-context button').onclick=function(){container.querySelector('#prd-edit-context').close();};
  container.querySelector('#prd-export').onclick=function(){var md=data.versions[vid].full_prd_markdown || data.versions[vid].files[page.id] || '';var url=URL.createObjectURL(new Blob([md],{type:'text/markdown;charset=utf-8'}));var a=document.createElement('a');a.href=url;a.download=data.project.name+'-'+vid+'-产品需求文档.md';a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);};
  draw();
}};
