from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.orm import Session

db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def clear_transaction_caches(session):
    session.info.pop("glorax_settings_cache", None)
    session.info.pop("glorax_dataset_members_cache", None)
