/* Nine standard chapters reference canonical sources; static mode never pretends to upload. */
window.PSStandardPRD = {
  render: function(container,data,vid,cid,query,esc) {
    var bundle=data.versions[vid], sections=bundle.prd_sections || [], section=sections.find(function(s){return s.id===cid;});
    if(!section){container.innerHTML='<div class="doc"><h1>此版本未登记该标准章节</h1><p>请在规划版补充，旧版本不会自动重生成。</p></div>';return;}
    if(cid==='functions' && bundle.spec){
      var specPage;
      bundle.manifest.modules.forEach(function(m){m.pages.forEach(function(p){if(p.type==='spec' && !specPage)specPage=p;});});
      window.PSPRD.render(container,data,vid,specPage || {id:'spec'},query,esc);return;
    }
    var labels={provided:'已提供（不代表需求已确认）',pending:'待补充',not_applicable:'不适用'};
    container.className='content';
    container.innerHTML='<div class="doc standard-prd"><header><h1>'+esc(section.title)+'</h1><p class="chapter-status">'+esc(labels[section.status]+' · '+section.reason)+'</p><button id="standard-prd-export">导出完整 PRD（Markdown）</button></header><div>'+window.PS.mdToHtml(section.markdown)+'</div>'+(cid==='appendix'?'<h2>文件附件</h2><p>静态查阅模式：可下载已保存文件。需要上传时，请通过 Agent 启动本机 PRD 编辑器，在“附录 · 文件附件”中操作；此页面不会假装把文件保存到磁盘。</p><div id="standard-attachments"></div>':'')+'</div>';
    var repeated=container.querySelector('.standard-prd > div > h1:first-child');
    if(repeated && repeated.textContent===section.title)repeated.remove();
    if(cid==='appendix'){
      var target=container.querySelector('#standard-attachments'), attachments=bundle.attachments || [];
      target.innerHTML=attachments.map(function(a){return '<article class="attachment-item"><h3><a download="'+esc(a.name)+'" href="../'+esc(a.relative_path)+'">'+esc(a.name)+'</a></h3><p>'+esc(a.description)+'</p><p>'+esc(a.size+' 字节 · '+a.created_at+' · '+(a.late_addition?'冻结后补充（不属于原交接快照）':'版本附件引用'))+'</p><p>关联需求：'+esc(a.requirement_ids.join('、') || '未指定')+'</p></article>';}).join('') || '<p>尚无附件。</p>';
    }
    container.querySelector('#standard-prd-export').onclick=function(){var url=URL.createObjectURL(new Blob([bundle.full_prd_markdown || ''],{type:'text/markdown;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download=data.project.name+'-'+vid+'-完整PRD.md';a.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);};
  }
};
