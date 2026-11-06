"""gout: purine profiles, flares and their daily severity

Revision ID: b5d2f8a1c3e4
Revises: e7c1a4d2b9f0
Create Date: 2026-11-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import gwc.db.types


# revision identifiers, used by Alembic.
revision: str = 'b5d2f8a1c3e4'
down_revision: Union[str, Sequence[str], None] = 'e7c1a4d2b9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('gout_profile',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('food_id', sa.Integer(), nullable=True),
    sa.Column('food_log_id', sa.Integer(), nullable=True),
    sa.Column('purine_mg', sa.Float(), nullable=False),
    sa.Column('animal_purine_mg', sa.Float(), nullable=False),
    sa.Column('alcohol_g', sa.Float(), nullable=False),
    sa.Column('sweet_drink', sa.Boolean(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', gwc.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', gwc.db.types.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['food_id'], ['food.id'], name=op.f('fk_gout_profile_food_id_food'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['food_log_id'], ['food_log.id'], name=op.f('fk_gout_profile_food_log_id_food_log'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gout_profile')),
    sa.UniqueConstraint('food_id', name=op.f('uq_gout_profile_food_id')),
    sa.UniqueConstraint('food_log_id', name=op.f('uq_gout_profile_food_log_id'))
    )
    op.create_table('gout_flare',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('started_at', gwc.db.types.UTCDateTime(), nullable=False),
    sa.Column('tz', sa.String(length=64), nullable=False),
    sa.Column('joints', sa.JSON(), nullable=False),
    sa.Column('ended_on', sa.Date(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', gwc.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', gwc.db.types.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gout_flare'))
    )
    with op.batch_alter_table('gout_flare', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gout_flare_started_at'), ['started_at'], unique=False)
    op.create_table('gout_day',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('flare_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('severity', sa.Integer(), nullable=False),
    sa.Column('medications', sa.JSON(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', gwc.db.types.UTCDateTime(), nullable=False),
    sa.Column('updated_at', gwc.db.types.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['flare_id'], ['gout_flare.id'], name=op.f('fk_gout_day_flare_id_gout_flare'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gout_day')),
    sa.UniqueConstraint('flare_id', 'day', name=op.f('uq_gout_day_flare_id'))
    )
    with op.batch_alter_table('gout_day', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gout_day_flare_id'), ['flare_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('gout_day', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_gout_day_flare_id'))
    op.drop_table('gout_day')
    with op.batch_alter_table('gout_flare', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_gout_flare_started_at'))
    op.drop_table('gout_flare')
    op.drop_table('gout_profile')
