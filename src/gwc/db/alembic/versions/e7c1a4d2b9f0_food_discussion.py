"""food discussion: chain sources, source notes, coach proposals

Revision ID: e7c1a4d2b9f0
Revises: db54030824a3
Create Date: 2026-11-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7c1a4d2b9f0'
down_revision: Union[str, Sequence[str], None] = 'db54030824a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('chain_source',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chain', sa.String(length=255), nullable=False),
    sa.Column('url', sa.String(length=1024), nullable=False),
    sa.Column('readable', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chain_source')),
    sa.UniqueConstraint('chain', name=op.f('uq_chain_source_chain'))
    )
    with op.batch_alter_table('food', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_note', sa.Text(), nullable=True))
    with op.batch_alter_table('coach_conversation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('food_log_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_coach_conversation_food_log_id_food_log'),
            'food_log', ['food_log_id'], ['id'], ondelete='SET NULL',
        )
    op.create_table('coach_proposal',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('food_log_id', sa.Integer(), nullable=True),
    sa.Column('changes', sa.Text(), nullable=False),
    sa.Column('before', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=True),
    sa.Column('update_library', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['coach_conversation.id'], name=op.f('fk_coach_proposal_conversation_id_coach_conversation'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['food_log_id'], ['food_log.id'], name=op.f('fk_coach_proposal_food_log_id_food_log'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_coach_proposal'))
    )
    with op.batch_alter_table('coach_proposal', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_coach_proposal_conversation_id'), ['conversation_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('coach_proposal', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_coach_proposal_conversation_id'))
    op.drop_table('coach_proposal')
    with op.batch_alter_table('coach_conversation', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_coach_conversation_food_log_id_food_log'), type_='foreignkey')
        batch_op.drop_column('food_log_id')
    with op.batch_alter_table('food', schema=None) as batch_op:
        batch_op.drop_column('source_note')
    op.drop_table('chain_source')
