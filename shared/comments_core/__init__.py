"""Shared primitives for UC table/column comment promotion."""

from comments_core.models import (
    CommentChange,
    CommentSet,
    PromotionRequest,
    TableComments,
    TableRef,
)
from comments_core.config import CatalogConfig
from comments_core.diff import compute_changes
from comments_core.extract import extract_comments
from comments_core.apply import ApplyResult, execute, generate_sql
from comments_core.yaml_io import read_comment_set, write_comment_set

__all__ = [
    "ApplyResult",
    "CatalogConfig",
    "CommentChange",
    "CommentSet",
    "PromotionRequest",
    "TableComments",
    "TableRef",
    "compute_changes",
    "execute",
    "extract_comments",
    "generate_sql",
    "read_comment_set",
    "write_comment_set",
]
