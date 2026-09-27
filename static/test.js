'use strict';
const root=document.getElementById('quiz');
const base=`/api/attempts/${root.dataset.attemptId}`;
const statusEl=document.getElementById('connection-status');
const retry=document.getElementById('retry');
const options=document.getElementById('options');
let active=null,endAt=0,busy=false,pending=null,timer=null;
const csrf=document.querySelector('meta[name="csrf-token"]').content;
function disable(){options.querySelectorAll('button').forEach(b=>b.disabled=true);}
function problem(){statusEl.textContent='Нет подтверждения сервера. Ответ пока не считается сохранённым. Время продолжает идти.';retry.hidden=false;disable();busy=false;}
async function load(){
 if(busy)return;busy=true;retry.hidden=true;
 const sent=performance.now();
 try{
  const response=await fetch(`${base}/current`,{cache:'no-store'});if(!response.ok)throw new Error();const q=await response.json();
  if(q.status!=='in_progress'){location.assign(q.result_url);return;}
  active=q;endAt=performance.now()+Math.max(0,Date.parse(q.deadline)-Date.parse(q.server_now))-(performance.now()-sent);
  document.getElementById('question-text').textContent=q.text;
  document.getElementById('progress-text').textContent=`Вопрос ${q.number} из ${q.total}`;
  document.getElementById('progress').value=q.number-1;options.replaceChildren();
  q.options.forEach(o=>{const b=document.createElement('button');b.className='option-btn';b.textContent=o.text;b.addEventListener('click',()=>submit(q.id,o.id));options.appendChild(b);});
  statusEl.textContent='Выберите один ответ';busy=false;clearInterval(timer);timer=setInterval(tick,100);tick();
 }catch(e){problem();}
}
async function submit(questionId,optionId){
 if(busy)return;busy=true;disable();pending={question_id:questionId,option_id:optionId};statusEl.textContent='Сохраняем ответ…';
 try{
  const response=await fetch(`${base}/answer`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(pending)});
  const data=await response.json();
  if(!response.ok&&!data.expired)throw new Error();
  pending=null;statusEl.textContent=data.accepted?'Ответ сохранён':'Время истекло';busy=false;await load();
 }catch(e){problem();}
}
function tick(){const left=Math.max(0,Math.ceil((endAt-performance.now())/1000));document.getElementById('timer').textContent=`${left} с`;if(left===0&&!busy){clearInterval(timer);disable();if(!pending)load();}}
retry.addEventListener('click',()=>{if(pending)submit(pending.question_id,pending.option_id);else load();});
load();
