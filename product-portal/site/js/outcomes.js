/* Observations are records, never deployment authorization or inferred test passes. */
window.PSOutcomes = {
  render: function (root, data, vid, esc) {
    var all = data.outcomes || [], bundle = data.versions[vid];
    var results = all.filter(function (r) { return r.payload.version === vid; });
    var incoming = all.filter(function (r) { return !r.superseded && r.payload.follow_up && r.payload.follow_up.target_version === vid; });
    var labels = {prototype:'原型验证', product:'真实产品验收', passed:'记录为通过', failed:'失败', blocked:'阻塞', not_run:'未执行', met:'记录为达标', missed:'未达标', inconclusive:'证据不足'};
    root.className = 'content';
    root.innerHTML = '<div class="doc"><h1>验收结果与上线复盘</h1><p>关联版本 '+esc(vid)+'。记录在交付后持续追加，不改写冻结物料；执行人与证据由记录人提供，系统未独立核验，不据此自动放行上线。</p><label>查看范围 <select id="outcome-filter"><option value="all">全部记录</option><option value="prototype">原型验证</option><option value="product">真实产品验收</option><option value="measurement">上线指标</option></select></label><div id="outcome-results"></div><h2>转入本版的跟进事项</h2><div id="outcome-incoming"></div><details><summary>填写结果草稿（导出后正式入库）</summary><p>此处不会直接保存项目。选择类型，按实际证据填写 JSON 后导出；交给 Agent 或执行 record_outcome.py，再构建站点。不要填写未经执行的通过结果。切换类型会重置草稿。</p><select id="outcome-kind"><option value="execution">验收记录</option><option value="measurement">上线指标观测</option></select><textarea id="outcome-draft" rows="18" aria-label="结果 JSON 草稿"></textarea><button id="outcome-export">导出待入库 JSON</button><p id="outcome-draft-status" role="status"></p></details></div>';
    function render() {
      var scope = root.querySelector('#outcome-filter').value;
      var filtered = results.filter(function (r) { return scope === 'all' || r.payload.scope === scope || r.payload.kind === scope; });
      var html = filtered.length ? '' : '<p>尚无结果记录；不能解释为已通过。</p>';
      filtered.forEach(function (r) {
        var p = r.payload;
        html += '<article class="outcome-record"><h3>'+esc(p.kind === 'execution' ? labels[p.scope]+' · '+p.acceptance_id : '指标 · '+p.goal)+'</h3><p>'+esc(labels[p.result || p.decision])+(r.stale ? ' · <strong>需求/物料基线已变化，需复核</strong>' : '')+(r.superseded ? ' · 已被后续记录更正（保留历史）' : '')+'</p><p>'+esc(p.actor+' · '+p.observed_at)+'</p>';
        var acceptance = r.context.acceptance || {};
        var detail = p.kind === 'execution' ? {需求:p.requirement_id,环境:p.environment,产品构建或原型地址:p.build,当时验收:'前提：'+acceptance.given+'；操作：'+acceptance.when+'；预期：'+acceptance.then} : {指标口径:p.metric,基线:p.baseline,目标:p.target,实际:p.actual,观察窗口:p.window,数据来源:p.data_source};
        Object.keys(detail).forEach(function (k) { html += '<p><strong>'+esc(k)+'：</strong>'+esc(detail[k])+'</p>'; });
        html += '<p>'+esc(p.notes)+'</p><p>证据（未自动核验）：'+esc(p.evidence.join('；') || '无执行证据')+'</p><p>记录 ID：'+esc(r.id)+'</p>';
        if (p.follow_up) html += '<p>跟进：<a href="#/v/'+encodeURIComponent(p.follow_up.target_version)+'/_outcomes/results">'+esc(p.follow_up.target_version)+'</a> · '+esc(p.follow_up.owner+' · '+p.follow_up.action)+'</p>';
        html += '</article>';
      });
      if (scope === 'prototype' || scope === 'product') {
        var covered = new Set(results.filter(function (r) { return !r.superseded && !r.stale && r.payload.scope === scope && r.payload.result !== 'not_run'; }).map(function (r) { return r.payload.requirement_id+'/'+r.payload.acceptance_id; }));
        var missing = [];
        ((bundle.spec || {}).requirements || []).forEach(function (r) { (r.acceptance || []).forEach(function (a) { if (!covered.has(r.id+'/'+a.id)) missing.push(r.id+' / '+a.id); }); });
        html = '<p>当前基线下未登记执行记录的验收项：'+esc(missing.join('、') || '无；有记录不代表通过，仍需核对环境、构建和结果。')+'</p>'+html;
      }
      root.querySelector('#outcome-results').innerHTML = html;
    }
    root.querySelector('#outcome-filter').onchange = render; render();
    root.querySelector('#outcome-incoming').innerHTML = incoming.length ? incoming.map(function (r) { var p=r.payload; return '<p><a href="#/v/'+encodeURIComponent(p.version)+'/_outcomes/results">'+esc(p.version)+'</a> · '+esc(p.follow_up.owner+'：'+p.follow_up.action)+' · 来源记录 '+esc(r.id)+(r.stale ? ' · 来源基线已变化，需复核' : '')+'</p>'; }).join('') : '<p>无已登记跟进；不会自动生成或确认新需求。</p>';
    function draft() {
      var kind = root.querySelector('#outcome-kind').value;
      var value = {kind:kind,version:vid,actor:'',observed_at:'',evidence:[],notes:''};
      Object.assign(value, kind === 'execution' ? {requirement_id:'',acceptance_id:'',scope:'product',result:'not_run',environment:'',build:''} : {goal:'',metric:'',baseline:'',target:'',actual:'',window:'',data_source:'',decision:'inconclusive'});
      root.querySelector('#outcome-draft').value = JSON.stringify(value,null,2);
    }
    root.querySelector('#outcome-kind').onchange = draft; draft();
    root.querySelector('#outcome-export').onclick = function () {
      try {
        var value = JSON.parse(root.querySelector('#outcome-draft').value);
        if (!value || value.version !== vid) throw new Error('草稿版本不匹配');
        var blob = new Blob([JSON.stringify(value,null,2)], {type:'application/json'}), url = URL.createObjectURL(blob), link = document.createElement('a');
        link.href=url; link.download='outcome-draft-'+vid+'.json'; link.click(); setTimeout(function () { URL.revokeObjectURL(url); },1000);
        root.querySelector('#outcome-draft-status').textContent='草稿已导出（未入库）。把文件交给 Agent 说「保存验收记录」即可入库；或自行运行 record_outcome.py 后重建站点。';
      } catch (e) { root.querySelector('#outcome-draft-status').textContent=e.message; }
    };
  }
};
