/* 运营助手 · 前端（零依赖 vanilla JS） */

const S = { page:'overview', overview:null, projects:[], candidates:[], assets:null };

/* ── 基础工具 ── */
const $  = s => document.querySelector(s);
const esc = s => String(s??'').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => (n??0).toLocaleString('zh-CN');
const bytes = n => n<1024?n+'B':n<1048576?(n/1024).toFixed(0)+'KB':(n/1048576).toFixed(1)+'MB';

async function api(path, body, method){
  // method 的推断规则：
  //   · 显式传了 method → 用它（删除等场景）
  //   · 有 body        → POST
  //   · 没有 body      → GET      ← 早期把这里默认成 POST，导致所有列表请求
  //                                都变成了"创建"，页面直接崩掉
  const m = method || ((body !== undefined && body !== null) ? 'POST' : 'GET');
  const opt = {method: m};
  if(body !== undefined && body !== null){
    opt.headers = {'Content-Type':'application/json'};
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const j = await r.json().catch(()=>({error:'响应不是 JSON'}));
  if(!r.ok || j.error) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}
let toastT;
function toast(msg, ms=2400){
  const t=$('#toast'); t.textContent=msg; t.classList.remove('hidden');
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.add('hidden'), ms);
}
function openDrawer(title, html){
  $('#drawerTitle').textContent = title;
  $('#drawerBody').innerHTML = html;
  $('#drawer').classList.remove('hidden');
}
function closeDrawer(){ $('#drawer').classList.add('hidden'); }
$('#drawer').addEventListener('click', e=>{ if(e.target.id==='drawer') closeDrawer(); });

/* ── 路由 ── */
const PAGES = {
  overview:{title:'总览',      render:renderOverview, actions:()=>''},
  topics:  {title:'选题',      render:renderTopics,   actions:()=>
    `<button class="primary" onclick="genTopics()">生成选题候选</button>`},
  projects:{title:'创作',      render:renderProjects, actions:()=>
    `<button onclick="newProject()">＋ 新建项目</button>`},
  assets:  {title:'素材库',    render:renderAssets,   actions:()=>
    `<button onclick="rescanAssets()">重新扫描目录</button>`},
  metrics: {title:'数据',      render:renderMetrics,  actions:()=>''},
  sop:     {title:'创作流程 SOP', render:renderSop, actions:()=>
    `<button onclick="renderSop()">刷新</button>`},
};

async function refreshSidebar(){
  // 侧栏统计要**每次进页面都刷新** —— 之前只在「总览」里更新，
  // 深链接直接进别的页面就会一直显示占位符「— 个项目」。
  try{
    const o = S.overview || await api('/api/overview');
    S.overview = o;
    $('#statProjects').textContent = o.projects + ' 个项目';
    $('#statAssets').textContent   = o.assets.present + ' 个素材';
  }catch(e){ /* 静默：统计不影响主流程 */ }
}

async function go(page, push=true, deepId){
  if(!PAGES[page]) page = 'overview';
  S.page = page;
  refreshSidebar();
  if(push && location.hash.slice(1) !== page) location.hash = page;
  // deepId 走**显式参数**，别在这里清 S.deepId（会把调用方刚设的值抹掉）
  if(deepId !== undefined) S.deepId = deepId;
  document.querySelectorAll('#nav a').forEach(a =>
    a.classList.toggle('active', a.dataset.page===page));
  $('#pageTitle').textContent = PAGES[page].title;
  $('#pageActions').innerHTML = PAGES[page].actions?.() || '';
  $('#view').innerHTML = '<div class="loading">载入中…</div>';
  try { await PAGES[page].render(); }
  catch(e){ $('#view').innerHTML = `<div class="empty"><b>载入失败</b>${esc(e.message)}</div>`; }
  if(S.deepId){ try{ await openProject(S.deepId); }catch(e){} }
}
document.querySelectorAll('#nav a').forEach(a =>
  a.addEventListener('click', ()=>go(a.dataset.page, true, '')));
function parseHash(){
  const raw = location.hash.slice(1) || 'overview';
  const [page, param] = raw.split('/');
  return [page || 'overview', param || ''];
}
window.addEventListener('hashchange', ()=>{
  const [p, param] = parseHash();
  if(p !== S.page || param) go(p, false, param);
});

