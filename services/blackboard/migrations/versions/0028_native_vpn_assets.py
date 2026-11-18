"""Store private Task VPN bytes separately from public Task/resource documents."""

from alembic import op
from bbx_blackboard.store.schema import native_vpn_assets

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    native_vpn_assets.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    native_vpn_assets.drop(bind=op.get_bind(), checkfirst=True)
