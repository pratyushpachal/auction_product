"""Create the common, batch metric and optional input tables."""
from alembic import op
from kkr_scout.database import metadata
import kkr_scout.metrics
from kkr_scout.inputs import inputs_metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())
    inputs_metadata.create_all(op.get_bind())


def downgrade():
    inputs_metadata.drop_all(op.get_bind())
    op.execute("DROP VIEW IF EXISTS regulation_deliveries")
    metadata.drop_all(op.get_bind())
