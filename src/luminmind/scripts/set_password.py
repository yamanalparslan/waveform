"""Kullanıcı parolasını komut satırından değiştirir.

Kullanım:
    python -m luminmind.scripts.set_password admin@luminmind.local YeniGucluSifre

Arayüzdeki **Kullanıcılar** sayfası normal yol; bu betik iki durum için var:

1. Paneli internete açmadan önce seed hesabının `admin` parolasını, arayüze hiç
   girmeden değiştirmek.
2. Parolanın unutulduğu/kilitlenildiği durumda kurtarma (giriş hız sınırına
   takıldığınızda da çalışır — HTTP katmanına hiç uğramaz).

Docker ile çalışırken:
    docker compose exec api python -m luminmind.scripts.set_password <e-posta> <sifre>
"""

import asyncio
import logging
import sys

from sqlalchemy import select

from luminmind.core.db import create_engine, session_scope
from luminmind.core.models import User
from luminmind.core.security import hash_password

logger = logging.getLogger(__name__)

MIN_LENGTH = 8  # web/routes.py:_MIN_PASSWORD_LENGTH ile aynı


async def set_password(email: str, password: str) -> bool:
    """Parolayı günceller; kullanıcı yoksa False döner."""
    engine = create_engine()
    try:
        async with session_scope(engine) as session:
            user = (await session.scalars(select(User).where(User.email == email))).one_or_none()
            if user is None:
                return False
            user.hashed_password = hash_password(password)
            return True
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)

    email, password = sys.argv[1], sys.argv[2]
    if len(password) < MIN_LENGTH:
        print(f"Şifre en az {MIN_LENGTH} karakter olmalı.")
        raise SystemExit(1)

    if asyncio.run(set_password(email, password)):
        print(f"{email} şifresi güncellendi.")
    else:
        print(f"{email} bulunamadı.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
