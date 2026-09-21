async page => {
 const checks=[];const check=(v,m)=>{if(!v)throw Error(m);checks.push(m);};
 await page.unroute('**/assets/demo.js?*');
 await page.route('**/api/session',r=>r.fulfill({status:401,contentType:'application/json',body:JSON.stringify({status:'NEEDS_INPUT',layer:'login',code:'LOGIN_REQUIRED',retryable:false,action:'relogin',request_id:'req_startup_login',search_id:'',schema_version:1})}));
 await page.reload();await page.locator('#startup-screen[data-state="error"]').waitFor();
 check(await page.locator('#startup-reload').innerText()==='重新登录','expired authentication offers login');
 check(!await page.locator('.app-shell').isVisible(),'expired authentication does not expose old transcript');
 await page.unroute('**/api/session');
 await page.reload();await page.locator('#startup-screen').waitFor({state:'hidden'});
 await page.evaluate(()=> { const at=Date.now()-3*3600000;localStorage.setItem('tiku-agent-current-chat-v2',JSON.stringify({lastActivityAt:at,savedAt:at,messages:[{me:true,message:'failure recovery history'}]}));localStorage.setItem('tiku-agent-session-activity-v1',String(at)); });
 await page.route('**/api/reset',r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({code:'SERVICE_UNAVAILABLE'})}));
 await page.reload();await page.locator('#startup-screen[data-state="error"]').waitFor();
 await page.unroute('**/api/reset');
 await page.locator('#startup-new-chat').click();await page.locator('#startup-screen').waitFor({state:'hidden'});
 check(await page.locator('#empty').isVisible(),'explicit new chat exits recovery only after confirmed reset');
 let release;const gate=new Promise(r=>release=r);
 await page.route('**/assets/demo.js?*',async r=>{await gate;await r.continue();});
 await page.setViewportSize({width:390,height:844});
 await page.reload({waitUntil:'commit'});
 await page.waitForFunction(()=>getComputedStyle(document.querySelector('.startup-actions')).visibility==='visible');
 check(await page.locator('#startup-reload').isVisible(),'slow or missing application script offers HTML reload link');
 await page.screenshot({path:'output/playwright/startup-mobile-slow.png',animations:'allow'});
 await page.setViewportSize({width:1280,height:900});
 await page.screenshot({path:'output/playwright/startup-desktop-slow.png',animations:'allow'});
 release();await page.locator('#startup-screen').waitFor({state:'hidden'});await page.unroute('**/assets/demo.js?*');
 return {checks};
}
