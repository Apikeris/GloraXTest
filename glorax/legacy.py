"""Read-only SQLite import, backup before touching destination, no guessed history."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .extensions import db
from .models import Dataset, Project, Fact, FactRevision, Question, QuestionRevision, Participant, Attempt, AttemptItem, LegacyRecord, uid, utcnow
from .facts import digest


def import_legacy(path,backup_dir):
    path=Path(path).resolve()
    if not path.is_file(): raise ValueError('Файл старой базы не найден.')
    source_hash=hashlib.sha256(path.read_bytes()).hexdigest()
    directory=Path(backup_dir).resolve();directory.mkdir(parents=True,exist_ok=True)
    backup=directory/f'glorax-{source_hash}.sqlite3'
    src=sqlite3.connect(f'file:{path}?mode=ro',uri=True);src.row_factory=sqlite3.Row
    if not backup.exists():
        dst=sqlite3.connect(backup);src.backup(dst);dst.close();backup.chmod(0o600)
    records={}
    for table in ('project','question','attempt','answer_log','system_status'):
        try: records[table]=[dict(r) for r in src.execute(f'SELECT * FROM {table}')]
        except sqlite3.OperationalError: records[table]=[]
    src.close()
    if db.session.execute(db.select(LegacyRecord).where(LegacyRecord.source_hash==source_hash)).first():
        return {'already_imported':True,'backup':str(backup),'source_hash':source_hash}
    # A source SHA identifies the complete import. All records + mapping commit together.
    ds=Dataset(status='legacy_unverified',report={'source_hash':source_hash,'warning':'Историческая БД не хранит версии вопросов, состав теста, порядок и дедлайны.'})
    db.session.add(ds);db.session.flush()
    mappings={}
    for row in records['project']:
        p=Project(key=f'legacy:{source_hash}:{row["id"]}',name=row.get('name') or 'Название утрачено',city=row.get('city'),status='legacy_unverified',enabled=False,coverage={'reason':'Нет подтверждающих источников; старые сведения не используются в новых тестах.'})
        db.session.add(p);db.session.flush();mappings[('project',row['id'])]=p.id
        for key in ('city','min_price','max_price','pros_cons','features'):
            fact=Fact(project_id=p.id,key=key,category='legacy',scope={},scope_key=digest({}));db.session.add(fact);db.session.flush()
            revision=FactRevision(fact_id=fact.id,dataset_id=ds.id,value=str(row[key]) if row.get(key) is not None else None,value_type='string',method='legacy_import',verification_status='legacy_unverified',is_exclusive=False,missing_reason='Источник и смысл денежных значений не подтверждены',conditions={})
            db.session.add(revision);db.session.flush();fact.current_revision_id=revision.id
    for row in records['question']:
        project_id=mappings.get(('project',row.get('project_id')))
        if not project_id: continue
        q=Question(project_id=project_id,external_id=f'legacy-{row["id"]}',origin='manual',status='needs_review',review_reason='legacy_unverified: формулировка из текущей старой таблицы не является историческим снимком.')
        db.session.add(q);db.session.flush()
        try: distractors=json.loads(row.get('distractors') or '[]')
        except (ValueError,TypeError): distractors=[]
        values=[row.get('correct_answer')]+(distractors if isinstance(distractors,list) else [])
        qr=QuestionRevision(question_id=q.id,dataset_id=ds.id,text=row.get('text') or 'Формулировка утрачена',category='legacy',options=[{'id':str(i),'text':str(v),'fact_revision_id':None} for i,v in enumerate(values)],correct_option_id='0',explanation='legacy_unverified',fingerprint=digest(row))
        db.session.add(qr);db.session.flush();q.current_revision_id=qr.id;mappings[('question',row['id'])]=q.id
    for row in records['attempt']:
        participant=Participant(name=row.get('username') or 'Имя не сохранено');db.session.add(participant);db.session.flush()
        related=[log for log in records['answer_log'] if log.get('attempt_id')==row['id']]
        project_id=mappings.get(('project',int(row['project_filter']))) if str(row.get('project_filter','')).isdigit() else None
        p=db.session.get(Project,project_id) if project_id else None
        started=None
        try: started=datetime.fromisoformat(row.get('start_time') or '').replace(tzinfo=timezone.utc)
        except ValueError: pass
        a=Attempt(participant_id=participant.id,full_name=participant.name,project_id=project_id,project_name=p.name if p else 'Проект не установлен',session_hash=hashlib.sha256(uid().encode()).hexdigest(),dataset_id=ds.id,status='interrupted',started_at=started or utcnow(),last_activity_at=started or utcnow(),total=len(related),score=max(0,row.get('score') or 0),settings={'legacy_unverified':True,'denominator_unknown':True},legacy_data={**row,'warning':'Исходный состав, порядок, время завершения и версии вопросов неизвестны. Балл сохранён без переоценки; количество записей не равно размеру теста.'})
        db.session.add(a);db.session.flush();mappings[('attempt',row['id'])]=a.id
        for position,log in enumerate(related,1):
            item=AttemptItem(attempt_id=a.id,position=position,snapshot={'text':None,'category':'legacy','options':[],'correct_option_id':None,'explanation':'Формулировка на момент ответа и показанный порядок не сохранялись. Исходная запись приведена без реконструкции.','legacy_log':log,'order_unknown':True},outcome='legacy_unknown')
            db.session.add(item);db.session.flush();mappings[('answer_log',log['id'])]=item.id
    for table,rows in records.items():
        for row in rows: db.session.add(LegacyRecord(source_hash=source_hash,table_name=table,old_id=row['id'],new_id=mappings.get((table,row['id'])),data=row))
    db.session.commit()
    return {'counts':{k:len(v) for k,v in records.items()},'backup':str(backup),'source_hash':source_hash,'already_imported':False}
