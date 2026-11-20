"""Migration connection uses DATABASE_URL through the application engine."""
from alembic import context
from kkr_scout.database import get_engine, metadata
import kkr_scout.metrics
from kkr_scout.inputs import inputs_metadata

target_metadata = [metadata, inputs_metadata]

if context.is_offline_mode():
    context.configure(url=str(get_engine().url), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with get_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