/* ── 总览 ── */
async function renderOverview(){
  const [o, trash] = await Promise.all([
    api('/api/overview'),
    api('/api/trash').catch(()=>({count:0, bytes:0}))]);
  S.overview = o;
  $('#statProjects').textContent = o.projects + ' 个项目';
  $('#statAssets').textContent   = o.assets.present + ' 个素材';

  const stages = Object.entries(o.stages||{}).map(([k,v])=>
    `<span class="tag">${esc(o.stage_labels[k]||k)} ${v}</span>`).join('') ||
    '<span class="hint">还没有项目</span>';

  const vts = Object.entries(o.video_templates||{});
  const perf = (o.performance||[]).length
    ? o.performance.map(p=>`<tr><td>${esc(p.name)}</td><td class="num">${p.n}</td>
        <td class="num">${fmt(p.views)}</td><td class="num">${p.rate.toFixed(1)}%</td></tr>`).join('')
    : `<tr><td colspan="4" class="hint" style="padding:14px">
         还没有数据。发布后用数据采集回填，这里就会显示哪种选题结构有效。</td></tr>`;

  $('#view').innerHTML = `
  <div class="grid g4" style="margin-bottom:16px">
    <div class="card"><div class="stat">${o.projects}</div>
      <div class="statlabel">内容项目</div></div>
    <div class="card"><div class="stat">${o.assets.present}</div>
      <div class="statlabel">在库素材 · ${o.assets.books} 个书目</div></div>
    <div class="card"><div class="stat">${Object.keys(o.templates||{}).length}</div>
      <div class="statlabel">图文模板</div></div>
    <div class="card"><div class="stat">${Object.keys(o.styles||{}).length}</div>
      <div class="statlabel">封面风格</div></div>
  </div>

  ${trash.count?`
  <div class="card" style="margin-bottom:16px;border-left:4px solid var(--warn)">
    <div class="flexrow">
      <div>
        <h3 style="margin:0">回收站有 ${trash.count} 项 / ${bytes(trash.bytes)}</h3>
        <div class="hint" style="margin-top:4px">
          删除的项目先放这里，误删可以恢复。确认不需要再清空。</div>
      </div>
      <div class="flexrow">
        <button class="sm" onclick="showTrash()">查看 / 恢复</button>
        <button class="danger sm" onclick="emptyTrash()">清空</button>
      </div>
    </div>
  </div>`:''}

  <div class="grid g2">
    <div class="card">
      <h3>项目阶段分布</h3>
      <div class="sub">内容从选题到数据回流的流转情况</div>
      <div style="line-height:2.2">${stages}</div>
    </div>
    <div class="card">
      <h3>选题结构表现</h3>
      <div class="sub">数据回流后，引擎会自动给表现好的结构加权</div>
      <table><thead><tr><th>结构</th><th class="num">条数</th>
        <th class="num">总浏览</th><th class="num">互动率</th></tr></thead>
        <tbody>${perf}</tbody></table>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h3>视频模板</h3>
    <div class="sub">在「创作」里选模板后一键生产</div>
    <div class="grid g3" style="gap:10px">
      ${vts.map(([k,v])=>`<div style="padding:10px 12px;border:1px solid var(--line);
        border-radius:8px;background:#fffdf9">
        <div style="font-weight:600;font-size:13px">${esc(v.name)}
          ${v.intro?'<span class="tag red" style="font-size:10px">片头</span>':''}
          ${v.outro?'<span class="tag" style="font-size:10px">片尾</span>':''}</div>
        <div class="hint" style="margin-top:4px">${esc(v.desc)}</div>
        <div class="hint" style="margin-top:4px;font-size:11px">转场 ${esc(v.transition)} · 运镜 ${esc(v.motion)}</div>
      </div>`).join('')}
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h3>工作流</h3>
    <div class="sub">五步闭环 —— 只有「填要点」和「点发布」需要你动手</div>
    <div class="flexrow" style="gap:8px;margin-top:6px">
      <span class="tag red serif">① 选题</span><span class="hint">→</span>
      <span class="tag red serif">② 建项目</span><span class="hint">→</span>
      <span class="tag red serif">③ 填要点</span><span class="hint">→</span>
      <span class="tag red serif">④ 一键生产</span><span class="hint">→</span>
      <span class="tag red serif">⑤ 数据回流</span>
    </div>
    <div class="sep"></div>
    <div class="hint">
      <b>工作区</b> ${esc(o.workspace)}<br>
      <b>素材库</b> ${esc(o.assets_dir)}<br>
      本工具只准备内容，<b>不触碰任何平台</b>。发布由你手动完成。
    </div>
  </div>`;
}

/* ── 选题 ── */
async function genTopics(){
  const niche = prompt('内容领域？', 'AI×国学');
  if(niche===null) return;
  $('#view').innerHTML = '<div class="loading">正在基于素材库生成候选…</div>';
  try{
    const r = await api('/api/topics', {niche, n:10});
    S.candidates = r.candidates;
    renderTopics();
    toast(`生成 ${r.candidates.length} 条候选`);
  }catch(e){ toast('生成失败：'+e.message, 3600); renderTopics(); }
}

function renderTopics(){
  if(!S.candidates.length){
    $('#view').innerHTML = `<div class="empty"><b>还没有候选选题</b>
      点右上角「生成选题候选」。引擎会结合你的素材库、
      6 种验证过的选题结构、以及历史数据表现来生成。</div>`;
    return;
  }
  $('#view').innerHTML = `<div class="grid g2">${S.candidates.map((c,i)=>`
    <div class="tcard">
      <div class="meta">
        <span class="score">${c.score.toFixed(2)}</span>
        <span class="tag red">${esc(c.pattern_name)}</span>
        ${c.assets.length?`<span class="tag ok">素材 ${c.assets.length}</span>`
          :`<span class="tag warn">需自备素材</span>`}
      </div>
      <div class="ttl">${esc(c.title)}</div>
      <div class="why">${esc(c.why)}</div>
      <div class="needs"><b>需要你提供：</b><br>
        ${c.needs.map(n=>'· '+esc(n)).join('<br>')}</div>
      <div class="foot">
        <span class="hint">适合 ${c.platforms.map(p=>esc(
          (S.overview?.platforms||{})[p]||p)).join('、')}</span>
        <button class="primary sm" style="margin-left:auto"
          onclick="pickTopic(${i})">建为项目</button>
      </div>
    </div>`).join('')}</div>`;
}

async function pickTopic(i){
  const c = S.candidates[i];
  try{
    const r = await api('/api/topics/pick', {candidate:c});
    toast('已建项目 '+r.id);
    const d = await api('/api/projects'); S.projects = d.projects;
    openProject(r.id);
  }catch(e){ toast('失败：'+e.message, 3600); }
}

