"""Where each restaurant chain publishes its nutrition, remembered between lookups."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.db.models import ChainSource


def _key(chain: str) -> str:
    return chain.strip().lower()[:255]


def find(s: Session, chain: str) -> ChainSource | None:
    return s.scalars(select(ChainSource).where(ChainSource.chain == _key(chain))).first()


def remember(s: Session, chain: str, url: str | None, readable: bool) -> None:
    """Save the chain's official page. Only a page Claude could read replaces a known one."""
    url = (url or "").strip()[:1024]
    if not chain.strip() or not url.lower().startswith(("http://", "https://")):
        return
    known = find(s, chain)
    if known is None:
        s.add(ChainSource(chain=_key(chain), url=url, readable=readable))
    elif readable or known.url == url:
        known.url, known.readable = url, readable
    s.flush()
