"""food category

Revision ID: d3b7e91a5c20
Revises: a54043480db4
Create Date: 2026-10-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3b7e91a5c20'
down_revision: Union[str, Sequence[str], None] = 'a54043480db4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('food_category',
    sa.Column('food_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['food_id'], ['food.id'], name=op.f('fk_food_category_food_id_food'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('food_id', 'name', name=op.f('pk_food_category'))
    )
    with op.batch_alter_table('food_category', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_food_category_name'), ['name'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('food_category', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_food_category_name'))

    op.drop_table('food_category')