/* ── 创作 ── */
async function renderProjects(){
  const d = await api('/api/projects');
  S.projects = d.projects;
  if(!d.projects.length){
    $('#view').innerHTML = `<div class="empty"><b>还没有内容项目</b>
      去「选题」生成候选并建为项目，或点右上角直接新建。</div>`;
    return;
  }
  $('#view').innerHTML = d.projects.map(p=>`
    <div class="prow" onclick="openProject('${p.id}')">
      <div style="min-width:0;flex:1">
        <div class="ptitle">${esc(p.title||p.angle||'（未选题）')}</div>
        <div class="pmeta">
          ${esc(p.platform_name)} · ${esc(p.niche)}
          ${p.pattern?` · <span class="tag serif" style="font-size:10px">${esc(p.pattern)}</span>`:''}
          ${p.metrics?` · 浏览 ${fmt(p.metrics.views)}`:''}
        </div>
      </div>
      <div class="pact">
        <button class="iconbtn" title="删除"
          onclick="event.stopPropagation();deleteProject('${p.id}',false)">✕</button>
      </div>
      <div class="pstage" style="min-width:132px">
        <div style="margin-bottom:3px">
          ${(p.sop?.done||0)>0 ? Array.from({length:p.sop.total||6},(_,i)=>
            `<span style="display:inline-block;width:14px;height:5px;border-radius:2px;
              margin-right:3px;background:${i<(p.sop?.done||0)?'var(--ok)':'var(--line2)'}"></span>`
          ).join('') : ''}
        </div>
        <div class="pmeta">${esc(p.sop?.current_name || p.stage_label)}</div>
      </div>
    </div>`).join('');
}

async function newProject(){
  const niche = prompt('内容领域？','AI×国学'); if(niche===null) return;
  const pf = prompt('目标平台？(xiaohongshu/gzh/zhihu/bilibili/douyin/toutiao)','xiaohongshu');
  if(pf===null) return;
  try{
    const r = await api('/api/projects', {niche, platform:pf.trim()});
    toast('已建项目 '+r.id); go('projects');
  }catch(e){ toast('失败：'+e.message, 3600); }
}

async function openProject(id){
  const [p, w] = await Promise.all([
    api('/api/projects/'+id), api('/api/projects/'+id+'/wizard')]);
  S.wizard = w; S.proj = p;
  // 选择器状态：已选的用项目现有素材预填
  S.picker = { sel: (p.brief?.assets||[]).map((path,i)=>({
      id:'existing'+i, path, book: path.split('/')[1]||'', chapter:'', page:''})),
    list: [], mode:'suggest' };

  // 进度条
  const bar = w.steps.map(st=>{
    const c = st.done?'var(--ok)':(st.status==='current'?'var(--red)':'var(--line2)');
    return `<span title="${esc(st.name)}" style="display:inline-block;width:26px;height:6px;
      border-radius:3px;background:${c};margin-right:4px"></span>`;
  }).join('');
  const steps = w.steps.map(st=>{
    const icon = st.done?'✅':(st.status==='current'?'▶':'○');
    const cls = st.status==='current'?'color:var(--red);font-weight:600':'';
    return `<div class="hint" style="${cls}">${icon} ${esc(st.name)}${
      st.status==='current'&&st.issues.length?` <span style="color:var(--warn)">
        ⚠️ ${st.issues.length} 处待改</span>`:''}</div>`;
  }).join('');

  const d = w.current_def;
  let body = '';
  if(!d){
    body = `<div class="card" style="text-align:center;padding:20px">
      <div style="font-size:20px">🎉</div>
      <div style="font-weight:700;margin:6px 0">六步全部完成</div>
      <div class="hint">内容已达可发布标准，去发布清单按平台操作。</div></div>`;
  } else {
    const blocked = w.blocked_by.length ? `
      <div class="hint" style="color:var(--red);background:var(--redbg);
        padding:9px 11px;border-radius:8px;margin-bottom:12px">
        <b>⛔ 卡在这一步，差这些：</b><br>
        ${w.blocked_by.map(x=>'· '+esc(x)).join('<br>')}</div>` : '';
    body = `
    <div class="card" style="border-left:4px solid var(--red);margin-bottom:12px">
      <div class="flexrow"><h3 style="margin:0">${esc(d.name)}</h3>
        <span class="tag red">${esc(d.who==='human'?'你来':d.who==='tool'?'工具':'协作')}</span></div>
      <div class="sub">${esc(d.goal)}</div>
      ${d.how.map(h=>`<div class="hint">· ${esc(h)}</div>`).join('')}
      ${d.need.length?`<div class="hint" style="margin-top:6px">
        <b>需要</b> ${d.need.map(esc).join(' / ')}</div>`:''}
      ${d.pass_when.length?`<div class="hint">
        <b>通过</b> ${d.pass_when.map(esc).join(' / ')}</div>`:''}
    </div>
    ${blocked}
    ${stepForm(id, d.key, w)}`;
  }

  const outputs = p.outputs.length ? p.outputs.map(f=>{
    const url = `/file/${id}/output/${encodeURIComponent(f.name)}`;
    const thumb = f.kind==='image'?`<img class="thumb" src="${url}" loading="lazy">`:'';
    const act = ['video','image','html'].includes(f.kind)
      ? `<a class="btn sm" href="${url}" target="_blank">打开</a>`:'';
    return `<div class="file">${thumb}<div class="fname">${esc(f.name)}</div>
      <div class="fsize">${bytes(f.size)}</div>${act}</div>`;
  }).join('') : `<div class="hint">还没有产出</div>`;

  openDrawer(p.title||p.angle||p.id, `
    <div class="flexrow" style="margin-bottom:10px">
      <span class="tag red">${esc(p.platform_name)}</span>
      <span class="tag">${esc(p.niche)}</span>
      ${p.pattern?`<span class="tag serif">${esc(p.pattern)}</span>`:''}
      <span class="tag ok">${esc(p.stage_label)}</span>
    </div>
    <div style="margin-bottom:6px">${bar}
      <span class="hint">SOP ${w.done}/${w.total}（${w.percent}%）</span></div>
    <div style="margin-bottom:14px">${steps}</div>
    <div class="sep"></div>
    ${body}
    <div class="sep"></div>
    <h3 style="margin:0 0 8px">产出文件（${p.outputs.length}）</h3>${outputs}
    <div class="sep"></div>
    <div class="flexrow">
      <button class="danger sm" onclick="deleteProject('${p.id}',false)">移入回收站</button>
      <button class="ghost sm" onclick="deleteProject('${p.id}',true)">彻底删除</button>
      <span class="hint">会删掉这个项目的图文、封面、视频等全部内容</span>
    </div>
    <div id="jobLog" style="margin-top:12px"></div>`);
}

