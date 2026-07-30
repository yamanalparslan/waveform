import json
import os
from pathlib import Path
from typing import Any

# Testler geliştiricinin `.env` dosyasından tamamen bağımsız olmalı. Bu satır
# `luminmind.config` ilk kez içe aktarılmadan önce çalışır (conftest her zaman
# test modüllerinden önce yüklenir) ve dosya okumasını kapatır. Aksi halde
# makinede `.env` olup olmamasına göre testler farklı sonuç verir.
os.environ["LM_ENV_FILE"] = ""

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(relative_path: str) -> dict[str, Any]:
        return json.loads((FIXTURES / relative_path).read_text())

    return _load
