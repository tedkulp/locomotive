from alembic import context

from locomotive import models  # noqa: F401  (registers tables)
from locomotive.db import Base

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