/* ── 删除项目（含确认，显示将删掉什么）── */
async function deleteProject(id, hard){
  let info = {files:0, bytes:0};
  try{ info = await api(`/api/projects/${id}/size`); }catch(e){}
  const what = `${info.files} 个文件 · ${bytes(info.bytes)}`;
  const msg = hard
    ? `⚠️ 彻底删除「${id}」？\n\n将永久抹掉 ${what}，包括图文、封面、视频，**无法恢复**。`
    : `删除「${id}」？\n\n将移入回收站（${what}），之后可以恢复。`;
  if(!confirm(msg)) return;
  try{
    const r = await api(`/api/projects/${id}${hard?'?hard=1':''}`,
                        {}, 'DELETE');
    toast(hard ? `已彻底删除（${what}）` : `已移入回收站（${what}）`, 3600);
    closeDrawer();
    go(S.page, false, '');     // 刷新当前页
  }catch(e){ toast('删除失败：'+e.message, 4000); }
}

/* ── 回收站 ── */
async function showTrash(){
  const t = await api('/api/trash');
  openDrawer('回收站', `
    <div class="hint" style="margin-bottom:12px">
      删除的项目会先放这里，误删可以恢复。
    </div>
    ${t.count ? `
      <div class="flexrow" style="margin-bottom:12px">
        <button class="danger sm" onclick="emptyTrash()">清空回收站（${t.count} 项 / ${bytes(t.bytes)}）</button>
      </div>
      ${t.items.map(x=>`
        <div class="card" style="padding:12px 14px;margin-bottom:8px">
          <div class="flexrow">
            <div style="flex:1">
              <div style="font-weight:600">${esc(x.pid)}</div>
              <div class="hint">${x.files} 个文件 · ${bytes(x.bytes)} · 删除于 ${esc(x.deleted_at)}</div>
            </div>
            <button class="sm" onclick="restoreTrash('${encodeURIComponent(x.name)}')">恢复</button>
          </div>
        </div>`).join('')}`
      : '<div class="hint">回收站是空的</div>'}`);
}
async function restoreTrash(name){
  try{ await api(`/api/trash/${name}/restore`, {}, 'POST');
    toast('已恢复'); showTrash(); go(S.page, false, '');
  }catch(e){ toast('恢复失败：'+e.message); }
}
async function emptyTrash(){
  const t = await api('/api/trash');
  if(!confirm(`清空回收站？\n\n将永久抹掉 ${t.count} 项 / ${bytes(t.bytes)}，无法恢复。`)) return;
  try{ const r = await api('/api/trash/empty', {}, 'POST');
    toast(`已清空（${r.count} 项）`); showTrash(); go(S.page, false, '');
  }catch(e){ toast('清空失败：'+e.message); }
}

/* ── 素材选择器（第①步用）── */
S.picker = S.picker || {sel: [], list: [], mode: 'suggest'};

function renderPicker(id, w){
  const sel = S.picker.sel;
  const list = S.picker.list.length ? S.picker.list : (w.suggested||[]);
  const grid = list.map(a=>`
    <div onclick="toggleAsset('${a.id}','${esc(a.path)}')"
      style="cursor:pointer;border:2px solid ${
        sel.some(x=>x.id===a.id)?'var(--red)':'var(--line)'};
        border-radius:8px;overflow:hidden;background:#fff">
      <img src="${a.thumb}" loading="lazy"
        style="width:100%;height:88px;object-fit:cover;display:block">
      <div style="padding:5px 7px;font-size:11px;line-height:1.35">
        <div style="font-weight:600">${esc(a.book||'')}</div>
        <div class="hint" style="font-size:10px">${esc((a.chapter||'').slice(0,10))}
          ${a.page?'· '+esc(a.page):''}</div>
      </div>
    </div>`).join('') || '<div class="hint">没有素材</div>';

  return `
    <div class="flexrow" style="margin-bottom:8px">
      <input id="pkQ" placeholder="搜书名或关键词，如 周易参同契 / 相法" style="flex:1"
        onkeydown="if(event.key==='Enter')searchAssets('${id}')">
      <button class="sm" onclick="searchAssets('${id}')">搜索</button>
      <button class="sm ghost" onclick="loadSuggested('${id}')">看推荐</button>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));
      gap:8px;max-height:250px;overflow:auto;margin-bottom:10px">${grid}</div>
    <div class="hint">
      <b>已选 ${sel.length} 个</b>
      ${sel.length?sel.map(x=>`<span class="tag red">${esc(x.book)}
        <span onclick="event.stopPropagation();removeAsset('${x.id}','${id}')"
          style="cursor:pointer;margin-left:4px">✕</span></span>`).join(''):'（至少要选 1 个，或写豁免理由）'}
    </div>`;
}

