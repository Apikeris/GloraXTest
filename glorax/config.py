import os
from datetime import timedelta
from pathlib import Path
from sqlalchemy.engine import make_url


def configuration():
    production = os.getenv('APP_ENV', 'development') == 'production'
    secret = os.getenv('SECRET_KEY')
    if not secret or len(secret) < 32:
        raise RuntimeError('Задайте SECRET_KEY длиной не менее 32 символов в окружении.')
    url = os.getenv('DATABASE_URL', 'sqlite:///glorax-v2.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://','postgresql+psycopg://',1)
    elif url.startswith('postgresql://'):
        url = url.replace('postgresql://','postgresql+psycopg://',1)
    options = {'pool_pre_ping': True, 'hide_parameters': True}
    if url.startswith('postgresql'):
        options.update(pool_size=int(os.getenv('DB_POOL_SIZE','3')),max_overflow=int(os.getenv('DB_MAX_OVERFLOW','1')),pool_recycle=300,pool_timeout=8,connect_args={'connect_timeout':5,'options':'-c timezone=UTC'})
    if production:
        parsed = make_url(url)
        ca = os.getenv('PGSSLROOTCERT') or parsed.query.get('sslrootcert')
        if not url.startswith('postgresql'):
            raise RuntimeError('Production требует PostgreSQL.')
        if not ca or not Path(ca).is_file():
            raise RuntimeError('Укажите существующий CA-файл PGSSLROOTCERT.')
        if parsed.query.get('sslmode') not in (None,'verify-full'):
            raise RuntimeError('В production разрешён только sslmode=verify-full.')
        options['connect_args'] = {'sslmode':'verify-full','sslrootcert':str(ca),'connect_timeout':5,'options':'-c timezone=UTC'}
    return dict(SECRET_KEY=secret,SQLALCHEMY_DATABASE_URI=url,SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS=options,SESSION_COOKIE_SECURE=production,SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2*1024*1024,WTF_CSRF_TIME_LIMIT=8*60*60,DEBUG=False,
        PRODUCTION=production,DISPLAY_TIMEZONE=os.getenv('DISPLAY_TIMEZONE','Europe/Moscow'),
        WORKER_LEASE_SECONDS=int(os.getenv('WORKER_LEASE_SECONDS','600')),
        WORKER_MAX_ATTEMPTS=int(os.getenv('WORKER_MAX_ATTEMPTS','3')),
        TRUST_PROXY=os.getenv('TRUST_PROXY','0')=='1')
