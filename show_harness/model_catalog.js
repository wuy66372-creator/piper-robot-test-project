// Catalog entries are validated by the server; paths are never accepted from UI.
(() => {
  const select=document.getElementById('model-profile');
  const arm=document.getElementById('model-arm');
  let requested=new URLSearchParams(location.search).get('lora');
  let restorePending=true;
  let signature='';
  const matchArm=()=>{
    const side=select.selectedOptions[0]?.dataset.arm;
    if(side)arm.value=side;
    else if(arm.value==='dual')arm.value='left';
  };
  select.addEventListener('change',()=>{
    requested=null;restorePending=false;matchArm();window.gumiPrefs?.saveModel();
  });
  async function refresh(){
    try{
      const response=await fetch('/api/model/catalog');
      if(!response.ok)return;
      const data=await response.json();
      if(!data.ok)return;
      window.gumiModelNames=Object.fromEntries(data.models.map(m=>[m.id,m.name]));
      const next=JSON.stringify(data.models);
      if(select.disabled || next===signature)return;
      const previous=requested||(restorePending&&window.gumiPrefs?.get('profile'))||select.value;
      const fallback=select.value;
      const options=data.models.map(m=>{
        const option=document.createElement('option');
        option.value=m.id;option.textContent=m.name;
        if(m.arm)option.dataset.arm=m.arm;
        return option;
      });
      select.replaceChildren(...options);
      if(data.models.some(m=>m.id===previous))select.value=previous;
      else if(data.models.some(m=>m.id===fallback))select.value=fallback;
      if(requested||restorePending){
        matchArm();
        if(requested){
          try{
            const url=new URL(location.href);url.searchParams.delete('lora');
            window.history.replaceState(window.history.state,'',url.pathname+url.search+url.hash);
          }catch(_){}
        }
        requested=null;restorePending=false;window.gumiPrefs?.saveModel();
      }
      signature=next;
    }catch(_){/* Keep existing options when the service is restarting. */}
  }
  refresh();setInterval(refresh,5000);
})();
