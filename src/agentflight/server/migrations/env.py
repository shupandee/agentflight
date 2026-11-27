from logging.config import fileConfig

from alembic import context

from agentflight.server.db import database_url, make_engine
from agentflight.server.models import Base

config = context.config
if config.config_file_name is not None:
    # Keep loggers that already exist (the app's, when migrations run in-process) enabled.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.attributes.get("url") or database_url()
    with make_engine(url).connect() as connection:
        if connection.dialect.name == "sqlite":
            # Batch migrations rebuild SQLite tables (copy, drop, rename). With foreign keys
            # on, dropping ``runs`` would cascade-delete every event.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
