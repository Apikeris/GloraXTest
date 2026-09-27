"""One deployment migration process, serialized by a PostgreSQL session lock."""
import sys
import argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from flask_migrate import upgrade
from sqlalchemy import text
from glorax import create_app
from glorax.extensions import db

def prepare_database(app, enqueue_initial=False):
    from glorax.jobs import enqueue_refresh
    from glorax.models import Dataset, Job

    def migrate_and_seed():
        upgrade(directory=str(Path(__file__).resolve().parent.parent/'migrations'))
        # Existing queued/failed/successful work is never reset by a deployment.
        if enqueue_initial and not db.session.scalar(db.select(Dataset.id).where(Dataset.status=='published').limit(1)) and not db.session.scalar(db.select(Job.id).where(Job.kind=='refresh').limit(1)):
            enqueue_refresh()
            print('Render: initial data collection queued', flush=True)
        db.session.remove()

    with app.app_context():
        if db.engine.dialect.name=='postgresql':
            with db.engine.connect() as lock:
                lock.execute(text('SELECT pg_advisory_lock(73192042)'))
                lock.commit()  # Session lock survives commit; avoid idle-in-transaction timeout.
                try:
                    migrate_and_seed()
                finally:
                    lock.execute(text('SELECT pg_advisory_unlock(73192042)'))
                    lock.commit()
        else:
            migrate_and_seed()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--enqueue-initial-refresh', action='store_true')
    args=parser.parse_args()
    prepare_database(create_app(), enqueue_initial=args.enqueue_initial_refresh)
