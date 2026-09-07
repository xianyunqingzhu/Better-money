const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const output = path.resolve(__dirname, '../../build-output');
require('node:fs').mkdirSync(output, {recursive:true});
(async () => {
 const browser = await chromium.launch({channel:'msedge',headless:true});
 const page = await browser.newPage({viewport:{width:360,height:800},isMobile:true,hasTouch:true});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.UI_TEST_URL || 'http://127.0.0.1:5173');await page.locator('#home-todos-count').filter({hasText:'0'}).waitFor();
 const add=async text=>{await page.getByRole('button',{name:'新建待办',exact:true}).click();await page.locator('#todo-title-input').fill(text);await page.getByRole('button',{name:'保存',exact:true}).click();await page.locator('.todo-title').filter({hasText:text}).waitFor();};
 await page.locator('#home-manage-todos').click();await add('给生活留一点余地');await add('整理本周的支出');
 // Unsaved editor returns to manager, with explicit discard protection.
 await page.locator('.todo-item').first().getByRole('button',{name:'编辑',exact:true}).click();
 await page.locator('#todo-title-input').fill('不保存的内容');await page.locator('#todos-back').click();await page.locator('#dialog-cancel').click();assert.equal(await page.locator('#todo-title-input').inputValue(),'不保存的内容');
 await page.locator('#todos-back').click();await page.locator('#dialog-ok').click();assert.equal(await page.locator('.todo-title').first().textContent(),'给生活留一点余地');
 // Cancel and accept permanent deletion; completed history supports deletion too.
 await page.getByRole('button',{name:'删除：整理本周的支出',exact:true}).click();await page.locator('#dialog-cancel').click();assert.equal(await page.locator('.todo-item').count(),2);
 await page.getByRole('button',{name:'删除：整理本周的支出',exact:true}).click();await page.locator('#dialog-ok').click();await page.waitForFunction(()=>document.querySelectorAll('.todo-item').length===1);
 await add('散步半小时');await page.screenshot({path:path.join(output, 'comfort-manager.png')});
 await page.locator('#todos-back').click();
 await page.getByRole('checkbox',{name:'完成：给生活留一点余地',exact:true}).click();await page.locator('.toast-action').click();await page.waitForFunction(()=>document.querySelectorAll('#home-todos input').length===2);
 await page.getByRole('checkbox',{name:'完成：散步半小时',exact:true}).click();await page.locator('#home-manage-todos').click();await page.locator('#todos-body').getByRole('button',{name:'历史记录',exact:true}).click();
 await page.getByRole('button',{name:'删除：散步半小时',exact:true}).click();await page.locator('#dialog-ok').click();await page.locator('#todos-body .empty-state').waitFor();
 await page.locator('#todos-back').click();await page.locator('#todos-back').click();await page.reload();await page.locator('#home-todos-count').filter({hasText:'1'}).waitFor();
 // A failed write must keep the todo visible and allow retry.
 await page.evaluate(()=>{const original=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key.endsWith('better_money.db.tmp')){Storage.prototype.setItem=original;throw new Error('simulated storage failure');}return original.call(this,key,value);};});
 await page.getByRole('checkbox',{name:'完成：给生活留一点余地',exact:true}).click();
 await page.locator('.toast-error').waitFor();assert.equal(await page.locator('#home-todos input').count(),1);
 await page.reload();await page.locator('#home-todos-count').filter({hasText:'1'}).waitFor();
 await page.getByRole('button',{name:'分析与历史',exact:true}).click();await page.waitForFunction(()=>document.querySelector('[data-page="page-insights"]').getAttribute('aria-current')==='page');
 await page.screenshot({path:path.join(output, 'comfort-insights.png')});
 await page.getByRole('button',{name:'记账与待办',exact:true}).click();
 for(const width of [320,360,412]) {
  await page.setViewportSize({width,height:800});
  for(const size of ['16px','24px']) {
   await page.evaluate(size=>document.documentElement.style.fontSize=size,size);
   assert.ok(await page.locator('#home-scroll').evaluate(e=>e.scrollWidth<=e.clientWidth),`home overflow ${width}/${size}`);
   await page.locator('#home-manage-todos').click();
   assert.ok(await page.locator('#todos-body').evaluate(e=>e.scrollWidth<=e.clientWidth),`manager overflow ${width}/${size}`);
   await page.locator('#todos-back').click();
  }
 }
 await page.evaluate(()=>document.documentElement.style.fontSize='16px');await page.setViewportSize({width:360,height:800});
 await page.evaluate(()=>document.querySelector('#toast-box')?.replaceChildren());
 await page.screenshot({path:path.join(output, 'comfort-home.png')});
 await page.emulateMedia({colorScheme:'dark',reducedMotion:'reduce'});
 assert.equal(await page.locator('.sheet-box').first().evaluate(e=>getComputedStyle(e).transitionDuration),'0s');
 await page.screenshot({path:path.join(output, 'comfort-home-dark.png')});
 // Opening a sheet during its close delay must cancel the old hide operation.
 await page.emulateMedia({reducedMotion:'no-preference'});
 await page.evaluate(async()=>{const dom=await import('/src/ui/dom.ts');dom.openSheet('#entry-sheet');await new Promise(r=>setTimeout(r,30));dom.closeSheet('#entry-sheet');dom.openSheet('#entry-sheet');});
 await page.waitForTimeout(300);assert.equal(await page.locator('#entry-sheet').isVisible(),true);
 await page.evaluate(async()=>{const dom=await import('/src/ui/dom.ts');dom.closeSheet('#entry-sheet');});
 assert.deepEqual(errors,[]);console.log('PASS: delete/cancel/history deletion/restart/undo/discard/back/navigation; 320/360/412 widths at 100/150% text; dark/reduced motion; sheet interruption.');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});