function toggleAsset(id, path){
  const i = S.picker.sel.findIndex(x=>x.id===id);
  const all = S.picker.list.length ? S.picker.list : (S.wizard?.suggested||[]);
  const a = all.find(x=>x.id===id) || {id, path, book:'', chapter:'', page:''};
  if(i>=0) S.picker.sel.splice(i,1);
  else S.picker.sel.push({id, path, book:a.book, chapter:a.chapter, page:a.page});
  refreshPicker();
}
function removeAsset(id){ S.picker.sel = S.picker.sel.filter(x=>x.id!==id); refreshPicker(); }
function refreshPicker(){
  const box = $('#pickerBox');
  if(box) box.innerHTML = renderPicker(S.proj?.id || '', S.wizard||{});
}
async function searchAssets(pid){
  const q = ($('#pkQ')||{}).value || '';
  S.picker.list = [];
  try{
    const r = await api('/api/assets/search?limit=40&q='+encodeURIComponent(q));
    S.picker.list = r.items;
    refreshPicker();
    toast(`找到 ${r.total} 个素材`);
  }catch(e){ toast('搜索失败：'+e.message); }
}
async function loadSuggested(pid){
  S.picker.list = [];
  const w = await api(`/api/projects/${pid}/wizard`);
  S.wizard = w;
  refreshPicker();
}

/* 按当前 SOP 步骤渲染**只问这一步要的东西** */
function stepForm(id, key, w){
  const g = (k,d='') => w.draft[k] ?? d;
  const f = {
    pick: `
      <label class="f"><span>选题角度（一句话说清你要做什么）</span>
        <input id="sAngle" value="${esc(g('angle'))}"
          placeholder="例：AI 以为的古文 vs 古籍原页长什么样"></label>
      <div class="sep"></div>
      <h3 style="margin:0 0 6px">文献依据（选古籍原页）</h3>
      <div class="hint" style="margin-bottom:10px">
        这一步要求有文献依据 —— 这是你区别于搬运号的地方。<br>
        下面是系统按选题**自动推荐**的素材，也可以搜索换。
      </div>
      <div id="pickerBox">${renderPicker(id, w)}</div>
      <div class="sep"></div>
      <details style="margin-bottom:12px">
        <summary class="hint" style="cursor:pointer">这条确实不需要素材？点这里写豁免理由</summary>
        <label class="f" style="margin-top:8px">
          <input id="sNoAsset" placeholder="例：纯技术测评，不引用古籍原文"></label>
      </details>
      <button class="primary" onclick="submitStep('${id}','pick')">提交并进入下一步</button>`,
    hook: `
      <div class="hint" style="margin-bottom:8px">
        公式：<b>所有人都以为 X，我实测发现 Y</b> —— 没有反差会被卡住</div>
      <label class="f"><span>钩子（前 3 秒抓人）</span>
        <textarea id="sHook" rows="3"
          placeholder="例：所有人都说知识图谱能把古籍串起来，我做了一半发现根本串不动">${esc(g('hook'))}</textarea></label>
      <button class="primary" onclick="submitStep('${id}','hook')">提交并进入下一步</button>`,
    body: `
      <div class="hint" style="margin-bottom:8px">
        这是灵魂。要求：≥2 条要点、正文 ≥80 字、<b>含具体数字或书名版本</b>、有古籍素材。</div>
      <label class="f"><span>核心要点（每行一条，至少 2 条）</span>
        <textarea id="sKp" rows="4">${esc((g('key_points')||[]).join('\n'))}</textarea></label>
      <label class="f"><span>你的真实经历 / 实测数据</span>
        <textarea id="sPersonal" rows="7"
          placeholder="你实际做了什么、怎么做的、结果如何。要具体到数字或书名。">${esc(g('personal'))}</textarea></label>
      <label class="f"><span>文献依据（素材库文件路径，每行一个，可留空用已有）</span>
        <textarea id="sAssets" rows="2">${esc((g('assets')||[]).join('\n'))}</textarea></label>
      <button class="primary" onclick="submitStep('${id}','body')">提交并进入下一步</button>`,
    close: `
      <div class="hint" style="margin-bottom:8px">
        抛一个开放问题，或给一个反差结论。不要「点赞关注」。</div>
      <label class="f"><span>收尾句</span>
        <input id="sClose" placeholder="例：你们觉得古籍的实体对齐，值得专门做一套规则吗？"></label>
      <button class="primary" onclick="submitStep('${id}','close')">提交并进入下一步</button>`,
    produce: `
      <label class="f"><span>视频模板</span>
        <select id="fTpl">
          <option value="">素片（不加片头片尾）</option>
          ${Object.entries(S.overview?.video_templates||{}).map(([k,v])=>
            `<option value="${k}">${esc(v.name)} — ${esc(v.desc)}</option>`).join('')}
        </select></label>
      <div class="flexrow" style="margin-bottom:10px">
        <button class="primary" onclick="produce('${id}')">▶ 生产全部产物</button>
        <button class="sm" onclick="produce('${id}',['covers'])">只做封面</button>
        <button class="sm" onclick="produce('${id}',['video'])">只做视频</button>
      </div>
      <div class="hint">生产完点下面的按钮确认，流程才会推进到第⑥步。</div>
      <button onclick="submitStep('${id}','produce')" style="margin-top:8px">
        我已生产完成，进入质检</button>`,
    qa: `
      <div class="flexrow" style="margin-bottom:10px">
        <button class="primary" onclick="runQa('${id}')">✓ 跑出厂质检</button>
        <button onclick="submitStep('${id}','qa')">确认质检通过，完成流程</button>
      </div>
      <div id="qaBox"></div>`,
  }[key] || `<div class="hint">该步骤暂无表单</div>`;
  return `<div class="card">${f}</div>`;
}

