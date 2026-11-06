(() => {
  const $=id=>document.getElementById(id);
  const selected=new Set();let state=null,viewId=null,logId=null,polling=false,submitting=false;
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={recording:'录制中',saved:'已保存',interrupted:'未完成',preparing:'准备数据',starting:'释放显存',training:'训练中',completed:'已完成',cancelled:'已取消',failed:'失败',cancelling:'取消中'};
  function message(text,bad=false){$('message').style.display='block';$('message').textContent=text;$('message').style.background=bad?'#fceaea':'#edf5fb';}
  async function api(path,body){const r=await fetch(path,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok||d.ok===false)throw Error(d.message||'请求失败');return d;}
  function syncInitialization(){
    const compatible=(state?.upstream_adapters||[]).some(a=>a.profile===$('base').value&&a.modes.includes($('mode').value));
    const option=$('initialization').querySelector('[value="upstream_lora"]');option.disabled=!compatible;
    if(!compatible)$('initialization').value='base';
    const continuing=$('initialization').value==='upstream_lora';
    $('rank').disabled=continuing;if(continuing)$('rank').value='64';
    $('initialization-note').textContent=continuing?'继续上游动作LoRA：保留Rank 64和全部目标模块；使用独立、校验过的快照，不修改上游权重。':compatible?'从原始Qwen基座新建LoRA；不继承已有动作策略。':'当前模式没有匹配的本地上游LoRA（现有为单臂v3），从基座训练；双臂v4功能仍可用。';
  }
  function selection(){
    const items=(state?.episodes||[]).filter(e=>selected.has(e.id));
    $('selection').textContent=`已选 ${items.length} 条轨迹 / ${items.reduce((n,e)=>n+e.steps,0)} 个动作步骤`;
    $('train-start').disabled=submitting||!state?.training_ready||state?.reserved||!items.length||!$('release-inference').checked;
  }
  function render(){
    syncInitialization();
    const episodes=state.episodes;
    $('total').textContent=episodes.length;$('eligible').textContent=episodes.filter(e=>e.trainable).length;
    $('samples').textContent=episodes.filter(e=>e.trainable).reduce((n,e)=>n+e.steps,0);
    $('environment').textContent=state.restoring_inference?'正在恢复推理':state.training_ready?'训练环境就绪':'训练依赖安装中';$('environment').className='pill '+(state.training_ready?'good':'');
    for(const id of [...selected])if(!episodes.some(e=>e.id===id&&e.trainable))selected.delete(id);
    const shown=episodes.filter(e=>$('show-archived').checked||!e.archived);
    $('episodes').innerHTML=shown.length?shown.map(e=>`<tr><td><input type="checkbox" data-select="${escape(e.id)}" ${selected.has(e.id)?'checked':''} ${e.trainable?'':'disabled'} aria-label="选择 ${escape(e.id)}"></td><td class="task"><strong>${escape(e.task_text||e.task)}</strong><br><small class="muted">${escape(e.id)}</small></td><td><span class="pill ${e.trainable?'good':''}">${e.archived?'已归档':e.success?'成功':labels[e.status]||'未标注'}</span></td><td>${e.steps}</td><td><button type="button" data-detail="${escape(e.id)}">查看</button></td></tr>`).join(''):'<tr><td colspan="5" class="empty">还没有采集数据。<br>返回 GUMI，填写任务，开始 2 cm 示教，完成后保存。</td></tr>';
    for(const option of $('base').options){const model=state.models.find(m=>m.id===option.value);option.disabled=!model?.installed;}
    $('jobs').innerHTML=state.jobs.length?state.jobs.map(j=>`<div class="job"><div class="row between"><strong>${escape(j.id)}</strong><span class="pill ${j.status==='completed'?'good':j.status==='failed'?'bad':''}">${escape(labels[j.status]||j.status)}</span></div><p>${escape(j.options.profile)} · ${escape(j.options.mode)} · ${j.samples??'—'} samples</p>${j.progress?`<p>Step ${escape(j.progress.current_steps??j.progress.step??'—')} / ${escape(j.progress.total_steps??'—')} · loss ${escape(j.progress.loss??'—')}</p>`:''}<div class="row"><button type="button" data-log="${escape(j.id)}">查看日志</button>${state.active_job===j.id&&['preparing','starting','training','cancelling'].includes(j.status)?`<button type="button" data-cancel="${escape(j.id)}">取消训练</button>`:''}${j.status==='completed'?`<a href="/api/training/artifact?id=${encodeURIComponent(j.id)}">下载 LoRA</a>`:''}</div>${j.error?`<p style="color:#a83b38">${escape(j.error)}</p>`:''}${j.status==='completed'?`<small class="muted">${escape(j.adapter_path)}</small>`:''}</div>`).join(''):'<p class="empty">尚无训练作业</p>';
    const loadable=new Set((state.inference_models||[]).map(m=>m.id));
    for(const button of $('jobs').querySelectorAll('[data-log]')){
      const job=state.jobs.find(j=>j.id===button.dataset.log);
      const source=document.createElement('small');source.textContent=job.options.initialization==='upstream_lora'?'起点：上游动作LoRA · Rank 64':'起点：原始基座';button.parentNode.appendChild(source);
      const sampling=document.createElement('small');sampling.textContent=job.sampling?`GRASP ×${job.sampling.grasp_repeat} · ${job.sampling.original_samples} → ${job.sampling.training_samples} 样本 · 抓取占比 ${(100*job.sampling.original_grasp_fraction).toFixed(1)}% → ${(100*job.sampling.training_grasp_fraction).toFixed(1)}%`:`GRASP ×${job.options.grasp_repeat??1}`;button.parentNode.appendChild(sampling);
      if(loadable.has(job.id)){
        const link=document.createElement('a');link.href='/?lora='+encodeURIComponent(job.id);
        link.textContent='在 GUMI 中选择此 LoRA →';button.parentNode.appendChild(link);
      }else if(job.status==='completed'){
        const note=document.createElement('small');note.textContent='当前单臂推理不兼容或适配器校验未通过';
        button.parentNode.appendChild(note);
      }
    }
    selection();
  }
  async function detail(id){
    const {episode:e}=await api('/api/training/episode?id='+encodeURIComponent(id));viewId=id;
    const panel=$('detail');panel.hidden=false;
    panel.innerHTML=`<div class="row between"><h3>${escape(id)}</h3><button type="button" id="detail-close">关闭</button></div><label class="field">任务文本<input id="edit-task" value="${escape(e.task_text||e.task)}" maxlength="1000" ${e.status==='recording'?'disabled':''}></label><div class="row"><button id="task-update" type="button">更新任务</button><button id="archive" type="button">${e.archived?'恢复轨迹':'归档轨迹'}</button></div><p>${e.steps} 步 · ${e.step_m===.02?'20 mm':'步长未对齐'} · ${e.success?'成功示教':'未成功/未完成'} · ${e.trainable?'可训练':'不可训练'}</p>${e.steps?'<label class="field">查看动作步骤<input id="step-preview" type="range" min="0" max="'+(e.steps-1)+'" value="0"></label><div id="action-label"></div><div class="frames" id="frames"></div>':''}`;
    function frame(i){const a=e.actions[i];$('action-label').textContent=`#${i} · L:${a.left.token} R:${a.right.token} · ${a.execution_status||'未知执行状态'}`;$('frames').innerHTML=['agentview','wrist_left','wrist_right'].map(v=>`<figure><img alt="${v} step ${i}" src="/api/training/image?id=${encodeURIComponent(id)}&view=${v}&step=${i}"><figcaption>${v}</figcaption></figure>`).join('');}
    if(e.steps){frame(0);$('step-preview').addEventListener('input',ev=>frame(Number(ev.target.value)));}
    $('detail-close').onclick=()=>{panel.hidden=true;viewId=null;};
    $('task-update').onclick=()=>api('/api/training/episode',{id,task:$('edit-task').value}).then(()=>{message('任务已更新');return refresh();}).catch(err=>message(err.message,true));
    $('archive').onclick=()=>api('/api/training/episode',{id,archived:!e.archived}).then(async()=>{message(e.archived?'轨迹已恢复':'轨迹已归档，可随时恢复');await refresh();await detail(id);}).catch(err=>message(err.message,true));
  }
  async function refresh(){if(polling)return;polling=true;try{state=await api('/api/training/state');render();if(logId){const r=await api('/api/training/log?id='+encodeURIComponent(logId));$('log').textContent=r.log||'等待日志…';}}catch(err){message(err.message,true);}finally{polling=false;}}
  $('episodes').addEventListener('change',ev=>{const id=ev.target.dataset.select;if(id){ev.target.checked?selected.add(id):selected.delete(id);selection();}});
  $('episodes').addEventListener('click',ev=>{const id=ev.target.dataset.detail;if(id)detail(id).catch(err=>message(err.message,true));});
  $('select-all').addEventListener('change',ev=>{for(const e of state?.episodes||[])if(e.trainable)(ev.target.checked?selected.add(e.id):selected.delete(e.id));render();});
  $('show-archived').addEventListener('change',()=>state&&render());
  $('release-inference').addEventListener('change',selection);
  $('base').addEventListener('change',()=>{if($('base').value==='qwen3_5_4b')$('quantization').value='4';syncInitialization();});
  $('mode').addEventListener('change',syncInitialization);
  $('initialization').addEventListener('change',()=>{syncInitialization();if($('initialization').value==='upstream_lora')$('lr').value='0.00002';});
  $('jobs').addEventListener('click',async ev=>{try{if(ev.target.dataset.log){logId=ev.target.dataset.log;$('log-panel').hidden=false;$('log-title').textContent=logId+' · 日志';await refresh();}if(ev.target.dataset.cancel){await api('/api/training/cancel',{id:ev.target.dataset.cancel});message('正在取消训练，保留检查点');await refresh();}}catch(err){message(err.message,true);}});
  $('training-form').addEventListener('submit',async ev=>{
    ev.preventDefault();submitting=true;selection();
    try{const result=await api('/api/training/start',{episodes:[...selected],profile:$('base').value,mode:$('mode').value,initialization:$('initialization').value,quantization:Number($('quantization').value),epochs:Number($('epochs').value),rank:Number($('rank').value),learning_rate:Number($('lr').value),max_steps:Number($('max-steps').value),grasp_repeat:Number($('grasp-repeat').value),release_inference:$('release-inference').checked});message(result.message);logId=result.job.id;$('log-panel').hidden=false;await refresh();}catch(err){message(err.message,true);}finally{submitting=false;selection();}
  });
  refresh();setInterval(refresh,3000);
})();
