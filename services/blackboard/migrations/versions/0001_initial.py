"""Initial blackboard storage.

Revision ID: 0001
Revises:
"""

from alembic import op
from bbx_blackboard.store.schema import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    metadata.create_all(bind=op.get_bind())


def downgrade():
    metadata.drop_all(bind=op.get_bind())
