import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
const base=process.env.SCOUT_URL||'http://127.0.0.1:8000';
let ready=false;
for(let attempt=0;attempt<60;attempt++){try{const r=await fetch(base+'/health',{signal:AbortSignal.timeout(1000)});if(r.ok){ready=true;break;}}catch{}await new Promise(resolve=>setTimeout(resolve,250));}
assert(ready,'Server did not become ready');
const installed=['C:/Program Files/Google/Chrome/Application/chrome.exe','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(p=>fs.existsSync(p));
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROME||installed?{executablePath:process.env.PLAYWRIGHT_CHROME||installed}:{})});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
const responses=[];page.on('response',r=>{if(r.url().includes('/api/'))responses.push({url:r.url(),status:r.status()});});
const waitLoaded=async()=>{await page.waitForFunction(()=>!document.querySelector('.status'));await page.waitForTimeout(100);await page.waitForFunction(()=>!document.querySelector('.status'));};
const nav=async name=>{await page.getByRole('navigation').getByRole('button',{name,exact:true}).click();await waitLoaded();};
const counts=await (await fetch(base+'/api/dashboard')).json();
await page.goto(base);await waitLoaded();
assert.equal(await page.locator('h1').textContent(),'The scouting desk');
for(const v of Object.values(counts.counts))assert((await page.locator('.stat-grid').textContent()).includes(v.toLocaleString('en-IN')));
await nav('Player Search');
const input=page.getByLabel('Player name');
const players=await (await fetch(base+'/api/players?page_size=2')).json();
assert(players.items.length>=2);
await input.fill(players.items[0].name);await page.getByRole('button',{name:'Search',exact:true}).click();await waitLoaded();
assert((await page.locator('tbody').textContent()).includes(players.items[0].name));
await page.getByRole('button',{name:'Clear filters'}).click();await waitLoaded();
for(const title of ['Competition','Season participation','Observed role','Archetype']){const field=page.getByLabel(title,{exact:true});const opts=await field.locator('option').all();if(opts.length>1){await field.selectOption({index:1});await waitLoaded();await field.selectOption('');await waitLoaded();}}
await page.getByLabel('Minimum rating').fill('1');await waitLoaded();await page.getByLabel('Minimum rating').fill('');await waitLoaded();
for(const value of ['name','sample','score']){await page.getByLabel('Sort by').selectOption(value);await waitLoaded();}
if(await page.getByRole('button',{name:'Next',exact:true}).isEnabled()){await page.getByRole('button',{name:'Next',exact:true}).click();await waitLoaded();await page.getByRole('button',{name:'Previous',exact:true}).click();await waitLoaded();}
await page.getByRole('button',{name:'Report',exact:true}).first().click();await waitLoaded();
assert((await page.locator('h1').textContent()).length>0);
const similar=page.getByRole('button',{name:'Report',exact:true});if(await similar.count()){await similar.first().click();await waitLoaded();}
await nav('Player Report Card');
await nav('Squad Gaps');assert((await page.locator('main').textContent()).includes('Squad file not loaded'));
const download=page.waitForEvent('download');await page.getByRole('link',{name:'Download squad template'}).click();assert((await download).suggestedFilename().endsWith('.csv'));
await nav('Auction Planner');await page.getByLabel('Players to select').fill('2');await page.getByLabel('Minimum batting coverage').fill('1');await page.getByLabel('Minimum bowling coverage').fill('1');
for(const title of ['Competition participation','Season participation']){const field=page.getByLabel(title,{exact:true});if((await field.locator('option').count())>1){await field.selectOption({index:1});await field.selectOption('');}}
await page.getByRole('button',{name:'Run optimizer'}).click();await page.waitForFunction(()=>document.querySelector('.inline-stats')||document.querySelector('[role="alert"]'));assert(!(await page.locator('[role="alert"]').count()));
await page.getByRole('button',{name:/Exclude .* and rerun/}).first().click();await page.waitForSelector('button:has-text("Restore full candidate pool")');await page.getByRole('button',{name:'Restore full candidate pool'}).click();await page.waitForFunction(()=>!document.querySelector('button:disabled')?.textContent.includes('Solving'));await nav('Compare');await waitLoaded();await page.locator('.choice-list button').nth(0).click();await page.waitForFunction(()=>document.querySelectorAll('.selected-list button').length===1);await page.locator('.choice-list button').nth(1).click();await page.waitForFunction(()=>document.querySelectorAll('.selected-list button').length===2);await page.getByRole('button',{name:'Compare selected players'}).click();await waitLoaded();await page.waitForSelector('.compare-grid .card');assert.equal(await page.locator('.compare-grid .card').count(),2);
await page.getByRole('button',{name:/Remove/}).first().click();await page.waitForFunction(()=>document.querySelectorAll('.selected-list button').length===1);await waitLoaded();
await page.getByLabel('Find player').fill(players.items[0].name);await waitLoaded();await page.getByLabel('Find player').fill('');await waitLoaded();
await nav('Methodology');assert((await page.locator('main').textContent()).includes('Data quality'));
await page.setViewportSize({width:390,height:844});await nav('Dashboard');assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
fs.mkdirSync(path.resolve('../reports'),{recursive:true});await page.screenshot({path:path.resolve('../reports/dashboard-mobile.png'),fullPage:true});await page.setViewportSize({width:1440,height:1000});await page.screenshot({path:path.resolve('../reports/dashboard-desktop.png'),fullPage:true});
assert.deepEqual(errors,[]);assert(responses.every(r=>r.status<400),JSON.stringify(responses.filter(r=>r.status>=400)));
const summary={status:'passed',pages:7,controls:'navigation, search, filters, sorting, pagination, report links, squad download, optimizer form, comparison selection and removal',api_requests:responses.length,javascript_errors:errors,mobile_width:390};fs.writeFileSync(path.resolve('../reports/ui-check.json'),JSON.stringify(summary,null,2)+'\n');console.log(JSON.stringify(summary));await browser.close();
