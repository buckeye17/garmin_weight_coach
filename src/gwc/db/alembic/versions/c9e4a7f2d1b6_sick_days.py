"""sick days

Revision ID: c9e4a7f2d1b6
Revises: b5d2f8a1c3e4
Create Date: 2026-11-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9e4a7f2d1b6'
down_revision: Union[str, Sequence[str], None] = 'b5d2f8a1c3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('food_day_status', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_sick', sa.Boolean(), nullable=False, server_default='0')
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('food_day_status', schema=None) as batch_op:
        batch_op.drop_column('is_sick')
