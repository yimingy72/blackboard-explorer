// Isolated design preview. All nodes, sources and replies below are synthetic.
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const labels = {confirmed:['已有依据','green','check'],active:['探索中','blue','clock'],blocked:['待补证','amber','block'],excluded:['已排除','gray','close']};
const nodes = [
  {id:'goal',kind:'root',x:52,y:336,w:242,h:135,title:'定位校验失败原因',caption:'明确触发条件，并保留可复现的依据',status:'active'},
  {id:'inputs',parent:'goal',kind:'branch',x:385,y:172,w:232,h:100,title:'输入与响应',caption:'编码线索与触发边界正在复核',status:'active',count:3},
  {id:'environment',parent:'goal',kind:'branch',x:385,y:436,w:232,h:100,title:'配置与环境',caption:'当前证据已排除环境差异',status:'excluded',count:1},
  {id:'delivery',parent:'goal',kind:'branch',x:385,y:614,w:232,h:100,title:'复现与交付',caption:'复现已保存，修复待验证',status:'blocked',count:2},
  {id:'response',parent:'inputs',kind:'leaf',x:724,y:40,w:280,h:106,title:'响应中的数据结构已确认',status:'confirmed',sources:['F8','F12'],agent:'Agent 1'},
  {id:'encoding',parent:'inputs',kind:'leaf',x:724,y:168,w:280,h:106,title:'旧编码数据可触发校验失败',status:'active',sources:['F19','I7'],agent:'Agent 2'},
  {id:'config',parent:'environment',kind:'leaf',x:724,y:436,w:280,h:106,title:'配置版本一致，差异方向已排除',status:'excluded',sources:['F15','I4'],agent:'Agent 3'},
  {id:'boundary',parent:'inputs',kind:'leaf',x:724,y:296,w:280,h:106,title:'触发边界仍需覆盖更多输入',status:'blocked',sources:['I9'],agent:'Agent 2'},
  {id:'repro',parent:'delivery',kind:'leaf',x:724,y:550,w:280,h:106,title:'最小复现脚本与输出已保存',status:'confirmed',sources:['F23'],agent:'Agent 1'},
  {id:'verification',parent:'delivery',kind:'leaf',x:724,y:678,w:280,h:106,title:'修复后的对照结果尚未产生',status:'blocked',sources:['I11'],agent:'待领取'},
];
const sourceData = {
  F8:['观察事实','Agent 1','在同一输入下，响应字段与文档约定的数据结构一致；保存了请求与响应记录。'],
  F12:['观察事实','Agent 1','重复三次请求，失败位置均处于旧数据读取后的校验阶段，响应结构没有发生变化。'],
  F19:['观察事实','Agent 2','对照样例中旧编码数据触发校验失败；新编码对照样例通过。该现象尚未覆盖全部字符边界。'],
  I7:['探索意图','Agent 2','比较两类编码样例的校验路径，定位触发差异。状态：已认领，仍在探索。'],
  F15:['观察事实','Agent 3','复核服务版本、配置摘要和依赖版本，当前两组环境一致。已保存对照文件。'],
  I4:['探索意图','Agent 3','检查配置与环境差异是否导致失败。状态：已关闭；结论：本次差异方向被排除。'],
  I9:['探索意图','Agent 2','扩展输入边界，验证旧编码触发条件的适用范围。状态：等待进一步证据。'],
  F23:['观察事实','Agent 1','已保存最小复现脚本与完整输出，可独立复查同一触发条件。'],
  I11:['探索意图','Agent 1','获得修复后的对照结果，验证原触发条件是否消除。状态：开放，尚未认领。'],
};
const details = {
  goal:{summary:'已定位到旧编码输入这一条可复现线索，环境与配置差异已排除。接下来需要补齐触发边界，并取得修复后的对照结果。',sources:['F19','F15','F23'],gap:'整体目标尚未由 Close 判定达成。树上的阶段成果不代表任务已经完成。'},
  inputs:{summary:'响应结构与约定一致，当前调查已收敛到旧编码数据。Agent 2 正在比较校验路径，尚未给出完整的适用边界。',sources:['F12','F19','I7'],gap:'需要排除样例偶然性，并明确哪些输入条件必然触发。'},
  environment:{summary:'目前的配置、服务版本和依赖对照一致。这条差异方向已被排除，保留对照依据，避免后续重复检查。',sources:['F15','I4'],gap:'仅排除已记录的两组环境差异，不推断所有环境都相同。'},
  delivery:{summary:'最小复现和输出已保存。当前具备复核条件，但还没有修复后的对照结果，交付仍有缺口。',sources:['F23','I11'],gap:'缺修复验证结果与适用范围说明。'},
  response:{summary:'已保存响应与重复请求记录，当前样例中的数据结构保持一致。响应结构本身尚未显示异常，后续重点转向数据内容。',sources:['F8','F12'],gap:'结构一致不代表校验逻辑已经正确，仍需复核编码差异。'},
  encoding:{summary:'旧编码样例可重复触发校验失败，新编码对照样例通过。这个结果支持继续检查编码处理路径，但还不足以直接断言根因已经完全确定。',sources:['F19','I7','F12'],gap:'尚未确认全部字符边界；修复后的对照结果也没有产生。'},
  config:{summary:'两组环境的配置、服务版本和依赖摘要一致。当前证据不支持“环境差异导致失败”这一方向，相关探索已关闭。',sources:['F15','I4'],gap:'该排除结论只适用于已复核范围。'},
  boundary:{summary:'现有样例数量有限，Agent 2 需要补齐边界输入并核对校验路径，才能说明结论适用于哪些情况。',sources:['I9','F19'],gap:'缺少边界样例和对应输出；暂不显示为已完成。'},
  repro:{summary:'独立复核所需的最小复现脚本与输出已登记，可以从证据入口读取。该成果说明问题具备可复现依据。',sources:['F23','F19'],gap:'复现成功不等于修复验证成功，仍需后续对照。'},
  verification:{summary:'当前没有修复后对照结果。成果整理 Agent 只记录这个缺口，不替工作 Agent 进行验证，也不宣布任务完成。',sources:['I11','F23'],gap:'等待工作 Agent 领取意图并提交实际验证证据。'},
};
const processSteps = [
  {at:'14:08',actor:'Agent 1',title:'复核响应',body:'重复请求并保存对照，响应结构保持一致。',refs:['F8','F12']},
  {at:'14:15',actor:'Agent 2',title:'发现编码差异',body:'旧编码样例失败，新编码对照通过；继续追查适用条件。',refs:['F19','I7'],file:'comparison.json'},
  {at:'14:18',actor:'Agent 3',title:'排除环境差异',body:'核对版本、配置与依赖，关闭这条差异方向。',refs:['F15','I4']},
  {at:'14:20',actor:'Agent 1',title:'保存最小复现',body:'登记可独立复核的脚本与对应输出。',refs:['F23'],file:'reproduce.py'},
  {at:'14:22',actor:'Agent 2',title:'记录边界缺口',body:'补充待检查的边界输入，未把样例结果扩大为完整结论。',refs:['I9']},
  {at:'14:24',actor:'Agent 1',title:'登记验证方向',body:'提出修复后对照验证，等待领取与实际结果。',refs:['I11']},
];
const canvas=document.querySelector('#canvas'),world=document.querySelector('#world'),inspector=document.querySelector('#inspector'),handle=document.querySelector('#resize-handle');
let selected=null,activePanel='detail',scale=1,pan={x:0,y:0},collapsed=new Set(),drag=null,toastTimer,hasNew=false;
const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function badge(status){const [label,color,symbol]=labels[status];return `<span class="tag ${color}">${icon(symbol)}${label}</span>`;}
function visible(node){return !node.parent||!collapsed.has(node.parent);}
function render(){
  document.querySelector('#nodes').innerHTML=nodes.filter(visible).map(n=>`<div role="button" tabindex="0" aria-label="${escape(n.title)}" class="tree-node ${n.kind} ${n.status==='excluded'?'excluded':''} ${selected===n.id?'selected':''} ${hasNew&&n.id==='verification'?'changed':''}" style="left:${n.x}px;top:${n.y}px;width:${n.w}px;height:${n.h}px" data-node="${n.id}">${n.kind==='root'?`<span class="root-label">${icon('tree')}任务目标 · 待裁定</span><strong>${n.title}</strong><span class="root-caption">${n.caption}</span>`:n.kind==='branch'?`<div class="node-top"><span>探索方向</span>${badge(n.status)}</div><h2 class="node-title">${n.title}</h2><span class="branch-meta">${n.caption}</span><button class="node-collapse" data-collapse="${n.id}" aria-label="${collapsed.has(n.id)?'展开':'收起'}${n.title}">${collapsed.has(n.id)?'+':'−'}</button>`:`<div class="node-top"><span class="node-top-label">${badge(n.status)}</span><span>${n.agent}</span></div><h2 class="node-title">${n.title}</h2><div class="node-bottom"><span class="node-source">${n.sources.join(' · ')}</span><span>${hasNew&&n.id==='verification'?'刚刚更新':'查看依据'} ${icon('arrow')}</span></div>`}</div>`).join('');
  const byId=new Map(nodes.map(n=>[n.id,n]));
  document.querySelector('#connections').innerHTML=nodes.filter(n=>n.parent&&visible(n)).map(n=>{const p=byId.get(n.parent),sx=p.x+p.w,sy=p.y+p.h/2,tx=n.x,ty=n.y+n.h/2,mid=(sx+tx)/2;const focused=selected===n.id||selected===p.id;return `<path class="${selected?focused?'focused':'fade':''}" d="M${sx},${sy}C${mid},${sy} ${mid},${ty} ${tx},${ty}"/>`;}).join('');
  document.querySelectorAll('[data-node]').forEach(el=>{el.addEventListener('pointerdown',startDrag);el.addEventListener('click',e=>{if(!e.target.closest('[data-collapse]')&&!el.dataset.moved)openNode(el.dataset.node);});el.addEventListener('keydown',e=>{if(e.target===el&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openNode(el.dataset.node);}});});
  document.querySelectorAll('[data-collapse]').forEach(el=>el.addEventListener('click',e=>{e.stopPropagation();collapsed.has(el.dataset.collapse)?collapsed.delete(el.dataset.collapse):collapsed.add(el.dataset.collapse);render();}));
}
function transform(){world.style.transform=`translate(${pan.x}px,${pan.y}px) scale(${scale})`;document.querySelector('#zoom-label').textContent=`${Math.round(scale*100)}%`;}
function fit(){const w=canvas.clientWidth,h=canvas.clientHeight;scale=Math.min(1,(w-65)/1060,(h-90)/805);scale=Math.max(.42,scale);pan={x:Math.max(16,(w-1060*scale)/2),y:Math.max(38,(h-805*scale)/2)};transform();}
function openNode(id){selected=id;const n=nodes.find(n=>n.id===id);inspector.hidden=false;handle.hidden=false;document.querySelector('#inspector-title').textContent=n.title;document.querySelector('#inspector-caption').textContent=n.kind==='root'?'任务成果总览':n.kind==='branch'?'探索方向':'阶段进展 · AI 整理';renderDetail(id);switchPanel('detail');render();if(innerWidth>899){const edge=(n.x+n.w)*scale+pan.x;if(edge>canvas.clientWidth-28){pan.x-=edge-canvas.clientWidth+28;transform();}}}
function renderDetail(id){const n=nodes.find(n=>n.id===id),d=details[id],steps=processSteps.filter(step=>step.refs.some(ref=>d.sources.includes(ref))),authors=[...new Set(steps.map(step=>step.actor))];document.querySelector('#detail-content').innerHTML=`<div class="detail-status">${badge(n.status)}<span>整理自黑板 ${hasNew?'v136':'v128'}</span></div><p class="readable-summary">${d.summary}</p><section class="detail-section"><h2>依据与原始记录 <span>${d.sources.length} 项</span></h2><div class="source-list">${d.sources.map(ref=>{const s=sourceData[ref];return `<button class="source-row" data-source="${ref}"><div><code>${ref}</code><small>${s[0]} · ${s[1]}</small>${icon('arrow')}</div><span>${s[2].slice(0,54)}${s[2].length>54?'…':''}</span></button>`;}).join('')}</div></section><section class="detail-section"><h2>关键过程</h2><ol class="timeline">${steps.map(step=>`<li><time>${step.at}</time><strong>${step.actor} · ${step.title}</strong><p>${step.body}</p><div class="inline-sources">${step.refs.map(ref=>`<button data-source="${ref}">${ref}</button>`).join('')}</div>${step.file?`<button class="file-chip" data-file>${icon('file')}${step.file}</button>`:''}</li>`).join('')}</ol></section><section class="detail-section"><h2>尚未解决</h2><p class="gap-copy">${d.gap}</p></section><section class="detail-section"><h2>参与 Agent</h2><div class="contributors">${authors.map(author=>`<button class="contributor" data-agent="${author}"><span class="small-avatar">${author.split(' ').at(-1)}</span>${author}</button>`).join('')}</div></section>`;document.querySelector('#detail-version').textContent=hasNew?'v136':'v128';}
function switchPanel(panel){activePanel=panel;document.querySelector('#detail-content').hidden=panel!=='detail';document.querySelector('#chat-content').hidden=panel!=='chat';document.querySelector('#inspector-footer').hidden=panel==='chat';document.querySelector('#detail-tab').classList.toggle('current',panel==='detail');document.querySelector('#chat-tab').classList.toggle('current',panel==='chat');document.querySelector('#scope-chip').hidden=!(panel==='chat'&&selected);}
function ask(){inspector.hidden=false;handle.hidden=false;if(!selected){document.querySelector('#inspector-title').textContent='成果整理 Agent';document.querySelector('#inspector-caption').textContent='本任务专属会话';}switchPanel('chat');document.querySelector('#message-input').focus();}
function closePanel(){inspector.hidden=true;handle.hidden=true;selected=null;render();}
function toast(text){const el=document.querySelector('#toast');el.textContent=text;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,3800);}
function source(ref){const s=sourceData[ref];document.querySelector('#source-title').textContent=`${ref} · ${s[0]}`;document.querySelector('#source-body').innerHTML=`<dl><dt>提交者</dt><dd>${s[1]}</dd><dt>范围</dt><dd>当前任务</dd><dt>来源</dt><dd>黑板已保存记录</dd></dl><p>${s[2]}</p>`;document.querySelector('#source-drawer').hidden=false;}
document.addEventListener('click',e=>{const ref=e.target.closest('[data-source]');if(ref)source(ref.dataset.source);if(e.target.closest('[data-file]'))toast('正式版在此预览已归档证据；设计稿不读取真实文件。');const agent=e.target.closest('[data-agent]');if(agent)toast(`正式版打开 ${agent.dataset.agent} 的现有会话，保留当前成果树。`);});
document.querySelector('#ask-button').onclick=ask;document.querySelector('#ask-node').onclick=ask;document.querySelector('#detail-tab').onclick=()=>{if(!selected){selected='goal';renderDetail('goal');document.querySelector('#inspector-title').textContent='定位校验失败原因';render();}switchPanel('detail');};document.querySelector('#chat-tab').onclick=ask;document.querySelector('#close-panel').onclick=closePanel;document.querySelector('#close-source').onclick=()=>document.querySelector('#source-drawer').hidden=true;
document.querySelector('#clear-scope').onclick=()=>{selected=null;document.querySelector('#scope-chip').hidden=true;render();};
document.querySelector('#goal-button').onclick=()=>{document.querySelector('#goal-panel').hidden=!document.querySelector('#goal-panel').hidden;};document.querySelector('#raw-tab').onclick=()=>toast('这里返回现有 Fact / 意图黑板；新成果树与原黑板并列，保留各自视角。');document.querySelector('#tree-tab').onclick=()=>toast('当前展示成果树设计稿。');
document.querySelector('#fit-button').onclick=fit;document.querySelector('#zoom-label').onclick=()=>{scale=1;transform();};document.querySelector('#zoom-in').onclick=()=>{scale=Math.min(1.4,scale+.1);transform();};document.querySelector('#zoom-out').onclick=()=>{scale=Math.max(.42,scale-.1);transform();};
canvas.addEventListener('pointerdown',e=>{if(e.target.closest('.tree-node,button,.canvas-controls'))return;drag={type:'pan',sx:e.clientX,sy:e.clientY,px:pan.x,py:pan.y};canvas.setPointerCapture(e.pointerId);});
function startDrag(e){if(e.target.closest('[data-collapse]'))return;e.stopPropagation();const n=nodes.find(n=>n.id===e.currentTarget.dataset.node);drag={type:'node',node:n,sx:e.clientX,sy:e.clientY,px:n.x,py:n.y,el:e.currentTarget};}
document.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-drag.sx,dy=e.clientY-drag.sy;if(drag.type==='pan'){pan={x:drag.px+dx,y:drag.py+dy};transform();}else if(drag.type==='resize'){inspector.style.width=`${Math.max(330,Math.min(innerWidth*.65,drag.width-dx))}px`;}else if(Math.abs(dx)+Math.abs(dy)>4){drag.node.x=Math.max(0,drag.px+dx/scale);drag.node.y=Math.max(0,drag.py+dy/scale);render();const el=document.querySelector(`[data-node="${drag.node.id}"]`);if(el)el.dataset.moved='true';}});
document.addEventListener('pointerup',()=>{if(drag?.type==='node'){const id=drag.node.id;setTimeout(()=>{const el=document.querySelector(`[data-node="${id}"]`);if(el)delete el.dataset.moved;},0);}drag=null;});
handle.onpointerdown=e=>{drag={type:'resize',sx:e.clientX,sy:e.clientY,width:inspector.getBoundingClientRect().width};handle.setPointerCapture(e.pointerId);};handle.onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();inspector.style.width=`${Math.max(330,Math.min(innerWidth*.65,inspector.clientWidth+(e.key==='ArrowLeft'?24:-24)))}px`;}};
document.querySelector('#composer').onsubmit=e=>{e.preventDefault();const input=document.querySelector('#message-input'),text=input.value.trim();if(!text)return;const list=document.querySelector('#chat-messages'),question=document.createElement('div');question.className='user-message';question.textContent=text;list.appendChild(question);const response=document.createElement('div');response.className='assistant-message';response.innerHTML=`<span class="message-author">成果整理 Agent · 示例回复</span><p>${text.includes('卡')?'当前主要缺口是触发边界与修复后的对照结果。旧编码线索有样例支撑，但不能据此宣布根因已经完全确定。':'这条结论来自已保存的黑板记录。你可以点下面的引用查看原文、提交者和证据，再决定是否需要向工作 Agent 追问。'}</p><div class="inline-sources"><button data-source="F19">F19</button><button data-source="I9">I9</button><button data-source="I11">I11</button></div>`;list.appendChild(response);input.value='';document.querySelector('#suggestions').hidden=true;list.scrollTop=list.scrollHeight;};
document.querySelector('#message-input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();document.querySelector('#composer').requestSubmit();}};
document.querySelectorAll('.suggestions button').forEach(el=>el.onclick=()=>{document.querySelector('#message-input').value=el.textContent;document.querySelector('#composer').requestSubmit();});
document.querySelector('#latest-button').onclick=()=>{if(hasNew){toast('已显示模拟新增结果。');return;}hasNew=true;const n=nodes.find(n=>n.id==='verification');n.status='active';n.title='已领取修复验证，等待对照结果';n.agent='Agent 4';nodes.find(n=>n.id==='delivery').caption='Agent 4 正在执行修复验证';sourceData.I11[2]='修复后对照验证已由 Agent 4 认领，仍在执行，尚无实际结果。';details.verification.summary='Agent 4 已领取修复后对照验证，目前尚无输出。成果整理 Agent 记录执行进展，但不替工作 Agent 判断验证成功。';details.verification.gap='等待 Agent 4 提交实际对照结果与相应证据。';details.delivery.summary='最小复现和输出已保存；修复验证已由 Agent 4 领取，但还没有对照结果。';processSteps.push({at:'14:28',actor:'Agent 4',title:'领取修复验证',body:'认领验证方向，执行中，尚未提交结果。',refs:['I11']});document.querySelector('#sync-text').textContent='已整理至黑板 v136';document.querySelector('.sync-state .muted').textContent='刚刚';render();if(selected){document.querySelector('#inspector-title').textContent=nodes.find(n=>n.id===selected).title;renderDetail(selected);}toast('只更新受影响的节点，保留其他节点与当前视角。');};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(!document.querySelector('#source-drawer').hidden)document.querySelector('#source-drawer').hidden=true;else closePanel();}});
render();fit();
if(location.hash==='#detail')openNode('encoding');if(location.hash==='#chat')ask();