async function submitStep(id, step){
  const payload = {step};
  const v = x => { const e=$('#'+x); return e ? e.value : ''; };
  if(step==='pick'){
    payload.angle = v('sAngle');
    payload.no_asset_reason = v('sNoAsset');
    if(S.picker.sel.length) payload.assets = S.picker.sel.map(x=>x.path);
  }
  if(step==='hook'){ payload.hook = v('sHook'); }
  if(step==='body'){
    payload.key_points = v('sKp').split('\n');
    payload.personal = v('sPersonal');
    const a = v('sAssets').split('\n').map(x=>x.trim()).filter(Boolean);
    if(a.length) payload.assets = a;
  }
  if(step==='close'){ payload.close = v('sClose'); }
  try{
    const r = await api(`/api/projects/${id}/step`, payload);
    if(r.ok){
      toast(r.done ? '🎉 流程全部完成' : '通过，进入下一步');
      openProject(id);
      return;
    }
    // 未通过：把问题显示出来
    const box = $('#qaBox') || document.querySelector('#drawerBody .card:last-child');
    const html = `<div class="hint" style="color:var(--red);background:var(--redbg);
      padding:10px 12px;border-radius:8px;margin-top:10px">
      <b>⛔ 未通过，差这些：</b><br>${r.issues.map(x=>'· '+esc(x)).join('<br>')}</div>`;
    if(box) box.insertAdjacentHTML('beforeend', html);
    toast(`未通过：${r.issues.length} 处待改`, 3200);
    openProject(id);
  }catch(e){ toast('提交失败：'+e.message, 3600); }
}

async function saveBrief(id){
  try{
    await api(`/api/projects/${id}/brief`, {
      hook: $('#fHook').value,
      key_points: $('#fKp').value.split('\n'),
      personal: $('#fPersonal').value,
    });
    await api(`/api/projects/${id}/angle`, {angle: $('#fAngle').value});
    toast('要点已保存');
    go('projects');
  }catch(e){ toast('保存失败：'+e.message, 3600); }
}

async function runQa(id){
  const box = $('#qaBox'), badge = $('#qaBadge');
  if(!box) return toast('质检区域未就绪，请重新打开项目');
  box.innerHTML = '<div class="hint">质检中…</div>';
  try{
    const r = await api(`/api/projects/${id}/qa`);
    const ok = r.structure||{};
    const rows = Object.entries(ok).map(([k,v])=>
      `<div class="hint">${v?'✅':'❌'} ${esc(k)}</div>`).join('');
    const red = (r.issues||[]).filter(i=>i.level==='red');
    const amber = (r.issues||[]).filter(i=>i.level==='amber');
    let issues = '';
    if(red.length) issues += `<div class="hint" style="color:var(--red);margin-top:8px">
      <b>🔴 硬红线 ${red.length} 处（会被拦截）</b><br>` +
      red.slice(0,6).map(i=>`· [${esc(i.where)}] ${esc(i.word)} → ${esc(i.advice)}`)
        .join('<br>') + '</div>';
    if(amber.length) issues += `<div class="hint" style="color:var(--warn);margin-top:8px">
      <b>🟡 易触发审核 ${amber.length} 处</b><br>` +
      amber.slice(0,5).map(i=>`· [${esc(i.where)}] ${esc(i.word)} → ${esc(i.advice)}`)
        .join('<br>') + '</div>';
    if(!red.length && !amber.length) issues =
      `<div class="hint" style="color:var(--ok);margin-top:8px">✅ 未命中任何触发词</div>`;
    box.innerHTML = `<div class="card" style="padding:12px 14px">
      <div class="hint" style="margin-bottom:6px"><b>结构完整度</b></div>${rows}${issues}</div>`;
    // qaBadge 只在抽屉的旧版质检区存在，步骤表单里没有 —— 取不到就跳过
    if(badge) badge.innerHTML = r.passed
      ? '<span class="tag ok">✅ 质检通过</span>'
      : `<span class="tag red">🔴 ${red.length} 处硬红线</span>`;
    toast(r.passed ? '质检通过' : `质检未通过：${red.length} 处硬红线`, 3000);
  }catch(e){ box.innerHTML = `<div class="hint" style="color:var(--red)">${esc(e.message)}</div>`; }
}

