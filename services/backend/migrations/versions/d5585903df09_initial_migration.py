"""initial_migration

Revision ID: d5585903df09
Revises: 
Create Date: 2026-02-11 11:46:58.894016

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = 'd5585903df09'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 插件检查
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # 2. 创建 movies 表
    op.create_table(
        "movies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("year", sa.String(length=100), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("rating_count", sa.Integer(), server_default="0", nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("url", sa.String(length=500), nullable=False, unique=True),
        sa.Column("director", sa.String(length=500), nullable=True),
        sa.Column("stars", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("cover_url", sa.String(length=500), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 3. 创建索引（向量检索 + 模糊搜索）
    op.execute("""
        CREATE INDEX IF NOT EXISTS movies_embedding_hnsw_idx 
        ON movies USING hnsw (embedding vector_cosine_ops) 
        WITH (m = 16, ef_construction = 64);
    """)
    
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_movie_search_trgm 
        ON movies USING GIN (
            (coalesce(title, '')::text || ' '::text || coalesce(summary, '')::text) gin_trgm_ops
        );
    """)

def downgrade() -> None:
    # 降级时删除
    op.drop_index('movies_embedding_hnsw_idx', table_name='movies')
    op.drop_index('idx_movie_search_trgm', table_name='movies')
    op.drop_table('movies')
