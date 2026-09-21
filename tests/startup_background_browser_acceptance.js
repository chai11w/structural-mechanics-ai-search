async page => {
 const origin=page.url().split('/').slice(0,3).join('/');
 const checks=[]; const check=(v,m)=>{if(!v)throw Error(m);checks.push(m);};
 const counts=async()=> (await page.request.get(origin+'/fixture/counts')).json();
 const gate=release=>page.evaluate(async release => { const r=await fetch('/fixture/gate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({release})});if(!r.ok)throw Error('fixture gate '+r.status); },release);
 await page.locator('#startup-screen').waitFor({state:'hidden'});
 const reset=page.waitForResponse(r=>r.url()===origin+'/api/reset'&&r.status()===200);
 await page.locator('#top-new-chat').click();await reset;
 await page.waitForFunction(()=>document.querySelector('#status-text').textContent==='任务状态已更新');
 check(await page.locator('body').getAttribute('data-background-execution')==='1','server enables background protocol in rendered HTML');
 const before=await counts();
 await gate(false);
 try {
   await page.locator('#text').fill('startup-background-fixture'); await page.locator('#send').click();
   await page.waitForFunction(()=>Object.keys(localStorage).filter(k=>k.startsWith('tiku-agent-background-job-v1:')).map(k=>JSON.parse(localStorage.getItem(k))).some(r=>!r.done&&r.id));
   await page.waitForFunction(async expected => { const r=await fetch('/fixture/counts');const c=await r.json();return c.calls===expected&&c.operations.some(op=>op.status==='RUNNING'); },before.calls+1);
   await page.reload(); await page.locator('#startup-screen').waitFor({state:'hidden'});
   check((await counts()).calls===before.calls+1,'refresh observes original running job without resubmission');
   await page.locator('#text').fill('draft kept while busy'); await page.locator('#text').press('Enter');
   check(await page.locator('#send').isDisabled(),'busy input accepts draft but send stays disabled');
   check((await counts()).calls===before.calls+1,'Enter during busy does not send another job');
   await gate(true); await page.getByText('reply:startup-background-fixture',{exact:true}).waitFor({timeout:20000});
   check(await page.locator('#text').inputValue()==='draft kept while busy','completion preserves typed draft');
   check(await page.locator('#send').isEnabled(),'completion restores send');
   await page.reload();await page.locator('#startup-screen').waitFor({state:'hidden'});
   check(await page.getByText('reply:startup-background-fixture',{exact:true}).count()===1,'completed reply restored once');
   check((await counts()).calls===before.calls+1,'terminal reload performs zero additional provider calls');
 } finally {await gate(true);}
 return {checks};
}