async function produce(id, only){
  const box = $('#jobLog');
  const tpl = $('#fTpl') ? $('#fTpl').value : '';
  box.innerHTML = `<div class="hint">已提交生产任务…</div>`;
  try{
    const r = await api(`/api/projects/${id}/produce`,
                        {only: only||[], video_template: tpl});
    const jid = r.job;
    const t0 = Date.now();
    const timer = setInterval(async ()=>{
      let j;
      try{ j = await api('/api/jobs/'+jid); }catch(e){ return; }
      const secs = ((Date.now()-t0)/1000).toFixed(0);
      box.innerHTML = `<div class="hint" style="margin-bottom:6px">
          生产进行中… ${secs}s</div>
        <div class="log">${esc((j.log||[]).slice(-14).join('\n'))}</div>`;
      if(j.state!=='running'){
        clearInterval(timer);
        if(j.state==='error'){
          box.innerHTML += `<div class="hint" style="color:var(--red);margin-top:8px">
            ❌ ${esc(j.error)}</div>`;
          toast('生产失败', 3600);
        }else{
          const pr = j.result?.produced||{};
          box.innerHTML += `<div class="hint" style="color:var(--ok);margin-top:8px">
            ✅ 完成：${Object.entries(pr).map(([k,v])=>k+' '+v).join(' · ')}</div>`;
          toast('生产完成');
          setTimeout(()=>openProject(id), 900);
        }
      }
    }, 900);
  }catch(e){ box.innerHTML = `<div class="hint" style="color:var(--red)">${esc(e.message)}</div>`; }
}

/* ── 素材库 ── */
async function renderAssets(){
  const d = await api('/api/assets');
  S.assets = d;
  const s = d.stats;
  const books = Object.entries(s.by_book||{}).map(([b,c])=>
    `<span class="tag">${esc(b)} ${c}</span>`).join('') || '<span class="hint">空</span>';
  const tags = Object.entries(s.tags||{}).slice(0,14).map(([t,c])=>
    `<span class="tag">${esc(t)} ${c}</span>`).join('') || '<span class="hint">空</span>';

  const rows = d.items.slice(0,300).map(a=>`<tr>
      <td>${a.present?'':'<span class="tag warn">缺失</span> '}${esc(a.name)}</td>
      <td>${esc(a.book||'—')}</td>
      <td>${esc(a.chapter||'—')}</td>
      <td>${esc(a.edition||'—')}</td>
      <td>${a.w?a.w+'×'+a.h:'—'}</td>
      <td>${a.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')||'—'}</td>
    </tr>`).join('') || `<tr><td colspan="6" class="hint" style="padding:16px">
      素材库是空的。把你的古籍图片放进 <code>${esc(d.root)}</code>
      然后点「重新扫描目录」；或命令行导入：
      <br><br><code>python3 asset_library.py import &lt;目录&gt; --auto-parse</code>
      </td></tr>`;

  $('#view').innerHTML = `
  <div class="grid g4" style="margin-bottom:16px">
    <div class="card"><div class="stat sm">${s.total}</div>
      <div class="statlabel">素材总数（在库 ${s.present}）</div></div>
    <div class="card"><div class="stat sm">${(s.bytes/1048576).toFixed(1)} MB</div>
      <div class="statlabel">占用空间</div></div>
    <div class="card"><div class="stat sm">${Object.keys(s.by_book||{}).length}</div>
      <div class="statlabel">书目数</div></div>
    <div class="card"><div class="stat sm">${Object.keys(s.tags||{}).length}</div>
      <div class="statlabel">标签数</div></div>
  </div>

  <div class="grid g2" style="margin-bottom:14px">
    <div class="card"><h3>按书目</h3><div style="line-height:2.1">${books}</div></div>
    <div class="card"><h3>高频标签</h3><div style="line-height:2.1">${tags}</div></div>
  </div>

  <div class="card">
    <h3>素材清单</h3>
    <div class="sub">文件原样存放在 ${esc(d.root)}，可直接用访达管理；改动后点「重新扫描目录」同步</div>
    <table><thead><tr><th>文件</th><th>书名</th><th>篇名</th><th>版本</th>
      <th>尺寸</th><th>标签</th></tr></thead><tbody>${rows}</tbody></table>
  </div>`;
}

async function rescanAssets(){
  toast('正在扫描…');
  try{
    const r = await api('/api/assets/rescan', {});
    toast(`扫描完成：新增 ${r.rescan.added} · 缺失 ${r.rescan.missing}`);
    renderAssets();
  }catch(e){ toast('失败：'+e.message, 3600); }
}

/* ── 数据 ── */
async function renderMetrics(){
  const o = await api('/api/overview');
  S.overview = o;
  const perf = (o.performance||[]);
  const table = perf.length ? perf.map(p=>`<tr>
      <td>${esc(p.name)}</td><td class="num">${p.n}</td>
      <td class="num">${fmt(p.views)}</td><td class="num">${fmt(p.eng)}</td>
      <td class="num"><b>${p.rate.toFixed(1)}%</b></td>
      <td><div class="bar" style="width:90px"><i style="width:${
        Math.min(p.rate/10*100,100)}%"></i></div></td>
    </tr>`).join('')
    : `<tr><td colspan="6" class="hint" style="padding:16px">
        还没有数据。发布内容后用数据采集回填。</td></tr>`;

  $('#view').innerHTML = `
  <div class="card" style="margin-bottom:14px">
    <h3>选题结构表现</h3>
    <div class="sub">发布数据回流后，选题引擎会自动给表现好的结构加权 —— 发得越多，越懂你的账号</div>
    <table><thead><tr><th>结构</th><th class="num">条数</th><th class="num">总浏览</th>
      <th class="num">总互动</th><th class="num">互动率</th><th></th></tr></thead>
      <tbody>${table}</tbody></table>
  </div>

  <div class="card">
    <h3>怎么采集数据</h3>
    <div class="sub">用 macOS 内置 Vision OCR 读你自己后台的数据看板 —— <b>只读不写</b></div>
    <div class="log" style="background:#faf7f0;color:var(--ink2);border:1px solid var(--line)"># 1. 打开后台截图（首次需你手动登录）
python3 metrics_collector.py capture --platform xiaohongshu

# 2. 只看不写（核对一下读得对不对）
python3 metrics_collector.py read dashboard_xiaohongshu.png

# 3. 匹配到项目并写回
python3 metrics_collector.py sync dashboard_xiaohongshu.png</div>
    <div class="sep"></div>
    <div class="hint">
      <b>边界</b>：只读取你自己账号的数据看板。不发布、不点赞、不评论、不关注、不改设置。<br>
      支持：${Object.keys(o.platforms||{}).length} 个平台的创作者后台。
    </div>
  </div>`;
}

