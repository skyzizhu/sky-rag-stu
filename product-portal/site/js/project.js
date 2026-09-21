/* Version-aware shell. One fixed entry, stable version/module/page routes. */
var PS = window.PS || {};
PS.bootProject = function () {
  var data = window.__PS_PROJECT, project = data.project, serial = 0;
  var labels = {planning: '规划中', frozen: '已冻结', developing: '开发中', released: '已发布'};
  var esc = PS.escapeHtml;
  var content = document.getElementById('content');
  document.body.classList.add('project-shell');
  document.getElementById('brand-name').textContent = project.name;
  document.getElementById('brand-meta').textContent = '项目固定入口 · 当前工作版本 ' + project.current_version;
  var nav = document.getElementById('menu');
  function link(vid, mid, pid) { return '#/v/' + [vid, mid, pid].map(encodeURIComponent).join('/'); }
  var html = '<a class="menu-item project-home" href="#/project">项目总览与版本计划</a>';
  var sortedVersions = project.versions.slice().sort(function (a, b) {
    return a.id.localeCompare(b.id, undefined, { numeric: true, sensitivity: 'base' });
  });
  sortedVersions.forEach(function (v) {
    if (v.handoff_parent) return;
    html += '<details class="version-group" data-version="' + esc(v.id) + '"><summary><span class="version-label">' + esc(v.id + ' · ' + v.title) + '</span>' +
      ' <span class="version-status">' + labels[v.status] + '</span></summary>' +
      '<details class="menu-group" data-scope-group><summary class="menu-title">版本与需求范围</summary>' +
      '<a class="menu-item" href="' + link(v.id, '_changes', 'report') + '">本版变更与待确认</a>' +
      '<a class="menu-item" href="'+link(v.id,'_delivery','tasks')+'">需求范围与交接</a>' +
      '</details>';
    var handoffs = project.versions.filter(function (h) { return h.handoff_parent === v.id; });
    if (handoffs.length) html += '<details class="menu-group"><summary>交接修订</summary>' + handoffs.map(function (h) { return '<a class="menu-item" href="' + link(h.id, '_changes', 'report') + '">' + esc(h.revision + (v.current_handoff === h.id ? ' · 当前开发依据' : ' · 历史交接')) + '</a>'; }).join('') + '</details>';
    var standard = data.versions[v.id].prd_sections;
    var prdSources = standard ? standard.reduce(function(all,s){return all.concat(s.sources);},[]) : [];
    var requirementExtras=[];
    if(standard) data.versions[v.id].manifest.modules.forEach(function (m) {
      if (m.special || m.id !== 'requirements') return;
      m.pages.forEach(function (p) { if (p.type !== 'spec' && !prdSources.includes(p.file)) requirementExtras.push(p); });
    });
    if(standard) {
      var chapterLinks = standard.map(function(s){return '<a class="menu-item page-link'+(s.id==='functions'?' key-material':'')+'" href="'+link(v.id,'_prd',s.id)+'">'+esc(s.title)+'<small class="prototype-page-id">'+({provided:'已提供',pending:'待补充',not_applicable:'不适用'}[s.status])+'</small></a>';});
      var idxExtras = [], otherExtras = [];
      requirementExtras.forEach(function (p) {
        var linkHtml = '<a class="menu-item page-link key-material" aria-label="' + esc(p.title) + '" title="' + esc(p.title) + '" href="' + link(v.id, 'requirements', p.id) + '">' + esc(p.title) + '</a>';
        (('/' + p.id + (p.file || '')).toLowerCase().includes('index') ? idxExtras : otherExtras).push(linkHtml);
      });
      var fnAt = standard.findIndex(function (s) { return s.id === 'functions'; });
      var at = fnAt < 0 ? chapterLinks.length : fnAt;
      chapterLinks.splice.apply(chapterLinks, [at, 0].concat(idxExtras));
      chapterLinks.splice.apply(chapterLinks, [at + idxExtras.length, 0].concat(otherExtras));
      html += '<details class="menu-group" data-module="standard-prd" open><summary class="menu-title">需求文档</summary>' + chapterLinks.join('') + '</details>';
    }
    data.versions[v.id].manifest.modules.forEach(function (m) {
      if (m.special) return;
      var visiblePages=m.pages.filter(function(p){return !standard || p.type!=='spec' && !prdSources.includes(p.file);});
      if(!visiblePages.length)return;
      if(standard && m.id === 'requirements') return;

      html += '<details class="menu-group" data-module="' + esc(m.id) + '" open><summary class="menu-title">' + esc(m.title) + '</summary>';
      visiblePages.forEach(function (p) {
        var key = (!standard && p.type === 'spec') || /index/i.test(p.id + (p.file || ''));
        html += '<a class="menu-item page-link' + (key ? ' key-material' : '') + '" aria-label="' + esc(p.title) + '" title="' + esc(p.title + (p.type === 'prototype' ? ' · 页面编号：' + p.id : '')) + '" href="' + link(v.id, m.id, p.id) + '">' + esc(p.title) + (p.type === 'prototype' ? '<small class="prototype-page-id">页面 · ' + esc(p.id) + '</small>' : '') + '</a>';
      });
      html += '</details>';
    });
    html += '<a class="menu-item" href="'+link(v.id,'_outcomes','results')+'">验收结果与上线复盘</a>';
    html += '</details>';
  });
  nav.innerHTML = html;
  var actions = document.createElement('div');
  actions.className = 'project-actions';
  actions.innerHTML = '<span id="version-context"></span><a id="project-open" target="_blank" rel="noopener" hidden>新窗口打开 ↗</a><button id="project-focus" type="button" aria-pressed="false">专注查看</button>';
  document.querySelector('.topbar').appendChild(actions);
  var focusButton = document.getElementById('project-focus');
  function setFocus(on) {
    document.body.classList.toggle('project-focus', on);
    focusButton.textContent = on ? '退出专注' : '专注查看';
    focusButton.setAttribute('aria-pressed', String(on));
  }
  focusButton.addEventListener('click', function () { setFocus(!document.body.classList.contains('project-focus')); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setFocus(false); });
  var memory = 'ps.' + project.id + '.versions';
  var saved = {};
  try { saved = JSON.parse(localStorage.getItem(memory) || '{}'); } catch (_) {}
  nav.querySelectorAll('.version-group').forEach(function (g) {
    g.open = saved[g.dataset.version] === undefined ? g.dataset.version === project.current_version : saved[g.dataset.version];
    g.addEventListener('toggle', function () {
      saved[g.dataset.version] = g.open;
      try { localStorage.setItem(memory, JSON.stringify(saved)); } catch (_) {}
    });
  });
  function diffLines(oldText, newText) {
    var a = String(oldText == null ? '' : oldText).split('\n');
    var b = String(newText == null ? '' : newText).split('\n');
    var n = a.length, m = b.length;
    if (n * m > 1500000) return null;
    var dp = [];
    for (var i = 0; i <= n; i++) { dp.push(new Array(m + 1).fill(0)); }
    for (var i = n - 1; i >= 0; i--) {
      for (var j = m - 1; j >= 0; j--) {
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i][j + 1], dp[i + 1][j]);
      }
    }
    var rows = [], i = 0, j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) { rows.push([' ', a[i]]); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push(['-', a[i]]); i++; }
      else { rows.push(['+', b[j]]); j++; }
    }
    while (i < n) { rows.push(['-', a[i]]); i++; }
    while (j < m) { rows.push(['+', b[j]]); j++; }
    return rows;
  }
  function textDiffs(v, data) {
    var base = v.base && data.versions[v.base];
    if (!base) return '<p>首版无基线可比较。</p>';
    var current = data.versions[v.id];
    var keys = Object.keys(current.files || {}).filter(function (k) {
      return Object.prototype.hasOwnProperty.call(base.files || {}, k) && base.files[k] !== current.files[k];
    }).sort();
    if (!keys.length) return '<p>正文文件无文本差异；其余变化见上方结构化需求差异与 changed_files。</p>';
    return keys.map(function (k) {
      var rows = diffLines(base.files[k], current.files[k]);
      var body;
      if (!rows) {
        body = '<p>文件过大，差异未计算，请人工比对。</p>';
      } else if (rows.length > 1500) {
        var add = rows.filter(function (r) { return r[0] === '+'; }).length;
        var del = rows.filter(function (r) { return r[0] === '-'; }).length;
        body = '<p>差异行数过多（' + rows.length + ' 行；新增 ' + add + ' / 删除 ' + del + '），请人工比对。</p>';
      } else {
        body = rows.map(function (r) {
          var cls = r[0] === ' ' ? 'ctx' : r[0] === '+' ? 'add' : 'del';
          return '<span class="diff-' + cls + '">' + esc(r[0] + ' ' + r[1]) + '</span>';
        }).join('');
      }
      return '<details class="ps-diff"><summary>' + esc(k) + '</summary><div class="diff-body">' + body + '</div></details>';
    }).join('');
  }
  function changesHtml(v, bundle) {
    var base = v.base && data.versions[v.base];
    var old = {}, current = {};
    ((base && base.spec && base.spec.requirements) || []).forEach(function (r) { old[r.id] = r; });
    ((bundle.spec && bundle.spec.requirements) || []).forEach(function (r) { current[r.id] = r; });
    var result = '<h1>本版变更与待确认</h1><p>基于 ' + esc(v.base || '首个版本') + ' · ' + labels[v.status] + '</p>';
    var changed = Array.from(new Set(Object.keys(old).concat(Object.keys(current)))).filter(function (id) {
      return JSON.stringify(old[id]) !== JSON.stringify(current[id]);
    });
    result += '<h2>需求差异</h2>' + (changed.length ? '<ul>' + changed.map(function (id) {
      return '<li>' + (!old[id] ? '新增' : !current[id] ? '移除' : '修改') + ' · ' + esc(id + ' ' + (current[id] || old[id]).title) + '</li>';
    }).join('') + '</ul>' : '<p>没有结构化需求变化；范围、背景及其他文档变更请查看本版概览。</p>');
    result += '<h2>正文变更（行级差异）</h2>' + textDiffs(v, data);
    result += '<h2>待确认需求</h2>';
    var pending = Object.values(current).filter(function (r) { return r.status === 'pending'; });
    result += pending.length ? '<ul>' + pending.map(function (r) { return '<li>' + esc(r.id + ' · ' + r.title) + (r.blocking ? ' · 阻塞开发' : ' · 可后补') + '</li>'; }).join('') + '</ul>' : '<p>没有待确认的结构化需求。</p>';
    result += '<h2>待确认文档</h2><ul>';
    bundle.manifest.modules.forEach(function (m) { (m.pages || []).filter(function (p) { return p.status === 'pending'; }).forEach(function (p) {
      result += '<li><a href="' + link(v.id, m.id, p.id) + '">' + esc(p.title) + '</a></li>';
    }); });
    return result + '</ul>';
  }
  async function route() {
    var turn = ++serial;
    var parts;
    var routeHash = location.hash.split('?')[0];
    var replayQuery = location.hash.includes('?') ? location.hash.slice(location.hash.indexOf('?') + 1) : '';
    try { parts = routeHash.slice(2).split('/').map(decodeURIComponent); } catch (_) { parts = []; }
    content.className = 'content';
    document.getElementById('page-badges').textContent = '';
    document.getElementById('version-context').textContent = '';
    document.getElementById('project-open').hidden = true;
    nav.querySelectorAll('a').forEach(function (a) {
      var active = a.hash === routeHash;
      a.classList.toggle('active', active);
      if (active) { a.setAttribute('aria-current', 'page'); var parent = a.closest('.menu-group'); if (parent) parent.open = true; }
      else a.removeAttribute('aria-current');
    });
    if (parts[0] === 'project' || !parts[0]) {
      document.getElementById('breadcrumb').textContent = project.name + ' / 项目总览';
      document.title = project.name + ' · 产品方案';
      content.innerHTML = '<div class="doc"><h1>' + esc(project.name) + '</h1><p>' + esc(project.summary || '在同一入口查看项目版本、交付范围与历史物料。') + '</p><div class="project-cards">' + project.versions.filter(function (v) { return !v.handoff_parent; }).map(function (v) {
        return '<article class="project-card"><h2><a href="' + link(v.id, '_changes', 'report') + '">' + esc(v.id + ' · ' + v.title) + '</a></h2><p>' + labels[v.status] + (v.id === project.current_version ? ' · 当前工作版本' : '') + '</p><p>' + esc(v.goal || '') + '</p></article>';
      }).join('') + '</div></div>';
      return;
    }
    // Preserve old #/module/page links after migrating a single-version project.
    if (parts[0] !== 'v') { location.hash = link(project.current_version, parts[0], parts[1] || 'index'); return; }
    var v = project.versions.find(function (item) { return item.id === parts[1]; });
    if (!v) { content.innerHTML = '<div class="doc"><h1>版本不存在</h1><a href="#/project">返回项目总览</a></div>'; return; }
    var group = Array.from(nav.querySelectorAll('.version-group')).find(function (g) { return g.dataset.version === v.id; });
    if (!group) group = Array.from(nav.querySelectorAll('.version-group')).find(function (g) { return g.dataset.version === v.handoff_parent; });
    if (group) group.open = true;
    var bundle = data.versions[v.id];
    document.getElementById('breadcrumb').textContent = project.name + ' / ' + v.id + ' · ' + labels[v.status];
    var context = document.getElementById('version-context');
    context.className = v.id !== project.current_version ? 'project-history' : 'project-current';
    context.textContent = v.handoff_parent ? v.handoff_parent + ' · 交接 ' + v.revision : v.current_handoff ? '工作草稿 · 开发依据 ' + v.current_handoff : v.id !== project.current_version ? '历史版本 ' + v.id : '当前版本';
    if (v.handoff_parent) context.textContent += project.versions.find(function (parent) { return parent.id === v.handoff_parent; }).current_handoff === v.id ? ' · 当前开发依据' : ' · 历史交接';
    context.title = '当前工作版本：' + project.current_version;
    var banner = '';
    if (parts[2] === '_prd') { document.getElementById('breadcrumb').textContent += ' / 需求文档'; window.PSStandardPRD.render(content,data,v.id,parts[3],replayQuery,esc); return; }
    if (parts[2] === '_delivery') { window.PSDelivery.render(content,data,v.id,esc); return; }
    if (parts[2] === '_outcomes') { window.PSOutcomes.render(content,data,v.id,esc); return; }
    if (v.handoff_parent && parts[2] === '_changes') banner = '<p><a href="'+link(v.id,'_delivery','tasks')+'">需求范围与交接</a> · <a href="'+link(v.id,'_outcomes','results')+'">验收结果与复盘</a></p>';
    if(v.handoff_parent && parts[2]==='_changes' && bundle.prd_sections) banner += '<p>'+bundle.prd_sections.map(function(s){return '<a href="'+link(v.id,'_prd',s.id)+'">'+esc(s.title)+'</a>';}).join(' · ')+'</p>';
    if (parts[2] === '_changes') { content.innerHTML = '<div class="doc">' + banner + changesHtml(v, bundle) + (v.handoff_parent ? '<h2>本次交接物料</h2>' + bundle.manifest.modules.map(function (m) { return '<h3>' + esc(m.title) + '</h3>' + m.pages.map(function (p) { return '<p><a href="' + link(v.id,m.id,p.id) + '">' + esc(p.title) + '</a></p>'; }).join(''); }).join('') : '') + '</div>'; return; }
    var mod = bundle.manifest.modules.find(function (m) { return m.id === parts[2]; });
    var page = mod && mod.pages.find(function (p) { return p.id === parts[3]; });
    if (!page) { content.innerHTML = '<div class="doc"><h1>页面不存在</h1><p>请从左侧选择本版本页面。</p></div>'; return; }
    document.title = page.title + ' · ' + v.id + ' · ' + project.name;
    if(bundle.prd_sections && (mod.id==='testing' || mod.id==='launch')) banner += '<p class="chapter-status">规则来源：<a href="'+link(v.id,'_prd','acceptance')+'">PRD · 验收与上线条件</a>；这里展示执行步骤/记录，不维护第二份验收口径。</p>';
    document.getElementById('breadcrumb').textContent += ' / ' + page.title + (page.type === 'prototype' ? ' · 页面编号：' + page.id : '');
    document.getElementById('page-badges').innerHTML = PS.badgesHtml(page);
    document.getElementById('breadcrumb').title = document.getElementById('breadcrumb').textContent;
    if (page.type === 'spec' && bundle.spec) { window.PSPRD.render(content,data,v.id,page,replayQuery,esc); return; }
    function url(p) { return '../versions/' + v.id + '/content/' + p.file + (p.id === page.id && p.type === 'prototype' && replayQuery ? '#' + replayQuery : ''); }
    function pageHtml(p) {
      var embedded = p.type === 'prototype' || p.type === 'html-embed';
      var body = embedded ? '' : '<h2>' + esc(p.title) + '</h2>';
      if (p.type === 'markdown' || p.type === 'spec') {
        body += PS.mdToHtml(bundle.files[p.type === 'spec' ? p.id : p.file] || '').replace(/\.\.\/content\//g, '../versions/' + v.id + '/content/');
      } else if (p.type === 'mermaid') body += '<div class="mermaid-inline">' + esc(bundle.files[p.file]) + '</div>';
      else body += '<iframe class="embed" title="' + esc(p.title) + '" src="' + esc(url(p)) + '"></iframe>';
      return body;
    }
    if (mod.layout === 'continuous') {
      content.innerHTML = '<div class="doc">' + banner + mod.pages.map(function (p) { return '<section class="project-section" id="section-' + esc(p.id) + '">' + pageHtml(p) + '</section>'; }).join('') + '</div>';
    } else {
      if (page.type === 'prototype' || page.type === 'html-embed') content.className = 'content embed-mode';
      content.innerHTML = (content.classList.contains('embed-mode') ? banner + pageHtml(page) : '<div class="doc">' + banner + pageHtml(page) + '</div>');
    }
    if (page.type === 'prototype' || page.type === 'html-embed') {
      var openLink = document.getElementById('project-open'); openLink.href = url(page); openLink.hidden = false;
    }
    var nodes = Array.from(content.querySelectorAll('.mermaid-inline'));
    for (var node of nodes) { if (turn !== serial) return; await PS.renderMermaid(node, node.textContent); }
    if (turn === serial && mod.layout === 'continuous') document.getElementById('section-' + page.id).scrollIntoView();
    else if (turn === serial) content.scrollTop = 0;
  }
  window.addEventListener('hashchange', route);
  route();
};
