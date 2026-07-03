"""Small shared string helpers."""

from __future__ import annotations


def escape_like(q: str) -> str:
    """Escape LIKE/ILIKE wildcards so a search for 「100%棉」 or 「A_B」 matches literally.

    Pair with ``.ilike(f"%{escape_like(q)}%", escape="\\\\")`` — the escape char must be
    declared on the operator or Postgres treats the backslashes as literals.
    """
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