/* ── 创作流程 SOP ── */
async function renderSop(){
  const [d, a] = await Promise.all([api('/api/sop'), api('/api/audit')]);
  if(d.error) return $('#view').innerHTML = `<div class="empty">${esc(d.error)}</div>`;

  const who = {tool:'🤖 工具', human:'🧑 你来', 'tool+human':'🤖🧑 协作'};
  const steps = d.steps.map((s,i)=>`
    <div class="card" style="margin-bottom:10px">
      <div class="flexrow" style="margin-bottom:6px">
        <h3 style="margin:0">${esc(s.name)}</h3>
        <span class="tag">${who[s.who]||s.who}</span>
      </div>
      <div class="sub">${esc(s.goal)}</div>
      ${s.how.map(h=>`<div class="hint">· ${esc(h)}</div>`).join('')}
      ${s.need.length?`<div class="hint" style="margin-top:6px">
        <b>需要</b> ${s.need.map(esc).join(' / ')}</div>`:''}
      ${s.pass_when.length?`<div class="hint">
        <b>通过</b> ${s.pass_when.map(esc).join(' / ')}</div>`:''}
    </div>`).join('');

  const pats = Object.entries(a.patterns||{}).sort((x,y)=>y[1]-x[1])
    .map(([k,v])=>`<tr><td>${esc(k)}</td><td class="num">${v}</td>
      <td><div class="bar" style="width:100px"><i style="width:${
        Math.min(v/Math.max(a.total,1)*100*2,100)}%"></i></div></td></tr>`).join('');

  $('#view').innerHTML = `
  <div class="card" style="margin-bottom:14px;border-left:4px solid var(--red)">
    <h3>${esc(d.name)}</h3>
    <div class="sub">${esc(d.desc)}</div>
    <div class="hint" style="margin-top:6px"><b>来源</b> ${esc(d.source)}</div>
    <div class="sep"></div>
    <div class="hint">
      <b>为什么要有质检关</b>：官媒在分析爆款时自己划了红线 ——
      同质化「每个都用同样语调会扁平化历史」、反戏说「别让戏说变成胡说」、
      肖像权「AI 换脸需标注」、触发词「风水运势等噱头」。
      这些不是建议，是<strong>能不能发出去</strong>的判据。
    </div>
  </div>

  <div class="grid g2" style="align-items:start">
    <div>${steps}</div>
    <div>
      <div class="card" style="margin-bottom:12px">
        <h3>全库同质化体检</h3>
        <div class="sub">官媒警告：『不管扁鹊还是李白都用同样语调咆哮』会审美疲劳</div>
        <table><thead><tr><th>结构</th><th class="num">条数</th><th></th></tr></thead>
          <tbody>${pats||'<tr><td colspan="3" class="hint">暂无数据</td></tr>'}</tbody></table>
        ${Object.keys(a.dup_patterns||{}).length
          ? `<div class="hint" style="margin-top:10px;color:var(--warn)">
              🟡 ${Object.entries(a.dup_patterns).map(([k,v])=>
                `「${esc(k)}」用了 ${v} 次`).join('、')} —— 建议换结构</div>`
          : `<div class="hint" style="margin-top:10px;color:var(--ok)">
              ✅ 结构分布均衡</div>`}
        ${Object.keys(a.dup_heads||{}).length
          ? `<div class="hint" style="margin-top:6px">🔵 开头重复：${
              Object.entries(a.dup_heads).map(([k,v])=>
                `「${esc(k)}…」${v} 次`).join('、')}</div>` : ''}
      </div>

      <div class="card">
        <h3>触发词红线</h3>
        <div class="sub">命中即触发平台审核 —— 生产前会自动拦截</div>
        <div style="margin-bottom:10px">
          <div class="hint" style="margin-bottom:5px"><b>🔴 硬红线（必须改）</b></div>
          ${(d.red_words||[]).map(w=>`<span class="tag red">${esc(w)}</span>`).join('')}
        </div>
        <div>
          <div class="hint" style="margin-bottom:5px"><b>🟡 易触发（建议改）</b></div>
          ${(d.amber_words||[]).map(w=>`<span class="tag warn">${esc(w)}</span>`).join('')}
        </div>
      </div>
    </div>
  </div>`;
}

/* ── 启动：支持深链接 ──
      http://127.0.0.1:8765/#assets              直接进素材库
      http://127.0.0.1:8765/#projects/<项目id>   直接打开项目流程向导 */
{ const [pg, param] = parseHash(); go(pg, false, param); }
