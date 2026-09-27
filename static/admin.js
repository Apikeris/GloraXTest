'use strict';
const copy=document.getElementById('copy-prompt');
if(copy)copy.addEventListener('click',async()=>{const area=document.getElementById('ai-prompt');try{await navigator.clipboard.writeText(area.value);document.getElementById('copy-status').textContent='Промпт скопирован';}catch(e){area.focus();area.select();document.getElementById('copy-status').textContent='Выделено. Скопируйте текст клавишами Ctrl/Cmd+C.';}});
const job=document.getElementById('job-status');
if(job&&['queued','running'].includes(job.dataset.state)){
 const labels={queued:'В очереди',running:'Выполняется',succeeded:'Завершено',failed:'Ошибка'};
 const stages={discovery:'Обнаружение проектов',collection:'Сбор',normalization:'Нормализация'};
 const timer=setInterval(async()=>{try{const r=await fetch(job.dataset.url);if(!r.ok)return;const data=await r.json();document.getElementById('job-stage').textContent=stages[data.stage]||data.stage;document.getElementById('job-progress').textContent=`${data.progress} / ${data.total} · ${labels[data.state]||data.state}`;document.getElementById('job-detail').textContent=data.detail||'';document.getElementById('job-error').textContent=data.error||'';document.getElementById('job-report').textContent=JSON.stringify(data.report,null,2);if(!['queued','running'].includes(data.state)){clearInterval(timer);location.reload();}}catch(e){document.getElementById('job-detail').textContent='Не удалось получить статус. Повторяем запрос…';}},4000);
}
