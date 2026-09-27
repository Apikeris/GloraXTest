"""One deployment migration process, serialized by a PostgreSQL session lock."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from flask_migrate import upgrade
from sqlalchemy import text
from glorax import create_app
from glorax.extensions import db

app=create_app()
with app.app_context():
    if db.engine.dialect.name=='postgresql':
        with db.engine.connect() as lock:
            lock.execute(text('SELECT pg_advisory_lock(73192042)'))
            try: upgrade(directory=str(Path(__file__).resolve().parent.parent/'migrations'))
            finally: lock.execute(text('SELECT pg_advisory_unlock(73192042)'))
    else: upgrade(directory=str(Path(__file__).resolve().parent.parent/'migrations'))
