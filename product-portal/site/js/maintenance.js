/* Portable Agent context; no network, no implicit writes. */
document.addEventListener('DOMContentLoaded',function(){
 var button=document.createElement('button');button.type='button';button.id='material-context';button.textContent='定位并交给 Agent';
 document.querySelector('.topbar').appendChild(button);
 var dialog=document.createElement('dialog');dialog.id='material-dialog';
 dialog.innerHTML='<h2>局部修改物料</h2><p>选择目标，复制上下文并附上修改要求。不会重新生成项目，也不会自动发送给 Agent。</p><label>检索<input id="material-search"></label><label>目标物料<select id="material-target"></select></label><textarea id="material-copy" aria-label="物料修改上下文" readonly rows="9"></textarea><button id="material-select-copy">选中上下文</button><button id="material-close">关闭</button>';
 document.body.appendChild(dialog);var items=[],vid;
 function update(){var item=items.find(function(i){return i.key===dialog.querySelector('select').value;});dialog.querySelector('textarea').value='请按轻量维护模式局部修改，不运行整套生成流程。\n项目：'+window.__PS_PROJECT.project.name+'（'+window.__PS_PROJECT.project.id+'）\n版本：'+vid+'\n当前链接：'+location.href+'\n目标：'+(item?item.title+'\n定位键：'+item.key+'\n来源：'+item.source:'项目元数据或派生视图：请定位其源数据，不直接改汇总HTML')+'\n先找到本地源目录，检查冻结状态和修订指纹；只修改目标及必要关联。\n我的修改要求：';}
 function filter(){var q=dialog.querySelector('input').value.toLowerCase(),select=dialog.querySelector('select');select.replaceChildren();items.filter(function(i){return JSON.stringify(i).toLowerCase().includes(q);}).forEach(function(i){select.add(new Option(i.title+' · '+i.key,i.key));});update();}
 button.onclick=function(){var parts=location.hash.split('?')[0].slice(2).split('/');vid=parts[0]==='v'?decodeURIComponent(parts[1]):window.__PS_PROJECT.project.current_version;var bundle=window.__PS_PROJECT.versions[vid] || {};items=bundle.material_index || [];dialog.querySelector('input').value='';filter();var key='page:'+decodeURIComponent(parts[3] || '');if(parts[2]==='_prd'){var chapter=(bundle.prd_sections || []).find(function(s){return s.id===parts[3];});if(chapter && chapter.sources.length)key='doc:'+chapter.sources[0];}if(items.some(function(i){return i.key===key;}))dialog.querySelector('select').value=key;update();dialog.showModal();};
 dialog.querySelector('input').oninput=filter;dialog.querySelector('select').onchange=update;
 dialog.querySelector('#material-select-copy').onclick=function(){dialog.querySelector('textarea').focus();dialog.querySelector('textarea').select();};
 dialog.querySelector('#material-close').onclick=function(){dialog.close();button.focus();};
});
