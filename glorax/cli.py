import json

import click
from werkzeug.security import generate_password_hash

from .extensions import db
from .models import Admin


def register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--username", prompt="Логин")
    @click.password_option(confirmation_prompt=True, prompt="Пароль (не менее 12 символов)")
    def create_admin(username, password):
        if not username.strip() or len(username) > 100 or len(password) < 12:
            raise click.ClickException("Логин 1–100 символов, пароль не менее 12 символов.")
        if db.session.execute(db.select(Admin).where(Admin.username == username)).first():
            raise click.ClickException("Логин уже занят.")
        db.session.add(Admin(username=username, password_hash=generate_password_hash(password)))
        db.session.commit()
        click.echo("Администратор создан.")

    @app.cli.command("import-legacy")
    @click.argument("sqlite_path", type=click.Path(exists=True))
    @click.option("--backup-dir", required=True, type=click.Path())
    def import_old(sqlite_path, backup_dir):
        from .legacy import import_legacy

        try:
            click.echo(
                json.dumps(import_legacy(sqlite_path, backup_dir), ensure_ascii=False, indent=2)
            )
        except ValueError as e:
            db.session.rollback()
            raise click.ClickException(str(e))

    @app.cli.command("enqueue-refresh")
    def enqueue():
        from .jobs import enqueue_refresh

        job = enqueue_refresh()
        click.echo(f"Задание: {job.id} ({job.state})")

    @app.cli.command("expire-attempts")
    def expire():
        from .attempts import sweep_expired

        click.echo(f"Проверено попыток: {sweep_expired()}")

    @app.cli.command("export-dataset")
    @click.argument("output", type=click.Path())
    def export(output):
        from pathlib import Path

        from .facts import export_dataset

        Path(output).write_text(
            json.dumps(export_dataset(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        click.echo("Датасет экспортирован без персональных данных.")

    @app.cli.command("generate-questions")
    def generate():
        from .facts import latest_dataset
        from .questions import generate_questions

        dataset = latest_dataset()
        if not dataset:
            raise click.ClickException("Сначала загрузите и опубликуйте данные.")
        result = generate_questions(dataset.id)
        db.session.commit()
        click.echo(json.dumps(result, ensure_ascii=False))
